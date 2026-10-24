#!/usr/bin/env python3
"""The band port must send the Xbox 360's SSLv2-format ClientHello to sslterm.

The 360 Viewer's wallpaper download opens 51305 with an SSLv2-compatible hello
(`80 28 01 03 00 ...`, captured from the operator's 360 on 2026-10-24) where the
PS2 sends an SSL 3.0 record (`16 03 00 ...`). The old sniffer took only `16 03`,
so the 360 went to the plain portal passthrough and its download closed at
once. Here the real serve() front door runs against a fake terminator and a
fake portal; each opener must reach the right one. The pre-fix sniffer fails
the 360 case.

    python tools/ucs_sslv2_hello_test.py
"""
import importlib.util, os, socket, sys, threading, time

os.environ["POL_UCS_PASSTHROUGH_LINGER"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "ucscgi_under_test", os.path.join(os.path.dirname(HERE), "services", "ucscgi.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.log = lambda *a, **k: None
m.save_capture = lambda *a, **k: None
m.tls_context = lambda: None
m.load_config = lambda: {}

#: The 360's opener, byte for byte from wp360b.pcap (2026-10-24 14:18:40Z).
X360_HELLO = bytes.fromhex(
    "8028010300000f0000001000000400000501008000000302008"
    "06b49abdbffc96d6a46035a1ac9db0cc6")
#: A PS2-shaped SSL 3.0 record hello: header, ClientHello, version 3.0, random,
#: no session id, RC4_128_MD5, null compression.
PS2_HELLO = (bytes.fromhex("160300002d" "01000029" "0300") + bytes(32)
             + bytes.fromhex("00" "0002" "0004" "0100"))
HTTP_GET = b"GET /pml/main/index.pml HTTP/1.0\r\nHost: wh000.pol.com\r\n\r\n"

assert len(X360_HELLO) == 42, len(X360_HELLO)

# The classifier on its own.
assert m._is_tls_hello(X360_HELLO[:4]), "360 SSLv2 hello not recognised"
assert m._is_tls_hello(PS2_HELLO[:4]), "PS2 SSL3 hello not recognised"
assert not m._is_tls_hello(HTTP_GET[:4]), "HTTP taken for TLS"
assert not m._is_tls_hello(b""), "empty taken for TLS"
assert not m._is_tls_hello(b"\x80\x28\x01"), "3-byte SSLv2 prefix should wait for byte 4"
assert not m._is_tls_hello(b"\x80\x28\x01\x00"), "SSL 2.0-only hello cannot be served by sslterm"

desc = m.describe_hello(X360_HELLO)
print("360:", desc)
assert "SSLv2 format" in desc and "client_ver=3.0" in desc and "RC4_128_MD5" in desc, desc
assert "TRUNCATED" not in desc, desc
print("PS2:", m.describe_hello(PS2_HELLO))
print("classifier: PASS")


def sink():
    """A listener that records what each connection sent."""
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(8)
    got = []

    def run():
        while True:
            c, _ = s.accept()
            c.settimeout(2)
            data = b""
            try:
                while True:
                    d = c.recv(4096)
                    if not d:
                        break
                    data += d
            except OSError:
                pass
            got.append(data)
            c.close()
    threading.Thread(target=run, daemon=True).start()
    return s.getsockname()[1], got


term_port, term_got = sink()
portal_port, portal_got = sink()
os.environ["POL_UCS_SSL3_HOST"] = "127.0.0.1"
os.environ["POL_UCS_SSL3_PORT"] = str(term_port)
os.environ["POL_UCS_PASSTHROUGH_IP"] = "127.0.0.1"
os.environ["POL_UCS_PASSTHROUGH_PORT"] = str(portal_port)

probe = socket.socket(); probe.bind(("127.0.0.1", 0))
band_port = probe.getsockname()[1]; probe.close()
threading.Thread(target=m.serve, args=(band_port,), daemon=True).start()
for _ in range(50):
    try:
        socket.create_connection(("127.0.0.1", band_port), timeout=0.2).close()
        break
    except OSError:
        time.sleep(0.05)
portal_got.clear(); term_got.clear()   # the readiness probe sent nothing


def send(opener):
    c = socket.create_connection(("127.0.0.1", band_port))
    c.sendall(opener)
    time.sleep(0.3)
    c.close()
    time.sleep(1.5)


for name, opener, want_term in (("360 SSLv2 hello", X360_HELLO, True),
                                ("PS2 SSL3 hello", PS2_HELLO, True),
                                ("portal HTTP", HTTP_GET, False)):
    term_got.clear(); portal_got.clear()
    send(opener)
    to_term = opener in term_got
    to_portal = opener in portal_got
    print("%-16s -> terminator=%s portal=%s" % (name, to_term, to_portal))
    assert to_term == want_term and to_portal == (not want_term), name

print("routing: PASS")
