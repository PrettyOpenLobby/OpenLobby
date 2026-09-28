#!/usr/bin/env python3
"""Exercise first-start patch replies over TCP, with empty/partial archives.

    python tests/test_polserver2_startup.py

The patch server runs as a subprocess on a free band of loopback ports. The
build each client announces is live state (clientbuilds.py), so the suite and
the server share a throwaway Valkey key prefix (tools/pgtest.py).
"""
import contextlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services'))
sys.path.insert(0, str(ROOT / 'tools'))
import pgtest  # noqa: E402
pgtest.use_fresh_valkey()
import clientbuilds  # noqa: E402
import polserver2  # noqa: E402
from polcore import kv  # noqa: E402
from slc import slc_compress, slc_decompress  # noqa: E402


def builds():
    """What the patch server recorded for this suite's client address."""
    return clientbuilds.for_address('127.0.0.1')


@contextlib.contextmanager
def running(root, **settings):
    # Reserve a free band for this test; never bind the deployed service ports.
    listeners = []
    for offset in range(-32000, -20000, 100):
        try:
            for port in [54000, *range(53001, 53016)]:
                sock = socket.socket()
                listeners.append(sock)
                sock.bind(('127.0.0.1', port + offset))
            break
        except OSError as error:
            if isinstance(error, PermissionError):
                raise
            for sock in listeners:
                sock.close()
            listeners = []
    else:
        raise RuntimeError('no free test port band')
    for sock in listeners:
        sock.close()
    env = {k: v for k, v in os.environ.items() if not k.startswith('POLP_')}
    env.update(POLP_CURRENT_ALL='1', POLP_CONSOLE_LIST_CAP='20130601_E',
               PYTHONUNBUFFERED='1')
    env.update(settings)
    with tempfile.TemporaryFile(mode='w+') as output:
        proc = subprocess.Popen([sys.executable, str(ROOT / 'services/polserver2.py'),
                                 str(root), '--advertise', '192.0.2.42', '--quiet',
                                 '--bind-offset', str(offset)],
                                env=env, stdout=output, stderr=output)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    output.seek(0)
                    raise AssertionError(output.read())
                try:
                    with socket.create_connection(('127.0.0.1', 54000 + offset), .1):
                        pass
                    break
                except OSError:
                    time.sleep(.02)
            else:
                raise AssertionError('patch server did not start')
            yield offset
        finally:
            proc.terminate()
            proc.wait(timeout=5)


def request(offset, product='1000', region='W2U', command=7, version='20030909_A'):
    payload = region.encode().ljust(4, b'\0') + product.encode() + version.encode() + b'\0'
    packet = polserver2.frame(command, payload)
    port = polserver2.port_for_product(product) + offset
    with socket.create_connection(('127.0.0.1', port), 2) as conn:
        conn.sendall(packet)
        reply = b''
        while len(reply) < 4 or len(reply) < struct.unpack_from('<I', reply)[0]:
            data = conn.recv(65536)
            if not data:
                raise AssertionError('connection closed without a complete reply')
            reply += data
    assert struct.unpack_from('<I', reply, 4)[0] == polserver2.cksum(reply)
    return reply


class StartupTests(unittest.TestCase):
    def setUp(self):
        kv.delete(clientbuilds.KEY + '127.0.0.1')

    def test_empty_archive_answers_viewer_and_title_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with running(root) as offset:
                for region, product in [('W2U', '1000'), ('PS2', '0001'), ('X2U', '0015')]:
                    reply = request(offset, product, region)
                    self.assertEqual(struct.unpack_from('<I', reply, 12)[0], 8)
                    self.assertIn(b'registered\0', reply)
                    self.assertIn(b'192.0.2.42\0', reply)
                    self.assertIn(b'20030909_A\0', reply[0x58:])
                # Version-only mode must never pretend to have update files.
                self.assertEqual(request(offset, command=1), polserver2.REJECT)
                self.assertIn('W2U/1000', builds())

    def test_empty_relogin_uses_static_viewer_fallback_without_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = {'POLP_FALLBACK_VERSION': '20130104_0'}
            for _ in range(2):  # Cold startup and restart use the same static value.
                before = builds()
                with running(root, **settings) as offset:
                    for region in ('P2U', 'PS2', 'W2U'):
                        reply = request(offset, region=region, version='')
                        self.assertIn(b'registered\0', reply)
                        self.assertEqual(reply[0x5c:], b'20130104_0\0')
                    self.assertEqual(builds(), before)
                    # The static fallback cannot override a reported build.
                    reported = request(offset, region='P2U', version='20031006_0')
                    self.assertEqual(reported[0x5c:], b'20031006_0\0')
                    previous = builds()
                    # Even after another build was reported, empty checks use
                    # configuration, and don't replace portal build metadata.
                    reply = request(offset, region='P2U', version='')
                    self.assertEqual(reply[0x5c:], b'20130104_0\0')
                    self.assertEqual(builds(), previous)
                    # Viewer fallback must not invent a title version.
                    title = request(offset, region='P2U', product='0001', version='')
                    self.assertIn(b'empty\0', title)
                    self.assertEqual(title[0x5c:], b'\0')
                    malformed = request(offset, version='bad')
                    self.assertIn(b'unknown\0', malformed)
                    self.assertEqual(malformed[0x5c:], b'bad\0')

    def test_partial_archive_served_and_shutdown_rows_filtered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / 'PS2-1000'
            bundle.mkdir()
            (bundle / 'meta.json').write_text(json.dumps({
                'latest_version': '20150901_X', 'port': 54000}))
            plain = (b'file keep.dat {\n20120610_A 1 1 1 keep.slc 1\n}\n'
                     b'file V/system/image/goodbye.png {\n'
                     b'20150901_X 1 1 1 goodbye.slc 1\n}\nend\n')
            (bundle / 'patchlist.raw').write_bytes(slc_compress(plain))
            with running(root, POLP_FALLBACK_VERSION='20130104_0') as offset:
                reply = request(offset, region='PS2', version='20120610_A')
                self.assertIn(b'20120610_A\0', reply[0x58:])
                self.assertIn(b'registered\0', reply)
                # Empty checks for real archives still use archive status/latest.
                empty = request(offset, region='PS2', version='')
                self.assertIn(b'empty\0', empty)
                self.assertEqual(empty[0x5c:], b'20120610_A\0')
                catalog = slc_decompress(request(offset, region='PS2', command=1)[16:])
                self.assertIn(b'keep.dat', catalog)
                self.assertNotIn(b'goodbye.png', catalog)
                self.assertIn(b'20030909_A\0', request(offset, region='P2U')[0x58:])

    def test_strict_mode_and_empty_archive_check_fail(self):
        with tempfile.TemporaryDirectory() as root:
            env = {k: v for k, v in os.environ.items() if not k.startswith('POLP_')}
            for arguments, settings in [([], {}), (['--check'], {'POLP_CURRENT_ALL': '1'})]:
                result = subprocess.run([sys.executable, str(ROOT / 'services/polserver2.py'),
                                         root, *arguments], env={**env, **settings},
                                        capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('no bundles under', result.stderr)


if __name__ == '__main__':
    unittest.main()
