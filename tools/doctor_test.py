#!/usr/bin/env python3
"""Offline tests for the parsers in tools/doctor.py: .env reading, compose
interpolation and port lists, DNS packets and the enabled-games list. Nothing
here touches the network or Docker.

    python tools/doctor_test.py
"""
import os
import socket
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import doctor  # noqa: E402

COMPOSE = """\
x-app: &app
  image: openlobby:latest
  environment: &appenv
    POL_ADVERTISE: "${POL_ADVERTISE:-}"

volumes:
  pol-data:

services:
  dns:
    <<: *app
    command: dns
    ports:
      # a comment inside the list
      - "${POL_DNS_BIND:-0.0.0.0}:53:53/udp"
    healthcheck: &pyhc
      test: ["CMD", "python"]

  login:
    <<: *app
    ports:
      - "51200-51202:51200-51202"
      - "110:110"   # POP3
    volumes:
      - ./config:/config:ro

  admin:
    <<: *app
    ports:
      - "127.0.0.1:8090:8090"

  gmd:
    <<: *app
    profiles: ["gm"]
    ports:
      - "51112:51112/udp"

  longform:
    ports:
      - target: 80
        published: "8081"
        protocol: tcp
        host_ip: 192.0.2.7
      - "9000"

  quiet:
    image: example
"""


class EnvTest(unittest.TestCase):
    def test_parse_env(self):
        env = doctor.parse_env(
            "# comment\n"
            "\n"
            "POL_ADVERTISE=192.0.2.10\n"
            "POL_WWW=\n"
            "export POL_DB_PASSWORD=abc123\n"
            "QUOTED=\"a # b\"\n"
            "SINGLE='x'\n"
            "INLINE=value # trailing comment\n"
            "HASHED=a#b\n"
            "not a line\n")
        self.assertEqual(env["POL_ADVERTISE"], "192.0.2.10")
        self.assertEqual(env["POL_WWW"], "")
        self.assertEqual(env["POL_DB_PASSWORD"], "abc123")
        self.assertEqual(env["QUOTED"], "a # b")
        self.assertEqual(env["SINGLE"], "x")
        self.assertEqual(env["INLINE"], "value")
        self.assertEqual(env["HASHED"], "a#b")
        self.assertNotIn("not a line", env)

    def test_substitute(self):
        env = {"SET": "198.51.100.4", "EMPTY": ""}
        s = doctor.substitute
        self.assertEqual(s("${SET:-0.0.0.0}:53", env), "198.51.100.4:53")
        self.assertEqual(s("${EMPTY:-0.0.0.0}:53", env), "0.0.0.0:53")
        self.assertEqual(s("${EMPTY-x}", env), "")
        self.assertEqual(s("${UNSET-x}", env), "x")
        self.assertEqual(s("${UNSET}/$SET", env), "/198.51.100.4")
        self.assertEqual(s("$$SET", env), "$SET")

    def test_stub_ip(self):
        self.assertEqual(doctor.config_stub_ip('dns:\nstub_ip: "203.0.113.9"\n'),
                         "203.0.113.9")
        self.assertEqual(doctor.config_stub_ip("nothing: here\n"), "")


class ComposeTest(unittest.TestCase):
    def setUp(self):
        self.services = doctor.parse_compose(COMPOSE)

    def test_services_and_profiles(self):
        self.assertEqual(list(self.services),
                         ["dns", "login", "admin", "gmd", "longform", "quiet"])
        self.assertEqual(self.services["gmd"]["profiles"], ["gm"])
        self.assertEqual(self.services["dns"]["profiles"], [])
        self.assertEqual(self.services["quiet"]["ports"], [])
        self.assertEqual(self.services["login"]["ports"],
                         ["51200-51202:51200-51202", "110:110"])

    def test_port_specs(self):
        p = doctor.parse_port_spec
        self.assertEqual(p("80:80"), [("", 80, "tcp")])
        self.assertEqual(p("127.0.0.1:8090:8090"), [("127.0.0.1", 8090, "tcp")])
        self.assertEqual(p("0.0.0.0:53:53/udp"), [("", 53, "udp")])
        self.assertEqual(p("192.0.2.3:53:53/UDP"), [("192.0.2.3", 53, "udp")])
        self.assertEqual([x[1] for x in p("53001-53003:53001-53003")],
                         [53001, 53002, 53003])
        self.assertEqual(p("9000"), [])
        self.assertEqual(p("[::1]:8080:8080"), [("::1", 8080, "tcp")])
        with self.assertRaises(ValueError):
            p("70000:70000")

    def test_compose_ports(self):
        ports = doctor.compose_ports(self.services, {"POL_DNS_BIND": "192.0.2.5"})
        self.assertIn(("dns", "192.0.2.5", 53, "udp"), ports)
        self.assertIn(("login", "", 51201, "tcp"), ports)
        self.assertIn(("login", "", 110, "tcp"), ports)
        self.assertIn(("admin", "127.0.0.1", 8090, "tcp"), ports)
        self.assertIn(("gmd", "", 51112, "udp"), ports)
        self.assertIn(("longform", "192.0.2.7", 8081, "tcp"), ports)
        self.assertEqual(len([x for x in ports if x[0] == "longform"]), 1)
        ports = doctor.compose_ports(self.services, {})
        self.assertIn(("dns", "", 53, "udp"), ports)

    def test_real_compose_file(self):
        """The shipped docker-compose.yml parses into the services and ports
        this tool expects to find in it."""
        path = os.path.join(doctor.ROOT, "docker-compose.yml")
        if not os.path.isfile(path):
            self.skipTest("no docker-compose.yml beside tools/")
        with open(path, encoding="utf-8") as f:
            services = doctor.parse_compose(f.read())
        for name in ("dns", "login", "authrelay", "patch", "admin"):
            self.assertIn(name, services)
        ports = {(p, proto) for _s, _h, p, proto in doctor.compose_ports(services, {})}
        for want in ((53, "udp"), (54000, "tcp"), (51240, "tcp"), (51241, "tcp"),
                     (8090, "tcp")):
            self.assertIn(want, ports)


class DnsTest(unittest.TestCase):
    def test_query_layout(self):
        q = doctor.dns_query("pt008.pol.com", 0x1234)
        self.assertEqual(q[:12], struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0))
        self.assertEqual(q[12:], b"\x05pt008\x03pol\x03com\x00\x00\x01\x00\x01")
        with self.assertRaises(ValueError):
            doctor.dns_query("bad..name", 1)

    def _reply(self, query, ips, rcode=0, qid=None):
        """A reply shaped like services/stub.py's: the question echoed, each
        answer pointing back at it with a compression pointer."""
        tid = struct.pack(">H", qid) if qid is not None else query[:2]
        header = tid + struct.pack(">HHHHH", 0x8180 | rcode, 1, len(ips), 0, 0)
        out = header + query[12:]
        for ip in ips:
            out += (b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4)
                    + socket.inet_aton(ip))
        return out

    def test_parse_answer(self):
        q = doctor.dns_query("pt008.pol.com", 77)
        self.assertEqual(doctor.dns_parse_reply(self._reply(q, ["192.0.2.10"]), 77),
                         (0, ["192.0.2.10"]))
        two = self._reply(q, ["198.51.100.1", "203.0.113.2"])
        self.assertEqual(doctor.dns_parse_reply(two, 77)[1],
                         ["198.51.100.1", "203.0.113.2"])

    def test_parse_refused_and_mismatch(self):
        q = doctor.dns_query("example.com", 9)
        self.assertEqual(doctor.dns_parse_reply(self._reply(q, [], rcode=5), 9), (5, []))
        with self.assertRaises(ValueError):
            doctor.dns_parse_reply(self._reply(q, ["192.0.2.1"], qid=10), 9)
        with self.assertRaises(ValueError):
            doctor.dns_parse_reply(q, 9)            # a query is not a reply
        with self.assertRaises(ValueError):
            doctor.dns_parse_reply(b"\x00\x09", 9)

    def test_skips_other_record_types(self):
        q = doctor.dns_query("example.com", 3)
        header = q[:2] + struct.pack(">HHHHH", 0x8180, 1, 2, 0, 0)
        cname = b"\x03www\xc0\x0c"
        body = (q[12:]
                + b"\xc0\x0c" + struct.pack(">HHIH", 5, 1, 60, len(cname)) + cname
                + b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4)
                + socket.inet_aton("203.0.113.40"))
        self.assertEqual(doctor.dns_parse_reply(header + body, 3), (0, ["203.0.113.40"]))


class MiscTest(unittest.TestCase):
    def test_enabled_games(self):
        g = doctor.parse_enabled_games
        self.assertEqual(g('["tm", "fe"]'), ["tm", "fe"])
        self.assertEqual(g('{"tm": true, "fe": false}'), ["tm"])
        self.assertEqual(g('{"enabled": ["jan"]}'), ["jan"])
        self.assertEqual(g("# titles\ntm\nfe, fmo\njan=../hippaulholo\n"),
                         ["tm", "fe", "fmo", "jan"])
        self.assertEqual(g(""), [])

    def test_ps_json(self):
        rows = doctor.parse_ps_json('{"Service":"dns","State":"running"}\n'
                                    '{"Service":"http","State":"exited"}\n')
        self.assertEqual([r["Service"] for r in rows], ["dns", "http"])
        rows = doctor.parse_ps_json('[{"Service":"dns","State":"running"}]')
        self.assertEqual(rows[0]["State"], "running")
        self.assertEqual(doctor.parse_ps_json(""), [])

    def test_advertise_checks(self):
        def run(adv, local, exists=True):
            rep = doctor.Report()
            path = os.path.abspath(__file__) if exists else "/nonexistent/.env"
            doctor.check_env(rep, path, {"POL_ADVERTISE": adv}, local)
            return [(r["status"], r["check"]) for r in rep.rows]
        self.assertIn(("WARN", "advertise"), run("", {"127.0.0.1"}))
        self.assertIn(("WARN", "advertise"), run("127.0.0.1", {"127.0.0.1"}))
        self.assertIn(("FAIL", "advertise"), run("host.example", set()))
        self.assertIn(("FAIL", "advertise"), run("0.0.0.0", set()))
        self.assertIn(("OK", "advertise"), run("192.0.2.10", {"192.0.2.10"}))
        self.assertIn(("WARN", "advertise"), run("192.0.2.10", {"198.51.100.3"}))
        self.assertIn(("WARN", "env"), run("192.0.2.10", {"192.0.2.10"}, exists=False))


class ConsolePathTest(unittest.TestCase):
    """The DNS answer and the Windows Firewall lines, with stand-ins for the
    resolver and for PowerShell."""

    RULES = [{"name": "OpenLobby UDP 53 (Private)", "enabled": "True", "protocol": "UDP", "ports": "53"},
             {"name": "OpenLobby TCP 80, 443 (Private)", "enabled": "True", "protocol": "TCP",
              "ports": "80,443"}]

    def rows(self, state, address="192.0.2.20"):
        rep = doctor.Report()
        doctor.check_windows_firewall(rep, address, state)
        return [(r["status"], r["check"], r["message"]) for r in rep.rows]

    def state(self, **kw):
        s = {"rules": list(self.RULES), "alias": "Ethernet", "profile": "Private", "found": True,
             "blocks": [], "allows": []}
        s.update(kw)
        return s

    def test_all_in_place(self):
        rows = self.rows(self.state())
        self.assertEqual([r[0] for r in rows], ["OK", "OK"], rows)
        self.assertIn("2 rule(s) in group OpenLobby, UDP 53 included", rows[0][2])
        self.assertIn("192.0.2.20 is on Ethernet (network type: Private)", rows[1][2])

    def test_public_network_missing_rules_and_docker_blocks(self):
        rows = self.rows(self.state(rules=[], profile="Public",
                                    blocks=["Docker Desktop Backend (Public)"]))
        got = {(s, c) for s, c, _m in rows}
        self.assertIn(("WARN", "firewall"), got)
        self.assertIn(("WARN", "network"), got)
        self.assertIn(("FAIL", "firewall"), got)
        self.assertTrue(any("Docker Desktop Backend (Public)" in m for _s, _c, m in rows))

    def test_docker_allow_rule_covers_a_private_network(self):
        rows = self.rows(self.state(rules=[], allows=["Private"]))
        self.assertEqual(rows[0][0], "OK", rows)
        self.assertIn("Docker Desktop's own allow rule", rows[0][2])
        rows = self.rows(self.state(rules=[], allows=["Private"], profile="Public"))
        self.assertEqual(rows[0][0], "WARN", rows)

    def test_rules_without_dns_and_address_gone(self):
        rows = self.rows(self.state(rules=[self.RULES[1]], found=False))
        self.assertIn(("WARN", "firewall", "the OpenLobby rules do not let UDP 53 (DNS) in"), rows)
        self.assertTrue(any(c == "network" and "not an address of this PC" in m for _s, c, m in rows))

    def test_no_answer_from_powershell_is_skipped(self):
        self.assertEqual(self.rows(None)[0][:2], ("SKIP", "firewall"))

    def test_state_reads_the_json_line(self):
        calls = []

        def run(cmd, cwd=None, timeout=30, env=None):
            calls.append(cmd)
            return 0, 'WARNING: noise\nOLJSON {"rules": {"name": "x", "enabled": "True", "protocol": "UDP", ' \
                      '"ports": "53"}, "alias": "Wi-Fi", "profile": "Private", "found": true, ' \
                      '"blocks": null, "allows": "Private"}\n', ""
        real = doctor.run
        doctor.run = run
        try:
            got = doctor.windows_firewall_state("192.0.2.20")
            self.assertIsNone(doctor.windows_firewall_state("not an address"))
        finally:
            doctor.run = real
        self.assertEqual(len(calls), 1)
        self.assertIn("'192.0.2.20'", calls[0][-1])
        self.assertNotIn("@ADDR@", calls[0][-1])
        self.assertEqual(got["rules"][0]["ports"], "53")     # one rule comes as an object
        self.assertEqual(got["blocks"], [])
        self.assertEqual(got["allows"], ["Private"])

    def test_dns_answer_compared_with_the_address(self):
        answers = {"192.0.2.20": (0, ["192.0.2.20"]), "192.0.2.21": (0, ["203.0.113.9"])}
        real = doctor.dns_ask
        doctor.dns_ask = lambda server, name, timeout=3.0: answers.get(server)
        try:
            for server, want in (("192.0.2.20", "OK"), ("192.0.2.21", "FAIL"), ("192.0.2.22", "FAIL")):
                rep = doctor.Report()
                doctor.check_dns(rep, server, "192.0.2.20")
                self.assertEqual(rep.rows[0]["status"], want, (server, rep.rows))
        finally:
            doctor.dns_ask = real


if __name__ == "__main__":
    unittest.main()
