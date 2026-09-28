#!/usr/bin/env python3
"""Throwaway PostgreSQL (and Valkey) servers for the self-tests.

    import pgtest
    with pgtest.database() as url:       # a fresh, empty database
        ...

The first call starts `postgres:17-alpine` in Docker on a free loopback port,
waits until it accepts connections, and removes the container when the test
process exits. Every `database()` is a new `CREATE DATABASE test_<random>` on
that one server, dropped again afterwards, so tests cannot see each other's
tables.

POL_TEST_DATABASE_URL points at an existing server instead (any database on
it; the tests create and drop their own next to it), for machines without
Docker and for CI that runs Postgres as a service container.

`container()` is the generic part, used by the Valkey test as well.

Suites that exercise the account code call `use_fresh_database()` once at the
top: it creates a database, points POL_DATABASE_URL (and so polcore.db, and
any server the suite starts as a subprocess) at it, and drops it at exit.
tools/run_all.py starts the server once and hands every suite its URL through
POL_TEST_DATABASE_URL, so a full run pays for one container.

    python tools/pgtest.py      # start one, print its URL, remove it
"""
import atexit
import contextlib
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse

PG_IMAGE = "postgres:17-alpine"
PG_PASSWORD = "pgtest"

_started = []                  # container ids to remove at exit
_pg_url = None


def docker_available():
    """True when a Docker daemon answers."""
    if not shutil.which("docker"):
        return False
    try:
        p = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, timeout=20)
        return p.returncode == 0 and bool(p.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def _remove(cid):
    subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=60)


@atexit.register
def _cleanup():
    while _started:
        with contextlib.suppress(Exception):
            _remove(_started.pop())


def container(image, port, env=None, args=(), cmd=()):
    """Start `image` detached with `port` published on a free loopback port.
    Returns (container id, host port). Removed at exit (or via `stop`)."""
    argv = ["docker", "run", "--rm", "-d", "-p", f"127.0.0.1::{port}",
            "--label", "openlobby.selftest=1"]
    for k, v in (env or {}).items():
        argv += ["-e", f"{k}={v}"]
    argv += list(args) + [image] + list(cmd)
    p = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError(f"docker run {image} failed: {p.stderr.strip()}")
    cid = p.stdout.strip()
    _started.append(cid)
    out = subprocess.run(["docker", "port", cid, f"{port}/tcp"],
                         capture_output=True, text=True, timeout=30).stdout
    # "127.0.0.1:49153" (possibly several lines; the first is ours)
    host_port = int(out.strip().splitlines()[0].rsplit(":", 1)[1])
    return cid, host_port


def stop(cid):
    if cid in _started:
        _started.remove(cid)
    _remove(cid)


def _wait_pg(url, timeout=60):
    import psycopg
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(url, connect_timeout=2) as c:
                c.execute("SELECT 1")
                return
        except psycopg.OperationalError as exc:
            last = exc
            time.sleep(0.3)
    raise RuntimeError(f"postgres did not become ready: {last}")


def server_url():
    """URL of the maintenance database on the test server (started on demand)."""
    global _pg_url
    if _pg_url:
        return _pg_url
    given = os.environ.get("POL_TEST_DATABASE_URL", "").strip()
    if given:
        _pg_url = given
        return _pg_url
    if not docker_available():
        raise RuntimeError("no Docker daemon and POL_TEST_DATABASE_URL is unset")
    # tmpfs and fsync=off: nothing here needs to survive, and it makes the
    # CREATE DATABASE per test cheap.
    # C collation, like the compose file's database: text sorts and lower()
    # folds byte-wise, as SQLite did.
    _cid, port = container(
        PG_IMAGE, 5432, env={"POSTGRES_PASSWORD": PG_PASSWORD,
                             "POSTGRES_INITDB_ARGS": "--locale=C -E UTF8"},
        args=["--tmpfs", "/var/lib/postgresql/data"],
        cmd=["-c", "fsync=off", "-c", "synchronous_commit=off",
             "-c", "full_page_writes=off"])
    url = f"postgresql://postgres:{PG_PASSWORD}@127.0.0.1:{port}/postgres"
    _wait_pg(url)
    _pg_url = url
    return url


def _with_dbname(url, name):
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(path="/" + name))


def create_database():
    """A new empty database on the test server; returns its URL."""
    import psycopg
    base = server_url()
    name = "test_" + secrets.token_hex(6)
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    return _with_dbname(base, name)


def drop_database(url):
    import psycopg
    name = urllib.parse.urlsplit(url).path.lstrip("/")
    with psycopg.connect(server_url(), autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@contextlib.contextmanager
def database():
    """A fresh database for one test, dropped afterwards."""
    url = create_database()
    try:
        yield url
    finally:
        with contextlib.suppress(Exception):
            drop_database(url)


_owned = []                    # databases use_fresh_database() made


@atexit.register
def _drop_owned():
    # Close this process's pool first, or the drop waits on its connections.
    polcore_db = sys.modules.get("polcore.db")
    if polcore_db is not None:
        with contextlib.suppress(Exception):
            polcore_db.close()
    while _owned:
        with contextlib.suppress(Exception):
            drop_database(_owned.pop())


def use_fresh_database():
    """Point this process, and every process it starts, at a new empty database.

    Sets POL_DATABASE_URL (and repoints polcore.db if it is already imported),
    and POL_LOGIN_PW_KEYFILE to a key file in a fresh temporary directory, so a
    password sealed by one process of the suite unseals in another. Calling it
    again moves everything to another new database: the SQLite suites made a
    new accounts.db the same way. The databases are dropped when this process
    exits. Returns the URL.

    Without Docker or POL_TEST_DATABASE_URL this exits the suite with a
    failure: the code under test cannot run without a database.
    """
    import tempfile
    try:
        url = create_database()
    except Exception as exc:              # noqa: BLE001 -- say why, then fail
        print(f"FAIL: no PostgreSQL for this suite ({exc}); start Docker or set "
              "POL_TEST_DATABASE_URL", flush=True)
        sys.exit(1)
    _owned.append(url)
    os.environ["POL_DATABASE_URL"] = url
    if not os.environ.get("POL_LOGIN_PW_KEY"):
        keydir = tempfile.mkdtemp(prefix="pgtest-key-")
        os.environ["POL_LOGIN_PW_KEYFILE"] = os.path.join(keydir, "login-pw.key")
    polcore_db = sys.modules.get("polcore.db")
    if polcore_db is not None:
        polcore_db.configure(None)
    return url


if __name__ == "__main__":
    try:
        print(server_url())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
