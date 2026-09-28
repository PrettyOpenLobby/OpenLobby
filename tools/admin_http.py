#!/usr/bin/env python3
"""Drive the admin panel over HTTP from a self-test.

    from admin_http import Client, free_port, start_panel, stop

    proc, url = start_panel(tmp, services, free_port())
    owner = Client(url)
    owner.login("operator", "owner-password")
    status, body = owner.call("api/accounts")           # GET
    status, body = owner.call("api/account-kick", {...}) # POST, JSON body

`start_panel` runs services/admin.py as a subprocess on a loopback port with
its data and art directories under `tmp`; the database and live-state store
are whatever POL_DATABASE_URL and POL_VALKEY_URL say in this process's
environment (tools/pgtest.py sets both). `stop` ends it.
"""
import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_panel(tmp, services, port, env_extra=None):
    """Start admin.py on 127.0.0.1:`port`. Returns (process, base URL)."""
    env = dict(os.environ)
    env.update(POL_ADMIN_PORT=str(port),
               POL_DATA_DIR=tmp, POL_LOG_DIR=tmp, POL_ADMIN_PASSWORD="",
               POL_ADMIN_ART_ROOT=os.path.join(tmp, "art"),
               POL_ADMIN_WWW=os.path.join(tmp, "art"),
               PYTHONUNBUFFERED="1")
    env.update(env_extra or {})
    os.makedirs(os.path.join(tmp, "art"), exist_ok=True)
    log_path = os.path.join(tmp, "admin-%d.log" % time.time_ns())
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(services, "admin.py")], cwd=services,
        env=env, stdout=log, stderr=subprocess.STDOUT, text=True)
    url = "http://127.0.0.1:%d/" % port
    for _ in range(300):
        if proc.poll() is not None:
            log.close()
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                sys.stdout.write(fh.read())
            raise SystemExit("admin.py exited before it listened")
        try:
            urllib.request.urlopen(url + "api/session", timeout=1).read()
            return proc, url
        except Exception:
            time.sleep(0.1)
    stop(proc)
    raise SystemExit("admin.py never answered on %s" % url)


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


class Client:
    """A cookie-keeping HTTP client: one signed-in person."""

    def __init__(self, url):
        self.url = url
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path.lstrip("/"), data=data,
                                     method="GET" if body is None else "POST",
                                     headers={"Content-Type": "application/json"})
        try:
            r = self.op.open(req, timeout=15)
            raw = r.read()
            code = r.status
        except urllib.error.HTTPError as exc:
            raw, code = exc.read(), exc.code
        try:
            return code, json.loads(raw or b"{}")
        except ValueError:
            return code, raw.decode("utf-8", "replace")

    def login(self, user, pw):
        return self.call("api/login", {"username": user, "password": pw})[0]
