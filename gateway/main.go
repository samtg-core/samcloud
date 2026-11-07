// samcloud gateway — SSH ForceCommand shell
//
// Receives user+device identity from ForceCommand args,
// queries the registry for permissions, and presents
// available targets or routes directly to a destination.
//
// Usage in authorized_keys:
//   command="/usr/local/bin/samcloud-gateway alice alices-phone" ssh-ed25519 AAAA...
//
// Direct routing via SSH_ORIGINAL_COMMAND (SSH user is always "samcloud";
// identity rides in the per-key ForceCommand args, not the SSH username):
//   ssh samcloud box-1            → interactive shell on box-1 via its tunnel
//   ssh samcloud "box-1 -- uname -a"  → run a one-off command on box-1, capture
//                                       stdout, propagate the remote exit code
//   ssh samcloud devices [--json] → list accessible targets (machine-readable)
//   ssh samcloud status  [--json] → registry status
//   ssh samcloud shell            → drops to admin bash (if permitted)
//   ssh samcloud                  → interactive menu
//
// Output contract: gateway chatter goes to stderr; a remote command's own
// output is the only thing on stdout — so agents can capture it cleanly.
//
// Exit-code contract (non-interactive only): a remote command's own exit code
// is propagated verbatim. Gateway-level failures use shell-style codes so
// callers can branch reliably: 127 unknown target, 126 permission denied,
// 125 misconfigured target, 2 usage error (e.g. "<dev> --" with no command).

package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"time"
)

var registryURL = "http://localhost:7777"
var gatewayKeyPath = "/var/lib/samcloud/gateway"
var gatewayToken = ""

const requestTimeout = 5 * time.Second

func init() {
	if env := os.Getenv("REGISTRY_URL"); env != "" {
		registryURL = env
	}
	if env := os.Getenv("GATEWAY_KEY"); env != "" {
		gatewayKeyPath = env
	}
	if env := os.Getenv("GATEWAY_TOKEN"); env != "" {
		gatewayToken = env
	}
}

// Registry types

type Device struct {
	ID              string  `json:"id"`
	Name            string  `json:"name"`
	Type            string  `json:"type"`
	OutboundMethod  *string `json:"outbound_method"`
	OutboundIP      *string `json:"outbound_ip"`
	OutboundSSHUser *string `json:"outbound_ssh_user"`
	OutboundSSHPort int     `json:"outbound_ssh_port"`
	TunnelPort      *int    `json:"tunnel_port"`
	Status          string  `json:"status"`
	HostPubkey      *string `json:"host_pubkey"`
}

type GroupMembership struct {
	GroupID   string `json:"group_id"`
	GroupName string `json:"name"`
	Role      string `json:"role"`
}

type User struct {
	ID       int    `json:"id"`
	Username string `json:"username"`
	Role     string `json:"role"`
	Scopes   string `json:"scopes"`
}

type AuditEntry struct {
	Actor  string `json:"actor"`
	Action string `json:"action"`
	Target string `json:"target"`
}

func main() {
	if len(os.Args) < 3 {
		fmt.Fprintln(os.Stderr, "Usage: samcloud-gateway <username> <device>")
		fmt.Fprintln(os.Stderr, "  (normally invoked via ForceCommand in authorized_keys)")
		os.Exit(1)
	}

	username := os.Args[1]
	device := os.Args[2]

	// Log connection
	audit(username, device, "gateway.connect")

	// Check for direct command (SSH_ORIGINAL_COMMAND) — non-interactive path
	originalCmd := os.Getenv("SSH_ORIGINAL_COMMAND")
	if originalCmd != "" {
		handleCommand(username, device, originalCmd, false)
		return
	}

	// Interactive mode
	interactive(username, device)
}

// handleCommand dispatches one directive. interactiveMode is true when called
// from the menu loop (failures print and return to the menu) and false for a
// non-interactive SSH_ORIGINAL_COMMAND (failures exit the process non-zero, so
// `ssh samcloud "x -- cmd" && next` is honest about routing failures).
func handleCommand(username, device, cmd string, interactiveMode bool) {
	// Split an optional remote command off the gateway directive. The first
	// standalone " -- " is the delimiter; everything after it is run verbatim
	// on the target. Form: "<target|subcmd> [flags] [-- <remote command...>]"
	var remoteCmd string
	hadDelim := false
	if idx := strings.Index(cmd, " -- "); idx >= 0 {
		remoteCmd = strings.TrimSpace(cmd[idx+4:])
		cmd = strings.TrimSpace(cmd[:idx])
		hadDelim = true
	} else if strings.HasSuffix(cmd, " --") {
		cmd = strings.TrimSpace(strings.TrimSuffix(cmd, " --"))
		hadDelim = true
	}

	parts := strings.Fields(cmd)
	if len(parts) == 0 {
		interactive(username, device)
		return
	}

	// A delimiter with no command behind it is a usage error — never silently
	// fall through to an interactive shell (which hangs a scripted caller).
	if hadDelim && remoteCmd == "" {
		fmt.Fprintln(os.Stderr, "gateway: '--' given but no command followed (usage: <device> -- <command>)")
		if !interactiveMode {
			os.Exit(2)
		}
		return
	}

	target := parts[0]
	flags := parts[1:]
	jsonOut := hasFlag(flags, "--json")

	switch target {
	case "shell", "bash":
		if !isAdmin(username) {
			fmt.Fprintln(os.Stderr, "Permission denied: admin access required")
			audit(username, device, "gateway.shell.denied")
			os.Exit(1)
		}
		audit(username, device, "gateway.shell")
		dropToShell()

	case "status":
		showStatus(username, jsonOut)

	case "devices", "ls":
		showTargets(username, jsonOut)

	case "help":
		showHelp(username, device)

	default:
		// Try to route to a device (optionally running a one-off command)
		routeTo(username, device, target, remoteCmd, interactiveMode)
	}
}

func hasFlag(args []string, flag string) bool {
	for _, a := range args {
		if a == flag {
			return true
		}
	}
	return false
}

func interactive(username, device string) {
	fmt.Printf("\n  samcloud gateway\n")
	fmt.Printf("  %s@%s\n\n", username, device)

	showTargets(username, false)

	if isAdmin(username) {
		fmt.Println("  Commands: <device> [-- <cmd>], devices, shell, status, help, exit")
	} else {
		fmt.Println("  Commands: <device> [-- <cmd>], devices, status, help, exit")
	}

	// Interactive loop
	reader := make([]byte, 1024)
	for {
		fmt.Printf("\n  > ")
		n, err := os.Stdin.Read(reader)
		if err != nil {
			return
		}
		input := strings.TrimSpace(string(reader[:n]))
		if input == "" {
			continue
		}
		if input == "exit" || input == "quit" || input == "q" {
			fmt.Println("  Goodbye.")
			return
		}
		handleCommand(username, device, input, true)
	}
}

func showTargets(username string, jsonOut bool) {
	devices := getAccessibleDevices(username)

	if jsonOut {
		type targetOut struct {
			ID     string `json:"id"`
			Method string `json:"method"`
			Status string `json:"status"`
			Detail string `json:"detail,omitempty"`
		}
		out := []targetOut{}
		for _, d := range devices {
			method := ""
			if d.OutboundMethod != nil {
				method = *d.OutboundMethod
			}
			detail := ""
			if d.OutboundIP != nil {
				detail = *d.OutboundIP
			}
			if d.TunnelPort != nil {
				detail = fmt.Sprintf(":%d", *d.TunnelPort)
			}
			out = append(out, targetOut{ID: d.ID, Method: method, Status: d.Status, Detail: detail})
		}
		b, _ := json.Marshal(out)
		fmt.Println(string(b))
		return
	}

	if len(devices) == 0 {
		fmt.Println("  No accessible devices.")
	} else {
		fmt.Println("  Targets:")
		for _, d := range devices {
			method := "--"
			if d.OutboundMethod != nil {
				method = *d.OutboundMethod
			}
			icon := "○"
			if d.Status == "online" {
				icon = "●"
			}
			detail := ""
			if d.OutboundIP != nil {
				detail = *d.OutboundIP
			}
			if d.TunnelPort != nil {
				detail = fmt.Sprintf(":%d", *d.TunnelPort)
			}
			fmt.Printf("    %s %-16s %-15s %s\n", icon, d.ID, method, detail)
		}
	}
}

func routeTo(username, device, target, remoteCmd string, interactiveMode bool) {
	// fail reports a gateway-level routing/validation failure. Non-interactively
	// it exits the process with a shell-style code so callers can branch on it
	// (127 unknown target, 126 permission denied, 125 misconfigured target);
	// interactively it just returns to the menu.
	fail := func(code int) {
		if !interactiveMode {
			os.Exit(code)
		}
	}

	// Look up target device
	dev := getDevice(target)
	if dev == nil {
		fmt.Fprintf(os.Stderr, "Unknown target: %s\n", target)
		fail(127)
		return
	}

	// Check permission. Audit the denial — and distinguish a denied one-off exec
	// from a denied interactive route, since attempted exec-on-deny is the
	// security-relevant event to capture (#90 review).
	if !canAccess(username, target) {
		fmt.Fprintf(os.Stderr, "Permission denied: no access to %s\n", target)
		deniedAction := "gateway.route.denied"
		if remoteCmd != "" {
			deniedAction = "gateway.exec.denied"
		}
		audit(username, device, deniedAction)
		fail(126)
		return
	}

	// Distinguish a one-off command run from an interactive route in the audit log
	if remoteCmd != "" {
		audit(username, device, "gateway.exec."+target)
	} else {
		audit(username, device, "gateway.route."+target)
	}

	// Determine how to connect
	if dev.OutboundMethod == nil {
		fmt.Fprintf(os.Stderr, "Device %s has no outbound access configured\n", target)
		fail(125)
		return
	}

	switch *dev.OutboundMethod {
	case "direct-ssh":
		if dev.OutboundIP == nil {
			fmt.Fprintln(os.Stderr, "No IP configured for direct SSH")
			fail(125)
			return
		}
		sshUser, ok := outboundSSHUser(dev)
		if !ok {
			fmt.Fprintf(os.Stderr, "Device %s has no SSH user: an admin sets outbound_ssh_user on it (PATCH /api/v1/devices/%s)\n", target, target)
			fail(125)
			return
		}
		port := fmt.Sprintf("%d", dev.OutboundSSHPort)
		// Gateway chatter → stderr so stdout stays clean for captured command output
		fmt.Fprintf(os.Stderr, "Connecting to %s (%s@%s:%s)...\n", target, sshUser, *dev.OutboundIP, port)
		execSSH(newHop(username, device, target, dev), sshUser, *dev.OutboundIP, port, remoteCmd)

	case "reverse-tunnel":
		if dev.TunnelPort == nil {
			fmt.Fprintf(os.Stderr, "No tunnel port configured for %s\n", target)
			fail(125)
			return
		}
		sshUser, ok := outboundSSHUser(dev)
		if !ok {
			fmt.Fprintf(os.Stderr, "Device %s has no SSH user: an admin sets outbound_ssh_user on it (PATCH /api/v1/devices/%s)\n", target, target)
			fail(125)
			return
		}
		port := fmt.Sprintf("%d", *dev.TunnelPort)
		fmt.Fprintf(os.Stderr, "Connecting to %s via tunnel :%s...\n", target, port)
		execSSH(newHop(username, device, target, dev), sshUser, "localhost", port, remoteCmd)

	default:
		fmt.Fprintf(os.Stderr, "Unsupported outbound method: %s\n", *dev.OutboundMethod)
		fail(125)
	}
}

// outboundSSHUser is the account the gateway logs in as on a device. The
// device's record names it (outbound_ssh_user, from enrolment or from the
// device's own whoami at activation). There is no default account to guess.
func outboundSSHUser(dev *Device) (string, bool) {
	if dev.OutboundSSHUser == nil || *dev.OutboundSSHUser == "" {
		return "", false
	}
	return *dev.OutboundSSHUser, true
}

// hop is who is going where, for one connection: enough to verify the target's
// host key and to audit what happened.
type hop struct {
	username  string // the identity making the hop
	from      string // the device its key is bound to (audit only)
	deviceID  string // the target device id — what the host key is pinned against
	pinnedKey string // the pinned host key; empty when nothing is pinned yet
}

func newHop(username, from, deviceID string, dev *Device) hop {
	h := hop{username: username, from: from, deviceID: deviceID}
	if dev != nil && dev.HostPubkey != nil {
		h.pinnedKey = strings.TrimSpace(*dev.HostPubkey)
	}
	return h
}

// execSSH connects to a target. With remoteCmd empty it opens an interactive
// shell (forced PTY) and returns to the caller's menu loop when the session
// ends. With remoteCmd set it runs that single command non-interactively (no
// PTY), streams the output, and exits the gateway process with the remote
// command's exit code — the form agents/automation use.
//
// The target's host key is verified against the pin the registry holds for that
// device. A matching key is silent. A changed key is refused by ssh, and the
// gateway then says which device and which fingerprints, because ssh's own
// banner names a temp file and tells you to contact an administrator.
func execSSH(h hop, user, host, port, remoteCmd string) {
	tty := "-tt"
	if remoteCmd != "" {
		tty = "-T"
	}

	knownHosts, removeKnownHosts, err := writeKnownHosts(h.deviceID, h.pinnedKey)
	if err != nil {
		// A pin we cannot write is a pin we cannot check — refuse rather than
		// fall back to trusting the target.
		fmt.Fprintf(os.Stderr, "samcloud-gateway: cannot verify host key for %s: %v\n", h.deviceID, err)
		audit(h.username, h.from, "gateway.hostkey.unverifiable."+h.deviceID)
		if remoteCmd != "" {
			os.Exit(255)
		}
		return
	}
	defer removeKnownHosts()
	learning := h.pinnedKey == ""

	args := []string{tty}
	args = append(args, hostKeySSHArgs(h.deviceID, h.pinnedKey, knownHosts)...)
	args = append(args, "-p", port)
	// A seat whose authorized_keys does not carry THIS gateway's key must be a
	// denial, not a hang. Without BatchMode ssh falls through to
	// keyboard-interactive and waits for a password that no one can type: on
	// 2026-09-20 a seat born from scripts/new-seat.sh (which installed a stale
	// copy of the gateway pubkey) answered every `ssh samcloud "<seat> -- cmd"`
	// with silence and an ssh process that never exited — three of them were
	// still resident in the container when the cause was found. Only for a
	// one-off command: an interactive route keeps the prompt.
	if remoteCmd != "" {
		args = append(args, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15")
	}
	if _, err := os.Stat(gatewayKeyPath); err == nil {
		args = append(args, "-i", gatewayKeyPath)
	}
	args = append(args, fmt.Sprintf("%s@%s", user, host))
	if remoteCmd != "" {
		args = append(args, remoteCmd)
	}

	// Pass stderr through while keeping a bounded copy, so a refused host key can
	// be told apart from any other transport failure — ssh exits 255 for both.
	sniffer := &stderrSniffer{out: os.Stderr}

	cmd := exec.Command("ssh", args...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = sniffer
	runErr := cmd.Run()

	switch {
	case learning:
		// First contact: pin what it presented, so the next hop is checked. Not
		// gated on runErr — the host key is learned during the handshake, so
		// `<newdev> -- false` must still pin.
		h.recordLearnedKey(knownHosts)
	case sniffer.sawHostKeyRefusal():
		h.reportMismatch(sniffer.offeredFingerprint())
	}

	if remoteCmd != "" {
		// Non-interactive: propagate the remote command's exit status. Remove the
		// per-hop known_hosts file first — os.Exit does not run deferred calls.
		code := 0
		if exitErr, ok := runErr.(*exec.ExitError); ok {
			code = exitErr.ExitCode()
		} else if runErr != nil {
			fmt.Fprintf(os.Stderr, "gateway: ssh transport error: %v\n", runErr)
			code = 255
		}
		removeKnownHosts()
		os.Exit(code)
	}
	// Interactive: returns to caller (menu loop) when session ends
}

// recordLearnedKey pins the key ssh just accepted on first contact.
//
// The registry records it only if nothing is pinned yet, so two hops learning at
// once cannot overwrite each other, and a later hop can never quietly replace a
// pin that already exists.
func (h hop) recordLearnedKey(knownHostsPath string) {
	content, err := os.ReadFile(knownHostsPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "samcloud-gateway: could not read back the host key for %s: %v\n", h.deviceID, err)
		return
	}
	if strings.TrimSpace(string(content)) == "" {
		// The hop never got as far as a host key (host down, wrong port, refused
		// connection). Nothing was learned and nothing is wrong with the pin.
		return
	}
	key, ok := parseKnownHostsLine(string(content), h.deviceID)
	if !ok {
		fmt.Fprintf(os.Stderr, "samcloud-gateway: could not read back the host key for %s\n", h.deviceID)
		return
	}

	body, _ := json.Marshal(map[string]string{"host_pubkey": key})
	status, err := registryPostStatus("/api/v1/devices/"+url.PathEscape(h.deviceID)+"/host-key", string(body))
	switch {
	case err != nil:
		fmt.Fprintf(os.Stderr, "samcloud-gateway: pinned nothing for %s (registry unreachable: %v) — the next hop will try again\n", h.deviceID, err)
	case status == 200:
		fmt.Fprintf(os.Stderr, "samcloud-gateway: host key for %s pinned (%s)\n", h.deviceID, sshFingerprint(key))
		audit(h.username, h.from, "gateway.hostkey.pinned."+h.deviceID)
	case status == 409:
		// Something pinned a different key between our lookup and now.
		fmt.Fprintf(os.Stderr, "samcloud-gateway: host key mismatch for %s — it was pinned to a different key while this hop ran\n", h.deviceID)
		fmt.Fprintf(os.Stderr, "samcloud-gateway:   offered %s\n", sshFingerprint(key))
		audit(h.username, h.from, "gateway.hostkey.conflict."+h.deviceID)
	default:
		fmt.Fprintf(os.Stderr, "samcloud-gateway: pinned nothing for %s (registry said %d) — the next hop will try again\n", h.deviceID, status)
	}
}

// reportMismatch says what ssh's banner does not: which device, which key
// samcloud trusts, and the one call that accepts a re-key.
func (h hop) reportMismatch(offered string) {
	fmt.Fprintf(os.Stderr, "samcloud-gateway: host key mismatch for %s — refused\n", h.deviceID)
	fmt.Fprintf(os.Stderr, "samcloud-gateway:   samcloud trusts %s\n", sshFingerprint(h.pinnedKey))
	if offered != "" {
		fmt.Fprintf(os.Stderr, "samcloud-gateway:   the host offered  %s\n", offered)
	} else {
		fmt.Fprintf(os.Stderr, "samcloud-gateway:   the host offered the key in ssh's message above\n")
	}
	fmt.Fprintf(os.Stderr, "samcloud-gateway:   if %s legitimately re-keyed: DELETE /api/v1/devices/%s/host-key, then hop again to pin the new key\n", h.deviceID, h.deviceID)
	audit(h.username, h.from, "gateway.hostkey.mismatch."+h.deviceID)
}

func dropToShell() {
	shell := os.Getenv("SHELL")
	if shell == "" {
		shell = "/bin/bash"
	}
	shellPath, _ := exec.LookPath(shell)
	syscall.Exec(shellPath, []string{shell}, os.Environ())
}

func showStatus(username string, jsonOut bool) {
	resp, err := registryGet("/api/v1/status")
	if err != nil {
		fmt.Fprintf(os.Stderr, "Registry unreachable: %v\n", err)
		return
	}

	var status struct {
		Counts struct {
			Devices     int `json:"devices"`
			Services    int `json:"services"`
			Ports       int `json:"ports"`
			OpenTickets int `json:"open_tickets"`
		} `json:"counts"`
	}
	json.Unmarshal(resp, &status)

	if jsonOut {
		b, _ := json.Marshal(status.Counts)
		fmt.Println(string(b))
		return
	}

	fmt.Printf("\n  System Status\n")
	fmt.Printf("  Devices:  %d\n", status.Counts.Devices)
	fmt.Printf("  Services: %d\n", status.Counts.Services)
	fmt.Printf("  Ports:    %d\n", status.Counts.Ports)
	fmt.Printf("  Tickets:  %d open\n\n", status.Counts.OpenTickets)
}

func showHelp(username, device string) {
	fmt.Println("\n  samcloud gateway commands:")
	fmt.Println("    <device-id>          Open an interactive shell on a device (e.g. 'box-1')")
	fmt.Println("    <device-id> -- <cmd> Run one command on the device, capture stdout,")
	fmt.Println("                         propagate its exit code (non-interactive)")
	fmt.Println("    devices [--json]     List accessible targets")
	fmt.Println("    status  [--json]     Show system status")
	fmt.Println("    help                 Show this help")
	if isAdmin(username) {
		fmt.Println("    shell                Drop to admin bash shell")
	}
	fmt.Println()
	fmt.Println("  Non-interactive form (for agents/scripts):")
	fmt.Println("    ssh samcloud \"<device-id> -- <command>\"")
	fmt.Println()
}

// Registry helpers

func registryGet(path string) ([]byte, error) {
	client := &http.Client{Timeout: requestTimeout}
	req, err := http.NewRequest("GET", registryURL+path, nil)
	if err != nil {
		return nil, err
	}
	if gatewayToken != "" {
		req.Header.Set("Authorization", "Bearer "+gatewayToken)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return io.ReadAll(resp.Body)
}

// registryPostStatus posts and reports the HTTP status, so a caller can tell a
// refusal (409, 403) from a success. registryPost ignores it, which is right for
// audit but not for pinning a key.
func registryPostStatus(path string, body string) (int, error) {
	client := &http.Client{Timeout: requestTimeout}
	req, err := http.NewRequest("POST", registryURL+path, strings.NewReader(body))
	if err != nil {
		return 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	if gatewayToken != "" {
		req.Header.Set("Authorization", "Bearer "+gatewayToken)
	}
	resp, err := client.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	return resp.StatusCode, nil
}

func registryPost(path string, body string) error {
	client := &http.Client{Timeout: requestTimeout}
	req, err := http.NewRequest("POST", registryURL+path, strings.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if gatewayToken != "" {
		req.Header.Set("Authorization", "Bearer "+gatewayToken)
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	return nil
}

func getDevice(id string) *Device {
	data, err := registryGet("/api/v1/devices/" + id)
	if err != nil {
		return nil
	}
	var dev Device
	if json.Unmarshal(data, &dev) != nil {
		return nil
	}
	if dev.ID == "" {
		return nil
	}
	return &dev
}

func getAccessibleDevices(username string) []Device {
	// The registry answers with the devices this user can enter — the same
	// predicate as can-access (stored scopes, admin role, group membership),
	// so the listing is a capability list, not a fleet inventory (scope v1
	// train 0; before it this fetched /devices with the gateway's own token).
	data, err := registryGet("/api/v1/auth/accessible-devices?username=" + url.QueryEscape(username))
	if err != nil {
		return nil
	}
	var resp struct {
		Devices []Device `json:"devices"`
	}
	json.Unmarshal(data, &resp)

	// Filter to devices with outbound access (can be connected to)
	var accessible []Device
	for _, d := range resp.Devices {
		if d.OutboundMethod != nil {
			accessible = append(accessible, d)
		}
	}
	return accessible
}

func isAdmin(username string) bool {
	data, err := registryGet("/api/v1/users")
	if err != nil {
		return false
	}
	var users []User
	json.Unmarshal(data, &users)
	for _, u := range users {
		if u.Username == username && u.Role == "admin" {
			return true
		}
	}
	return false
}

func canAccess(username, targetDevice string) bool {
	// Query registry for group-resolved access check
	path := fmt.Sprintf("/api/v1/auth/can-access?username=%s&device_id=%s", url.QueryEscape(username), url.QueryEscape(targetDevice))
	data, err := registryGet(path)
	if err != nil {
		// Fall back to admin check if registry unreachable
		return isAdmin(username)
	}
	var result struct {
		Access bool   `json:"access"`
		Reason string `json:"reason"`
	}
	if err := json.Unmarshal(data, &result); err != nil {
		return isAdmin(username)
	}
	return result.Access
}

func audit(username, device, action string) {
	body := fmt.Sprintf(`{"actor":"user:%s","action":"%s","target":"%s","detail":"{\"device\":\"%s\"}"}`,
		username, action, username+"@"+device, device)
	registryPost("/api/v1/audit", body)
}
