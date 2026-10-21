package main

import (
	"strings"
	"testing"
)

func TestParseRoutes(t *testing.T) {
	rs, err := parseRoutes(" 127.0.0.1:8444=userctl.pol.com>127.0.0.1:80\n 127.0.0.1:8445=ucs.pol.com>127.0.0.1:8080,md5 ")
	if err != nil {
		t.Fatal(err)
	}
	want := []route{
		{"127.0.0.1:8444", "userctl.pol.com", "127.0.0.1:80", false},
		{"127.0.0.1:8445", "ucs.pol.com", "127.0.0.1:8080", true},
	}
	if len(rs) != len(want) {
		t.Fatalf("got %d routes, want %d", len(rs), len(want))
	}
	for i := range want {
		if rs[i] != want[i] {
			t.Errorf("route %d = %+v, want %+v", i, rs[i], want[i])
		}
	}
	for _, bad := range []string{"", "127.0.0.1:1", "127.0.0.1:1=host", "a=b>c,sha256"} {
		if _, err := parseRoutes(bad); err == nil {
			t.Errorf("parseRoutes(%q) accepted a malformed route", bad)
		}
	}
}

// Each verdict is checked against the record that should produce it AND
// against the others, so a describe() that says "trusted" for everything fails.
func TestDescribe(t *testing.T) {
	hello := []byte{0x16, 0x03, 0x00, 0x00, 0x35, 0x01, 0x00, 0x00, 0x31, 0x03, 0x00}
	v2hello := []byte{0x80, 0x2e, 0x01, 0x03, 0x00}
	cke := []byte{0x16, 0x03, 0x00, 0x00, 0x84, 0x10, 0x00, 0x00, 0x80}
	alert := []byte{0x15, 0x03, 0x00, 0x00, 0x02, 0x02, 42}
	cases := []struct {
		name  string
		b     []byte
		wrote int
		want  string
	}{
		{"nothing", nil, 0, "sent nothing"},
		{"no server hello", hello, 0, "before ServerHello"},
		{"silent close v3", hello, 900, "silent rejection"},
		{"silent close v2", v2hello, 900, "silent rejection"},
		{"trusted", cke, 900, "TRUSTED"},
		{"alert", alert, 900, "bad_certificate"},
	}
	for _, c := range cases {
		got := describe(c.b, c.wrote)
		if !strings.Contains(got, c.want) {
			t.Errorf("%s: describe = %q, want it to mention %q", c.name, got, c.want)
		}
		for _, o := range cases {
			if o.want != c.want && strings.Contains(got, o.want) {
				t.Errorf("%s: describe = %q also claims %q", c.name, got, o.want)
			}
		}
	}
}
