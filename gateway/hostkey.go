// Host-key pinning for gateway hops.
//
// A hop used to run with StrictHostKeyChecking=no and UserKnownHostsFile=/dev/null,
// so anything answering on a target's address or tunnel port got the session.
// Now every hop is checked against the key the registry has pinned for that
// DEVICE — not for its address.
//
// The pin is keyed on the device id, via ssh's HostKeyAlias. That matters here:
// every reverse-tunnel target answers at localhost:<port> from one reallocated
// pool, so an address-keyed pin would cross-wire devices as ports are reused,
// and a direct-ssh target that changed LAN IP would silently lose its pin.

package main

import (
	"crypto/sha256"
	"encoding/base64"
	"fmt"
	"os"
	"strings"
)

// hostKeyTypes are the key types a pin may name. The registry validates what it
// stores; the gateway validates again because the gateway is what writes the
// value into a known_hosts file.
var hostKeyTypes = map[string]bool{
	"ssh-ed25519":                        true,
	"ssh-rsa":                            true,
	"ecdsa-sha2-nistp256":                true,
	"ecdsa-sha2-nistp384":                true,
	"ecdsa-sha2-nistp521":                true,
	"sk-ssh-ed25519@openssh.com":         true,
	"sk-ecdsa-sha2-nistp256@openssh.com": true,
}

// hostKeyAlias is the name a device's pin is filed under in known_hosts. It is
// derived from the device id alone, so it survives an address or tunnel-port
// change.
func hostKeyAlias(deviceID string) string {
	return "samcloud-" + deviceID
}

// sanitizeHostPubkey returns the key as "<type> <base64>", or ok=false.
//
// It refuses anything carrying a newline: a second line in the known_hosts file
// would pin a host pattern nobody asked for (a "*" line would trust every
// target). A trailing comment is dropped.
func sanitizeHostPubkey(raw string) (string, bool) {
	if strings.ContainsAny(raw, "\n\r") {
		return "", false
	}
	fields := strings.Fields(raw)
	if len(fields) < 2 {
		return "", false
	}
	keytype, body := fields[0], fields[1]
	if !hostKeyTypes[keytype] {
		return "", false
	}
	blob, err := base64.StdEncoding.DecodeString(body)
	if err != nil || len(blob) < 4 {
		return "", false
	}
	// SSH wire format: uint32 length, then that many bytes of algorithm name.
	nameLen := int(blob[0])<<24 | int(blob[1])<<16 | int(blob[2])<<8 | int(blob[3])
	if nameLen < 0 || nameLen > len(blob)-4 {
		return "", false
	}
	if string(blob[4:4+nameLen]) != keytype {
		return "", false
	}
	return keytype + " " + body, true
}

// knownHostsLine is the single line written to the per-hop known_hosts file.
func knownHostsLine(deviceID, hostPubkey string) (string, bool) {
	key, ok := sanitizeHostPubkey(hostPubkey)
	if !ok {
		return "", false
	}
	return hostKeyAlias(deviceID) + " " + key + "\n", true
}

// parseKnownHostsLine pulls the key back out of what ssh wrote when it learned
// one, matching on the alias. HashKnownHosts is forced off for the hop so this
// sees a plain alias rather than a salted hash (Debian's ssh_config turns
// hashing on, and the gateway image is Debian).
func parseKnownHostsLine(content, deviceID string) (string, bool) {
	alias := hostKeyAlias(deviceID)
	for _, line := range strings.Split(content, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) < 3 || fields[0] != alias {
			continue
		}
		if key, ok := sanitizeHostPubkey(fields[1] + " " + fields[2]); ok {
			return key, true
		}
	}
	return "", false
}

// sshFingerprint renders a key the way ssh prints one: SHA256:<unpadded base64>.
func sshFingerprint(hostPubkey string) string {
	key, ok := sanitizeHostPubkey(hostPubkey)
	if !ok {
		return "(unreadable key)"
	}
	blob, err := base64.StdEncoding.DecodeString(strings.Fields(key)[1])
	if err != nil {
		return "(unreadable key)"
	}
	sum := sha256.Sum256(blob)
	return "SHA256:" + strings.TrimRight(base64.StdEncoding.EncodeToString(sum[:]), "=")
}

// writeKnownHosts writes the per-hop known_hosts file and returns its path with
// a remove func. An empty hostPubkey yields an empty file — the learn path.
//
// One file per hop, removed after, so concurrent hops share no mutable state.
func writeKnownHosts(deviceID, hostPubkey string) (path string, remove func(), err error) {
	f, err := os.CreateTemp("", "samcloud-knownhosts-")
	if err != nil {
		return "", func() {}, err
	}
	path = f.Name()
	remove = func() { os.Remove(path) }

	if hostPubkey != "" {
		line, ok := knownHostsLine(deviceID, hostPubkey)
		if !ok {
			f.Close()
			remove()
			return "", func() {}, fmt.Errorf("pinned host key for %s is unreadable", deviceID)
		}
		if _, err := f.WriteString(line); err != nil {
			f.Close()
			remove()
			return "", func() {}, err
		}
	}
	if err := f.Close(); err != nil {
		remove()
		return "", func() {}, err
	}
	return path, remove, nil
}

// hostKeySSHArgs are the ssh options that make a hop verify the target.
//
// With a pin: strict, so a changed or unpinned-type key is refused. ssh prints
// nothing at all when the key matches.
// Without one: accept-new, so enrolling a device still needs no person — the key
// it presents is learned and then pinned.
func hostKeySSHArgs(deviceID, hostPubkey, knownHostsPath string) []string {
	strict := "yes"
	if hostPubkey == "" {
		strict = "accept-new"
	}
	return []string{
		"-o", "HostKeyAlias=" + hostKeyAlias(deviceID),
		"-o", "StrictHostKeyChecking=" + strict,
		"-o", "UserKnownHostsFile=" + knownHostsPath,
		// Read the learned key back by alias rather than a salted hash.
		"-o", "HashKnownHosts=no",
	}
}

// stderrSniffer passes stderr through untouched while keeping a bounded copy of
// the start of it, so the gateway can tell a refused host key from any other
// transport failure (ssh exits 255 for both).
type stderrSniffer struct {
	out  *os.File
	seen []byte
}

const snifferCap = 8192

func (s *stderrSniffer) Write(p []byte) (int, error) {
	if len(s.seen) < snifferCap {
		room := snifferCap - len(s.seen)
		if room > len(p) {
			room = len(p)
		}
		s.seen = append(s.seen, p[:room]...)
	}
	return s.out.Write(p)
}

// sawHostKeyRefusal reports whether ssh refused the target's host key. ssh's
// banner arrives before the remote command produces anything, so the bounded
// copy always contains it when it happened.
func (s *stderrSniffer) sawHostKeyRefusal() bool {
	return hostKeyRefused(string(s.seen))
}

func hostKeyRefused(stderr string) bool {
	return strings.Contains(stderr, "Host key verification failed") ||
		strings.Contains(stderr, "REMOTE HOST IDENTIFICATION HAS CHANGED")
}

// offeredFingerprint lifts the rejected key's fingerprint out of ssh's message,
// so the gateway can name both keys itself instead of pointing up the screen.
// Empty when ssh did not print one.
func (s *stderrSniffer) offeredFingerprint() string {
	return offeredFingerprint(string(s.seen))
}

func offeredFingerprint(stderr string) string {
	const marker = "sent by the remote host is"
	i := strings.Index(stderr, marker)
	if i < 0 {
		return ""
	}
	rest := stderr[i+len(marker):]
	j := strings.Index(rest, "SHA256:")
	if j < 0 {
		return ""
	}
	fp := rest[j:]
	// Runs to the end of the base64 token; ssh follows it with "." and a newline.
	end := strings.IndexFunc(fp[len("SHA256:"):], func(r rune) bool {
		return !(r >= 'A' && r <= 'Z' || r >= 'a' && r <= 'z' ||
			r >= '0' && r <= '9' || r == '+' || r == '/' || r == '=')
	})
	if end >= 0 {
		fp = fp[:len("SHA256:")+end]
	}
	if fp == "SHA256:" {
		return ""
	}
	return fp
}
