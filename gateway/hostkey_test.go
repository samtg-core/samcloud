package main

import (
	"encoding/base64"
	"os"
	"strings"
	"testing"
)

// ed25519HostKey builds a syntactically real host key in SSH wire format
// (uint32 length + bytes, twice), so the validator is exercised for real.
func ed25519HostKey(fill byte) string {
	sshString := func(b []byte) []byte {
		n := len(b)
		return append([]byte{byte(n >> 24), byte(n >> 16), byte(n >> 8), byte(n)}, b...)
	}
	body := make([]byte, 32)
	for i := range body {
		body[i] = fill
	}
	blob := append(sshString([]byte("ssh-ed25519")), sshString(body)...)
	return "ssh-ed25519 " + base64.StdEncoding.EncodeToString(blob)
}

func TestHostKeyAliasIsDerivedFromDeviceIDOnly(t *testing.T) {
	// The whole point: the pin does not move when an address or tunnel port does.
	if got := hostKeyAlias("studio-services"); got != "samcloud-studio-services" {
		t.Fatalf("hostKeyAlias = %q", got)
	}
	if hostKeyAlias("a") == hostKeyAlias("b") {
		t.Fatal("two devices share an alias")
	}
}

func TestSanitizeHostPubkeyAcceptsARealKey(t *testing.T) {
	key := ed25519HostKey(0xAA)
	got, ok := sanitizeHostPubkey(key)
	if !ok || got != key {
		t.Fatalf("sanitizeHostPubkey(%q) = %q, %v", key, got, ok)
	}
}

func TestSanitizeHostPubkeyDropsATrailingComment(t *testing.T) {
	key := ed25519HostKey(0xAA)
	got, ok := sanitizeHostPubkey(key + " root@host")
	if !ok || got != key {
		t.Fatalf("got %q, %v", got, ok)
	}
}

func TestSanitizeHostPubkeyRefusesAnInjectedSecondLine(t *testing.T) {
	// A newline would put a second pattern in known_hosts; "*" would trust every
	// target the gateway ever hops to.
	evil := ed25519HostKey(0xAA) + "\n* " + ed25519HostKey(0xBB)
	if _, ok := sanitizeHostPubkey(evil); ok {
		t.Fatal("accepted a key carrying a newline")
	}
	if _, ok := sanitizeHostPubkey(ed25519HostKey(0xAA) + "\r"); ok {
		t.Fatal("accepted a key carrying a carriage return")
	}
}

func TestSanitizeHostPubkeyRefusesBadInput(t *testing.T) {
	good := ed25519HostKey(0xAA)
	body := strings.Fields(good)[1]
	cases := map[string]string{
		"empty":           "",
		"one field":       "ssh-ed25519",
		"unknown type":    "ssh-magic AAAA",
		"not base64":      "ssh-ed25519 not!base64!",
		"type mismatch":   "ssh-rsa " + body,
		"truncated blob":  "ssh-ed25519 " + base64.StdEncoding.EncodeToString([]byte{0, 0}),
		"absurd name len": "ssh-ed25519 " + base64.StdEncoding.EncodeToString([]byte{0x7f, 0xff, 0xff, 0xff, 1, 2}),
	}
	for name, in := range cases {
		if _, ok := sanitizeHostPubkey(in); ok {
			t.Errorf("%s: accepted %q", name, in)
		}
	}
}

func TestKnownHostsLineIsTheAliasThenTheKey(t *testing.T) {
	key := ed25519HostKey(0xAA)
	line, ok := knownHostsLine("studio-services", key)
	if !ok {
		t.Fatal("knownHostsLine refused a good key")
	}
	if line != "samcloud-studio-services "+key+"\n" {
		t.Fatalf("line = %q", line)
	}
}

func TestParseKnownHostsLineRoundTrips(t *testing.T) {
	key := ed25519HostKey(0xAA)
	line, _ := knownHostsLine("studio-services", key)
	got, ok := parseKnownHostsLine(line, "studio-services")
	if !ok || got != key {
		t.Fatalf("round trip gave %q, %v", got, ok)
	}
}

func TestParseKnownHostsLineIgnoresOtherDevicesAndComments(t *testing.T) {
	keyA, keyB := ed25519HostKey(0xAA), ed25519HostKey(0xBB)
	content := "# a comment\n" +
		"samcloud-other " + keyB + "\n" +
		"\n" +
		"samcloud-wanted " + keyA + "\n"
	got, ok := parseKnownHostsLine(content, "wanted")
	if !ok || got != keyA {
		t.Fatalf("got %q, %v", got, ok)
	}
	if _, ok := parseKnownHostsLine(content, "absent"); ok {
		t.Fatal("found a key for a device that is not there")
	}
}

func TestParseKnownHostsLineRejectsAHashedEntry(t *testing.T) {
	// HashKnownHosts is forced off for a hop; if it were ever on, the alias would
	// be a salted hash and must not be mistaken for a key we can read back.
	content := "|1|c2FsdA==|aGFzaA== " + ed25519HostKey(0xAA) + "\n"
	if _, ok := parseKnownHostsLine(content, "studio-services"); ok {
		t.Fatal("read a key out of a hashed entry")
	}
}

func TestSSHFingerprintMatchesSSHKeygen(t *testing.T) {
	// Verified against the real tool: `ssh-keygen -lf` on this exact key prints
	// "256 SHA256:0M0y80ai0sm0JRSMIydZXsm3fAPifTYR1w3QgnI8nGM". The gateway's
	// message must agree with what ssh shows a person, or the two fingerprints in
	// a mismatch report cannot be compared.
	const wantAA = "SHA256:0M0y80ai0sm0JRSMIydZXsm3fAPifTYR1w3QgnI8nGM"
	key := ed25519HostKey(0xAA)
	if got := sshFingerprint(key); got != wantAA {
		t.Fatalf("sshFingerprint = %q, want %q (ssh-keygen -lf)", got, wantAA)
	}
	if sshFingerprint(key) == sshFingerprint(ed25519HostKey(0xBB)) {
		t.Fatal("two different keys share a fingerprint")
	}
	if sshFingerprint("garbage") != "(unreadable key)" {
		t.Fatal("garbage produced a fingerprint")
	}
}

func TestHostKeySSHArgsAreStrictWhenPinned(t *testing.T) {
	args := strings.Join(hostKeySSHArgs("studio-services", ed25519HostKey(0xAA), "/tmp/kh"), " ")
	for _, want := range []string{
		"HostKeyAlias=samcloud-studio-services",
		"StrictHostKeyChecking=yes",
		"UserKnownHostsFile=/tmp/kh",
		"HashKnownHosts=no",
	} {
		if !strings.Contains(args, want) {
			t.Errorf("missing %q in %q", want, args)
		}
	}
	// The old behaviour must be gone.
	if strings.Contains(args, "StrictHostKeyChecking=no") ||
		strings.Contains(args, "/dev/null") {
		t.Fatalf("hop still skips host-key checks: %q", args)
	}
}

func TestHostKeySSHArgsLearnWhenNothingIsPinned(t *testing.T) {
	// Enrolling a device must still need no person in the loop.
	args := strings.Join(hostKeySSHArgs("newdev", "", "/tmp/kh"), " ")
	if !strings.Contains(args, "StrictHostKeyChecking=accept-new") {
		t.Fatalf("first hop is not accept-new: %q", args)
	}
}

func TestWriteKnownHostsPinnedAndLearning(t *testing.T) {
	key := ed25519HostKey(0xAA)

	path, remove, err := writeKnownHosts("studio-services", key)
	if err != nil {
		t.Fatalf("writeKnownHosts: %v", err)
	}
	content, _ := os.ReadFile(path)
	if string(content) != "samcloud-studio-services "+key+"\n" {
		t.Fatalf("pinned file = %q", content)
	}
	remove()
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("known_hosts file outlived the hop")
	}

	// Learning: an empty file for ssh to append to.
	path, remove, err = writeKnownHosts("newdev", "")
	if err != nil {
		t.Fatalf("writeKnownHosts (learn): %v", err)
	}
	defer remove()
	if content, _ := os.ReadFile(path); len(content) != 0 {
		t.Fatalf("learn file not empty: %q", content)
	}
}

func TestWriteKnownHostsRefusesAnUnreadablePin(t *testing.T) {
	// A pin we cannot write is a pin we cannot check; execSSH turns this into a
	// refusal rather than an unverified hop.
	if _, _, err := writeKnownHosts("dev", "ssh-ed25519 not!base64!"); err == nil {
		t.Fatal("accepted an unreadable pinned key")
	}
}

func TestHostKeyRefusedRecognisesSSHsRefusals(t *testing.T) {
	// The real text ssh emits, captured from openssh in both shapes.
	changed := `@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @
Host key for samcloud-dev has changed and you have requested strict checking.
Host key verification failed.`
	if !hostKeyRefused(changed) {
		t.Fatal("missed a changed host key")
	}
	if !hostKeyRefused("Host key verification failed.\n") {
		t.Fatal("missed a plain verification failure")
	}
	// Must not fire on unrelated failures, or every broken hop reads as an attack.
	for _, other := range []string{
		"ssh: connect to host localhost port 10006: Connection refused\n",
		"agent@localhost: Permission denied (publickey).\n",
		"kex_exchange_identification: Connection reset by peer\n",
		"",
	} {
		if hostKeyRefused(other) {
			t.Errorf("false positive on %q", other)
		}
	}
}

func TestStderrSnifferPassesThroughAndStillDetects(t *testing.T) {
	// stderr must reach the caller untouched; the copy is only for classifying.
	f, err := os.CreateTemp("", "sniff-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.Remove(f.Name())
	s := &stderrSniffer{out: f}

	// Split the marker across writes — ssh's banner arrives in pieces.
	s.Write([]byte("Host key verif"))
	s.Write([]byte("ication failed.\n"))
	if !s.sawHostKeyRefusal() {
		t.Fatal("marker split across writes was missed")
	}
	f.Close()
	content, _ := os.ReadFile(f.Name())
	if string(content) != "Host key verification failed.\n" {
		t.Fatalf("stderr was not passed through verbatim: %q", content)
	}
}

func TestStderrSnifferIsBounded(t *testing.T) {
	devNull, _ := os.OpenFile(os.DevNull, os.O_WRONLY, 0)
	defer devNull.Close()
	s := &stderrSniffer{out: devNull}
	for i := 0; i < 100; i++ {
		s.Write(make([]byte, 1024))
	}
	if len(s.seen) > snifferCap {
		t.Fatalf("sniffer grew to %d bytes", len(s.seen))
	}
}

func TestNewHopCarriesThePinnedKey(t *testing.T) {
	key := ed25519HostKey(0xAA)
	padded := "  " + key + "  "
	dev := &Device{ID: "studio-services", HostPubkey: &padded}
	h := newHop("alice", "alices-laptop", "studio-services", dev)
	if h.pinnedKey != key {
		t.Fatalf("pinnedKey = %q", h.pinnedKey)
	}

	// No pin yet → the learn path, not a broken hop.
	if h := newHop("alice", "alices-laptop", "newdev", &Device{ID: "newdev"}); h.pinnedKey != "" {
		t.Fatalf("expected no pin, got %q", h.pinnedKey)
	}
	if h := newHop("alice", "alices-laptop", "newdev", nil); h.pinnedKey != "" {
		t.Fatal("nil device produced a pin")
	}
}

func TestOfferedFingerprintFromRealSSHOutput(t *testing.T) {
	// Captured verbatim from openssh refusing a changed key.
	stderr := `@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
The fingerprint for the ED25519 key sent by the remote host is
SHA256:duwHT/p1q/P49fsy0Oyj/8cHoItkiw1RKDgaGHfr5UI.
Please contact your system administrator.
Host key verification failed.`
	want := "SHA256:duwHT/p1q/P49fsy0Oyj/8cHoItkiw1RKDgaGHfr5UI"
	if got := offeredFingerprint(stderr); got != want {
		t.Fatalf("offeredFingerprint = %q, want %q", got, want)
	}
	// Absent, truncated or unrelated output yields "", and the caller falls back
	// to pointing at ssh's own message rather than printing a bare "SHA256:".
	for name, in := range map[string]string{
		"unrelated": "ssh: connect to host x port 22: Connection refused",
		"empty":     "",
		"no digest": "sent by the remote host is\nPlease contact your system administrator.",
	} {
		if got := offeredFingerprint(in); got != "" {
			t.Errorf("%s: got %q, want empty", name, got)
		}
	}
}

func TestOutboundSSHUserIsTheDevicesOwn(t *testing.T) {
	user := "agent"
	if got, ok := outboundSSHUser(&Device{ID: "box-1", OutboundSSHUser: &user}); !ok || got != "agent" {
		t.Fatalf("got %q, %v; want the device's own user", got, ok)
	}
}

func TestOutboundSSHUserHasNoDefault(t *testing.T) {
	empty := ""
	for _, dev := range []*Device{{ID: "box-1"}, {ID: "box-2", OutboundSSHUser: &empty}} {
		if got, ok := outboundSSHUser(dev); ok {
			t.Fatalf("%s: got %q; a device without a user must not get a guessed one", dev.ID, got)
		}
	}
}
