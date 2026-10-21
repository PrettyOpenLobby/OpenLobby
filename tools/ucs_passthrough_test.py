#!/usr/bin/env python3
"""ucscgi._passthrough must not FIN the console before the console closes.

The upstream finishes a reply at once and closes; a slow client must still get
every byte, and must not see end of stream while it is still connected. The
pre-fix splice fails the second check.

    python tools/ucs_passthrough_test.py
"""
import importlib.util, os, socket, sys, threading, time

os.environ["POL_UCS_PASSTHROUGH_LINGER"] = "3"
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "ucscgi_under_test", os.path.join(os.path.dirname(HERE), "services", "ucscgi.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.log = lambda *a, **k: None

PAYLOAD = os.urandom(200_000)

up_srv = socket.socket(); up_srv.bind(("127.0.0.1", 0)); up_srv.listen(1)
def upstream():
    c, _ = up_srv.accept()
    c.recv(100)                       # the request
    c.sendall(PAYLOAD)
    c.close()                         # sslterm closes the moment it is done
threading.Thread(target=upstream, daemon=True).start()

front = socket.socket(); front.bind(("127.0.0.1", 0)); front.listen(1)
def frontend():
    conn, _ = front.accept()
    m._passthrough(conn, "test", "127.0.0.1", up_srv.getsockname()[1])
threading.Thread(target=frontend, daemon=True).start()

cl = socket.create_connection(front.getsockname())
cl.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
cl.sendall(b"GET /x HTTP/1.1\r\n\r\n")
got = b""
while len(got) < len(PAYLOAD):
    time.sleep(0.01)                  # a slow reader, like the Viewer
    d = cl.recv(4096)
    assert d, "EOF after %d of %d bytes" % (len(got), len(PAYLOAD))
    got += d
assert got == PAYLOAD, "payload differs"
cl.settimeout(1.0)
try:
    d = cl.recv(1)
    early_fin = (d == b"")
except socket.timeout:
    early_fin = False
print("all %d bytes received; FIN while client still open: %s" % (len(got), early_fin))
assert not early_fin, "server sent FIN before the console closed"
cl.close()
print("linger: PASS")


# Pacing (TLS route): a reply that follows a pause must still be spread out.
# The Viewer decrypts into 64 KiB and writes it to disk as it goes; a burst
# fills it and the download is abandoned. The upstream waits first, the way
# the terminator does while the portal fetches the file, so a pacer that
# banks the idle time fails here as surely as no pacing at all.
def paced_transfer(pace):
    payload = os.urandom(200_000)
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)

    def slow_upstream():
        c, _ = srv.accept()
        c.recv(100)
        time.sleep(0.6)
        c.sendall(payload)
        c.close()
    threading.Thread(target=slow_upstream, daemon=True).start()
    fr = socket.socket(); fr.bind(("127.0.0.1", 0)); fr.listen(1)

    def front():
        conn, _ = fr.accept()
        m._passthrough(conn, "test", "127.0.0.1", srv.getsockname()[1], pace=pace)
    threading.Thread(target=front, daemon=True).start()
    c = socket.create_connection(fr.getsockname())
    c.sendall(b"GET /x HTTP/1.1\r\n\r\n")
    got, first = b"", None
    while len(got) < len(payload):
        d = c.recv(65536)
        assert d, "EOF after %d bytes" % len(got)
        if first is None:
            first = time.monotonic()
        got += d
    spread = time.monotonic() - first
    c.close()
    assert got == payload
    return spread


rate = m._TLS_CHUNK / m._TLS_CHUNK_DELAY
burst = paced_transfer(False)
paced = paced_transfer(True)
print("unpaced spread %.3fs, paced spread %.3fs (rate %.0f B/s, expect >= %.2fs)"
      % (burst, paced, rate, 0.75 * 200_000 / rate))
assert burst < 0.2, "control: the unpaced transfer should arrive as a burst"
assert paced >= 0.75 * 200_000 / rate, "paced transfer arrived too fast"
print("PASS")
