// sslterm terminates the SSL 3.0 that the PlayOnline clients speak and relays
// the plaintext to an ordinary backend.
//
// The handshake is patapancakes/sslspoof (AGPL-3.0), built unmodified from
// upstream at a pinned commit (see Dockerfile). For every connection it mints a
// leaf for the configured hostname under a chain that ends in RSA Data
// Security's "Secure Server Certification Authority", one of the four roots in
// the stock cert.db, so an unmodified client accepts it:
//
//	leaf CN=<host>  <-  sslspoof CA (v1)  <-  www.icom.com (512-bit, factored)  <-  RSA Secure Server CA
//
// Routes come from SSLTERM_ROUTES, whitespace separated:
//
//	listen=host>upstream[,md5]
//	0.0.0.0:8443=ucs.pol.com>127.0.0.1:8080
//
// ",md5" signs the leaf with MD5 instead of SHA-1.
//
// sslspoof drops a failed handshake without saying why, and why is the only
// thing worth knowing when a client refuses us. Each accepted socket is wrapped
// so a handshake that dies is logged with what the client sent last: a
// ClientKeyExchange means it trusted the certificate, an alert record names the
// objection, and a bare close after our Certificate is the silent rejection the
// 2002-era stacks give.
package main

import (
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/patapancakes/sslspoof"
)

type route struct {
	listen   string
	host     string
	upstream string
	md5      bool
}

func parseRoutes(s string) ([]route, error) {
	var routes []route
	for _, f := range strings.Fields(s) {
		r := route{}
		listen, rest, ok := strings.Cut(f, "=")
		if !ok {
			return nil, fmt.Errorf("route %q: want listen=host>upstream", f)
		}
		host, upstream, ok := strings.Cut(rest, ">")
		if !ok {
			return nil, fmt.Errorf("route %q: want listen=host>upstream", f)
		}
		upstream, opts, _ := strings.Cut(upstream, ",")
		for _, o := range strings.Split(opts, ",") {
			switch o {
			case "":
			case "md5":
				r.md5 = true
			default:
				return nil, fmt.Errorf("route %q: unknown option %q", f, o)
			}
		}
		r.listen, r.host, r.upstream = listen, host, upstream
		routes = append(routes, r)
	}
	if len(routes) == 0 {
		return nil, errors.New("SSLTERM_ROUTES is empty")
	}
	return routes, nil
}

var connSeq atomic.Uint64

// watchedConn records the traffic of one socket until sslspoof either hands it
// back from Accept (handshake done) or closes it (handshake failed).
type watchedConn struct {
	net.Conn
	id       uint64
	route    route
	start    time.Time
	mu       sync.Mutex
	in, out  int
	lastIn   []byte
	accepted atomic.Bool
	closed   atomic.Bool
}

func (c *watchedConn) Read(b []byte) (int, error) {
	n, err := c.Conn.Read(b)
	if n > 0 && !c.accepted.Load() {
		c.mu.Lock()
		c.in += n
		c.lastIn = append(c.lastIn[:0], b[:min(n, 16)]...)
		c.mu.Unlock()
	}
	return n, err
}

func (c *watchedConn) Write(b []byte) (int, error) {
	n, err := c.Conn.Write(b)
	if !c.accepted.Load() {
		c.mu.Lock()
		c.out += n
		c.mu.Unlock()
	}
	return n, err
}

func (c *watchedConn) Close() error {
	if !c.accepted.Load() && !c.closed.Swap(true) {
		c.mu.Lock()
		log.Printf("#%d %s %s handshake FAILED after %v: read %d B, wrote %d B, last client bytes % x (%s)",
			c.id, c.route.host, c.RemoteAddr(), time.Since(c.start).Round(time.Millisecond),
			c.in, c.out, c.lastIn, describe(c.lastIn, c.out))
		c.mu.Unlock()
	}
	return c.Conn.Close()
}

var alertNames = map[byte]string{
	0: "close_notify", 10: "unexpected_message", 20: "bad_record_mac",
	30: "decompression_failure", 40: "handshake_failure", 41: "no_certificate",
	42: "bad_certificate", 43: "unsupported_certificate", 44: "certificate_revoked",
	45: "certificate_expired", 46: "certificate_unknown", 47: "illegal_parameter",
	48: "unknown_ca", 51: "decrypt_error", 70: "protocol_version",
}

// describe names the last record the client sent before the handshake died.
func describe(b []byte, wrote int) string {
	switch {
	case len(b) == 0:
		return "client sent nothing"
	case wrote == 0:
		return "died before ServerHello (not an SSLv3 ClientHello?)"
	case b[0] == 0x15 && len(b) >= 7:
		return fmt.Sprintf("client ALERT level %d %s", b[5], alertNames[b[6]])
	case b[0] == 0x16 && len(b) >= 6 && b[5] == 0x10:
		return "client sent ClientKeyExchange: it TRUSTED the certificate, failure is after that"
	case b[0] == 0x16 && len(b) >= 6 && b[5] == 0x01, b[0] == 0x80:
		return "client closed after our Certificate with no alert: silent rejection"
	default:
		return "unrecognised"
	}
}

// handshakeTimeout bounds one handshake. sslspoof runs the handshake inside
// Accept, one connection at a time, so a client that stalls would otherwise
// hold the whole listener.
const handshakeTimeout = 15 * time.Second

type watchingListener struct {
	net.Listener
	route route
}

func (l *watchingListener) Accept() (net.Conn, error) {
	c, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	c.SetDeadline(time.Now().Add(handshakeTimeout))
	w := &watchedConn{Conn: c, id: connSeq.Add(1), route: l.route, start: time.Now()}
	return w, nil
}

// handshook marks the socket behind an accepted sslspoof conn as done: its
// deadline is lifted and its Close no longer reports a failure.
func handshook(c net.Conn) uint64 {
	sc, ok := c.(*sslspoof.Conn)
	if !ok {
		return 0
	}
	w, ok := sc.Conn.(*watchedConn)
	if !ok {
		return 0
	}
	w.accepted.Store(true)
	w.Conn.SetDeadline(time.Time{})
	return w.id
}

func serve(r route) error {
	l, err := sslspoof.NewListener(r.listen, r.host, r.md5)
	if err != nil {
		return err
	}
	l.Listener = &watchingListener{Listener: l.Listener, route: r}
	sig := "sha1"
	if r.md5 {
		sig = "md5"
	}
	log.Printf("listening %s as CN=%s (%s leaf) -> %s", r.listen, r.host, sig, r.upstream)
	for {
		c, err := l.Accept()
		if err != nil {
			return err
		}
		go relay(c, r, handshook(c))
	}
}

func relay(c net.Conn, r route, id uint64) {
	log.Printf("#%d %s %s handshake OK -> %s", id, r.host, c.RemoteAddr(), r.upstream)
	up, err := net.DialTimeout("tcp", r.upstream, 10*time.Second)
	if err != nil {
		log.Printf("#%d %s %s upstream %s: %v", id, r.host, c.RemoteAddr(), r.upstream, err)
		c.Close()
		return
	}
	var wg sync.WaitGroup
	var toUp, toClient int64
	wg.Add(2)
	go func() {
		defer wg.Done()
		toUp, _ = io.Copy(up, c)
		if tc, ok := up.(*net.TCPConn); ok {
			tc.CloseWrite()
		}
	}()
	go func() {
		defer wg.Done()
		toClient, _ = io.Copy(c, up)
		c.Close()
	}()
	wg.Wait()
	up.Close()
	log.Printf("#%d %s %s closed: %d B to upstream, %d B to client", id, r.host, c.RemoteAddr(), toUp, toClient)
}

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC | log.Lmicroseconds)
	routes, err := parseRoutes(os.Getenv("SSLTERM_ROUTES"))
	if err != nil {
		log.Fatal(err)
	}
	errs := make(chan error, len(routes))
	for _, r := range routes {
		go func(r route) { errs <- fmt.Errorf("%s: %w", r.listen, serve(r)) }(r)
	}
	log.Fatal(<-errs)
}
