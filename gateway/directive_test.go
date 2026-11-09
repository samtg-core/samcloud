package main

import "testing"

func TestSplitDirective(t *testing.T) {
	cases := []struct {
		in, directive, remote string
		hadDelim              bool
	}{
		{"lagoon -- uptime", "lagoon", "uptime", true},
		{"lagoon -- echo a -- b", "lagoon", "echo a -- b", true},
		{"lagoon --", "lagoon", "", true},
		{"lagoon", "lagoon", "", false},
		{"devices --json", "devices --json", "", false},
		{"devices --json -- x", "devices --json", "x", true},
		{"lagoon --\npython3 -\nprint(1)\n", "lagoon", "python3 -\nprint(1)", true},
		{"lagoon\t--\tuptime", "lagoon", "uptime", true},
		{"lagoon --\n", "lagoon", "", true},
	}
	for _, c := range cases {
		d, r, h := splitDirective(c.in)
		if d != c.directive || r != c.remote || h != c.hadDelim {
			t.Errorf("splitDirective(%q) = (%q, %q, %v), want (%q, %q, %v)",
				c.in, d, r, h, c.directive, c.remote, c.hadDelim)
		}
	}
}
