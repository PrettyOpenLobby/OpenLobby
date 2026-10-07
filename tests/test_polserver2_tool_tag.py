#!/usr/bin/env python3
"""A PC drive updater's "PS2+" tag: the console's bundle, unpaced, unrecorded.

    python tests/test_polserver2_tool_tag.py

psbbn-playonline's `playonline.update` writes a console's title partition from
a PC. It asks for the console's bundle as "PS2+" / "P2U+" so the reply skips
the PS2 pacing (~280 KB/s), which would make a 5 GB FFXI update take as long
from the PC as on the console. The marked tag must get byte-identical answers
to the console's own tag, must not be paced, and must not be recorded as the
build a console at that address runs.
"""
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services'))
sys.path.insert(0, str(ROOT / 'tools'))
import pgtest  # noqa: E402
pgtest.use_fresh_valkey()
import clientbuilds  # noqa: E402
import polserver2  # noqa: E402
from polcore import kv  # noqa: E402
from slc import slc_compress  # noqa: E402

ADDR = ('127.0.0.77', 40000)
BLOB = b'\x01hello from the bundle'


def bundle(root):
    path = root / 'PS2-0001'
    (path / 'blobs' / '20160203_0').mkdir(parents=True)
    listing = ('file a.dat {\n20160203_0 21 0 0 20160203_0/0.slc %d\n}\n\nend\n\n\n'
               % len(BLOB)).encode()
    (path / 'patchlist.raw').write_bytes(slc_compress(listing))
    (path / 'blobs' / '20160203_0' / '0.slc').write_bytes(BLOB)
    (path / 'meta.json').write_text(json.dumps({
        'region': 'PS2', 'product': '0001', 'port': 53001,
        'latest_version': '20160203_0', 'oldest_version': '00000000_0'}))
    return polserver2.load_bundles(str(root))


def cmd7(region, version='20070911_0'):
    body = bytearray(0x48)
    body[0:4] = region.encode().ljust(4, b'\0')
    body[4:8] = b'0001'
    body[8:8 + len(version)] = version.encode()
    return polserver2.frame(7, bytes(body))


def cmd1(region):
    return polserver2.frame(1, region.encode().ljust(4, b'\0') + b'0001')


def cmd3(region, path='20160203_0/0.slc'):
    body = struct.pack('<II', 0, 0x10000)
    body += region.encode().ljust(4, b'\0') + b'0001'
    body += struct.pack('<I', len(path) + 1) + path.encode() + b'\0'
    return polserver2.frame(3, body)


class ToolTagTests(unittest.TestCase):
    def setUp(self):
        kv.delete(clientbuilds.KEY + ADDR[0])
        self.dir = tempfile.TemporaryDirectory()
        self.server = polserver2.Server(bundle(Path(self.dir.name)),
                                        '192.0.2.42', False)

    def tearDown(self):
        self.dir.cleanup()

    def ask(self, pkt):
        cmd = struct.unpack_from('<I', pkt, 12)[0]
        return self.server.dispatch(pkt, cmd, ADDR, '192.0.2.42')

    def test_marked_tag_gets_the_console_answers(self):
        for build in (cmd7, cmd1, cmd3):
            with self.subTest(cmd=build.__name__):
                want = self.ask(build('PS2'))
                self.assertNotEqual(want, polserver2.REJECT)
                self.assertEqual(self.ask(build('PS2+')), want)

    def test_marked_tag_is_not_paced(self):
        for build in (cmd7, cmd1, cmd3):
            with self.subTest(cmd=build.__name__):
                pkt = build('PS2+')
                cmd = struct.unpack_from('<I', pkt, 12)[0]
                self.assertFalse(polserver2.ps2_request(pkt, cmd))
                # the control: the console's own tag IS paced
                pkt = build('PS2')
                self.assertTrue(polserver2.ps2_request(pkt, cmd))

    def test_marked_tag_is_not_recorded_as_a_console_build(self):
        self.ask(cmd7('PS2+'))
        self.assertNotIn('PS2/0001', clientbuilds.for_address(ADDR[0]))
        self.ask(cmd7('PS2'))
        self.assertIn('PS2/0001', clientbuilds.for_address(ADDR[0]))

    def test_mark_on_an_unknown_region_still_rejects(self):
        self.assertEqual(self.ask(cmd7('W2U+')), polserver2.REJECT)
        self.assertEqual(self.ask(cmd3('P2U+')), polserver2.REJECT)


if __name__ == '__main__':
    unittest.main()
