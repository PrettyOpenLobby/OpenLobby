"""Live state shared between processes: Valkey, or an in-process store.

    POL_VALKEY_URL   valkey://host:6379/0 (redis:// works too). Empty means the
                     in-process MemoryKV, which is right for tests and for a
                     single-process run and wrong for anything else: two
                     containers with MemoryKV do not see each other.
    POL_KV_PREFIX    namespace put in front of every key and channel
                     (default "pol:")

Nothing durable goes here. An operator who loses Valkey loses who is online,
live sessions, live rooms and undelivered pushes, and nothing else; accounts,
friends, mail and saves live in the database (polcore.db).

Values are text. Numbers are stored as their decimal string; structured values
go through get_json/set_json. Both backends expose the same methods:

    get(key) / set(key, value, ttl=None) / delete(*keys) / exists(key)
    setnx(key, value, ttl=None) -> bool       SET NX, the lock primitive
    expire(key, ttl) -> bool / ttl(key) -> int (-2 missing, -1 no expiry)
    incr(key, amount=1) -> int
    hget / hset(key, field=None, value=None, mapping=None) / hgetall / hdel
    push(key, *values) -> length / pop(key, timeout=None) / llen(key)
    move(src, dst, timeout=None) -> the value moved from the head of src to
                                    the tail of dst, or None (LMOVE/BLMOVE)
    lrem(key, value, count=1) -> removed / lrange(key, start=0, stop=-1)
    publish(channel, message) -> receivers
    subscribe(channels, callback) -> Subscription (callback(channel, message)
                                     runs on its own thread; .close() stops it)
    keys(pattern="*") -> key names without the prefix
    get_json / set_json, ping()

`ttl` arguments are seconds (int or float). `pop(key, timeout)` and
`move(src, dst, timeout)` block up to `timeout` seconds for an element; None
does not block. `move` is the reliable-queue step: the element stays in `dst`
(a processing list) until the consumer `lrem`s it, so a consumer that dies
mid-delivery leaves it there to be delivered again.

The module-level functions (kv.get, kv.set, ...) use one backend per process,
chosen at first use from the environment. `kv.default()` returns it and
`kv.reset()` forgets it (tests).
"""
import collections
import fnmatch
import json
import math
import os
import queue
import threading
import time

__all__ = ["MemoryKV", "ValkeyKV", "Subscription", "open_kv", "default", "reset"]


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


class Subscription:
    """A running subscription. close() stops delivery and joins the thread."""

    def __init__(self, stop, thread):
        self._stop = stop
        self._thread = thread

    def close(self, timeout=2.0):
        self._stop()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _Base:
    prefix = "pol:"

    def _k(self, key):
        return self.prefix + key

    def get_json(self, key, default=None):
        raw = self.get(key)
        return default if raw is None else json.loads(raw)

    def set_json(self, key, value, ttl=None):
        return self.set(key, json.dumps(value, separators=(",", ":")), ttl=ttl)

    @staticmethod
    def _channels(channels):
        return [channels] if isinstance(channels, str) else list(channels)


# --------------------------------------------------------------------------- #
# in-process backend
# --------------------------------------------------------------------------- #
class MemoryKV(_Base):
    """Thread-safe in-process store with the same behaviour as the Valkey one.

    Expiry is checked on every access, so an expired key is never returned,
    whether or not anything has swept it yet.
    """

    backend = "memory"

    def __init__(self, prefix=None):
        self.prefix = os.environ.get("POL_KV_PREFIX", "pol:") if prefix is None else prefix
        self._data = {}              # full key -> value (str, dict or deque)
        self._exp = {}               # full key -> monotonic deadline
        self._cond = threading.Condition()
        self._subs = collections.defaultdict(list)   # full channel -> [queue]

    # internal: callers hold self._cond
    def _live(self, k):
        dl = self._exp.get(k)
        if dl is not None and time.monotonic() >= dl:
            self._data.pop(k, None)
            self._exp.pop(k, None)
        return k in self._data

    def _typed(self, k, kind):
        if not self._live(k):
            return None
        v = self._data[k]
        if not isinstance(v, kind):
            raise TypeError(f"WRONGTYPE: {k!r} holds a {type(v).__name__}")
        return v

    def _set_ttl(self, k, ttl):
        if ttl is None:
            self._exp.pop(k, None)
        else:
            self._exp[k] = time.monotonic() + float(ttl)

    def ping(self):
        return True

    def get(self, key):
        with self._cond:
            return self._typed(self._k(key), str)

    def set(self, key, value, ttl=None):
        k = self._k(key)
        with self._cond:
            self._data[k] = _text(value)
            self._set_ttl(k, ttl)
        return True

    def setnx(self, key, value, ttl=None):
        k = self._k(key)
        with self._cond:
            if self._live(k):
                return False
            self._data[k] = _text(value)
            self._set_ttl(k, ttl)
            return True

    def delete(self, *keys):
        n = 0
        with self._cond:
            for key in keys:
                k = self._k(key)
                if self._live(k):
                    n += 1
                self._data.pop(k, None)
                self._exp.pop(k, None)
        return n

    def exists(self, key):
        with self._cond:
            return self._live(self._k(key))

    def expire(self, key, ttl):
        k = self._k(key)
        with self._cond:
            if not self._live(k):
                return False
            self._set_ttl(k, ttl)
            return True

    def ttl(self, key):
        k = self._k(key)
        with self._cond:
            if not self._live(k):
                return -2
            dl = self._exp.get(k)
            return -1 if dl is None else max(0, math.ceil(dl - time.monotonic()))

    def incr(self, key, amount=1):
        k = self._k(key)
        with self._cond:
            cur = self._typed(k, str)
            n = (int(cur) if cur is not None else 0) + int(amount)
            self._data[k] = str(n)
            return n

    # hashes
    def hget(self, key, field):
        with self._cond:
            h = self._typed(self._k(key), dict)
            return None if h is None else h.get(_text(field))

    def hset(self, key, field=None, value=None, mapping=None):
        items = dict(mapping or {})
        if field is not None:
            items[field] = value
        k = self._k(key)
        with self._cond:
            h = self._typed(k, dict)
            if h is None:
                h = self._data[k] = {}
            added = 0
            for f, v in items.items():
                f = _text(f)
                added += f not in h
                h[f] = _text(v)
            return added

    def hgetall(self, key):
        with self._cond:
            h = self._typed(self._k(key), dict)
            return dict(h) if h else {}

    def hdel(self, key, *fields):
        k = self._k(key)
        with self._cond:
            h = self._typed(k, dict)
            if h is None:
                return 0
            n = sum(h.pop(_text(f), None) is not None for f in fields)
            if not h:
                self._data.pop(k, None)
                self._exp.pop(k, None)
            return n

    # lists (a FIFO queue: push at the tail, pop from the head)
    def push(self, key, *values):
        k = self._k(key)
        with self._cond:
            lst = self._typed(k, collections.deque)
            if lst is None:
                lst = self._data[k] = collections.deque()
            lst.extend(_text(v) for v in values)
            self._cond.notify_all()
            return len(lst)

    def pop(self, key, timeout=None):
        k = self._k(key)
        deadline = None if not timeout else time.monotonic() + float(timeout)
        with self._cond:
            while True:
                lst = self._typed(k, collections.deque)
                if lst:
                    v = lst.popleft()
                    if not lst:
                        self._data.pop(k, None)
                        self._exp.pop(k, None)
                    return v
                if deadline is None:
                    return None
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(left)

    def llen(self, key):
        with self._cond:
            lst = self._typed(self._k(key), collections.deque)
            return len(lst) if lst else 0

    def move(self, src, dst, timeout=None):
        ks, kd = self._k(src), self._k(dst)
        deadline = None if not timeout else time.monotonic() + float(timeout)
        with self._cond:
            while True:
                lst = self._typed(ks, collections.deque)
                if lst:
                    out = self._typed(kd, collections.deque)
                    if out is None:
                        out = self._data[kd] = collections.deque()
                    v = lst.popleft()
                    if not lst:
                        self._data.pop(ks, None)
                        self._exp.pop(ks, None)
                    out.append(v)
                    self._cond.notify_all()
                    return v
                if deadline is None:
                    return None
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(left)

    def lrem(self, key, value, count=1):
        k = self._k(key)
        value = _text(value)
        with self._cond:
            lst = self._typed(k, collections.deque)
            if not lst:
                return 0
            n = 0
            keep = collections.deque()
            for v in lst:
                if v == value and (count == 0 or n < abs(count)):
                    n += 1
                    continue
                keep.append(v)
            if keep:
                self._data[k] = keep
            else:
                self._data.pop(k, None)
                self._exp.pop(k, None)
            return n

    def lrange(self, key, start=0, stop=-1):
        with self._cond:
            lst = self._typed(self._k(key), collections.deque)
            items = list(lst) if lst else []
        stop = len(items) if stop == -1 else stop + 1
        return items[start:stop]

    def keys(self, pattern="*"):
        full = self.prefix + pattern
        with self._cond:
            names = [k for k in list(self._data) if self._live(k)]
        return sorted(k[len(self.prefix):] for k in names
                      if fnmatch.fnmatchcase(k, full))

    # pub/sub: each subscription has its own queue and delivery thread, so a
    # slow callback never blocks the publisher (as with Valkey).
    def publish(self, channel, message):
        c = self._k(channel)
        with self._cond:
            targets = list(self._subs.get(c, ()))
        for q in targets:
            q.put((channel, _text(message)))
        return len(targets)

    def subscribe(self, channels, callback):
        names = self._channels(channels)
        q = queue.Queue()
        with self._cond:
            for name in names:
                self._subs[self._k(name)].append(q)
        stop_mark = object()

        def run():
            while True:
                item = q.get()
                if item is stop_mark:
                    return
                try:
                    callback(*item)
                except Exception as exc:            # keep the subscription alive
                    print(f"[kv] subscriber callback raised: {exc!r}")

        t = threading.Thread(target=run, name="kv-sub", daemon=True)
        t.start()

        def stop():
            with self._cond:
                for name in names:
                    lst = self._subs.get(self._k(name), [])
                    if q in lst:
                        lst.remove(q)
            q.put(stop_mark)

        return Subscription(stop, t)

    def flush(self):
        """Drop every key (tests)."""
        with self._cond:
            self._data.clear()
            self._exp.clear()


# --------------------------------------------------------------------------- #
# Valkey backend
# --------------------------------------------------------------------------- #
def _ms(ttl):
    return None if ttl is None else max(1, int(round(float(ttl) * 1000)))


class ValkeyKV(_Base):
    """The same API over a Valkey (or Redis) server."""

    backend = "valkey"

    def __init__(self, url, prefix=None):
        import valkey                      # only needed when this backend is used
        self.prefix = os.environ.get("POL_KV_PREFIX", "pol:") if prefix is None else prefix
        self.url = url
        self._r = valkey.Valkey.from_url(url, decode_responses=True,
                                         health_check_interval=30)

    def ping(self):
        return bool(self._r.ping())

    def get(self, key):
        return self._r.get(self._k(key))

    def set(self, key, value, ttl=None):
        return bool(self._r.set(self._k(key), _text(value), px=_ms(ttl)))

    def setnx(self, key, value, ttl=None):
        return bool(self._r.set(self._k(key), _text(value), px=_ms(ttl), nx=True))

    def delete(self, *keys):
        return self._r.delete(*[self._k(k) for k in keys]) if keys else 0

    def exists(self, key):
        return bool(self._r.exists(self._k(key)))

    def expire(self, key, ttl):
        return bool(self._r.pexpire(self._k(key), _ms(ttl)))

    def ttl(self, key):
        ms = self._r.pttl(self._k(key))
        return ms if ms < 0 else math.ceil(ms / 1000)

    def incr(self, key, amount=1):
        return self._r.incrby(self._k(key), int(amount))

    def hget(self, key, field):
        return self._r.hget(self._k(key), _text(field))

    def hset(self, key, field=None, value=None, mapping=None):
        items = {_text(f): _text(v) for f, v in (mapping or {}).items()}
        if field is not None:
            items[_text(field)] = _text(value)
        return self._r.hset(self._k(key), mapping=items) if items else 0

    def hgetall(self, key):
        return self._r.hgetall(self._k(key))

    def hdel(self, key, *fields):
        return self._r.hdel(self._k(key), *[_text(f) for f in fields]) if fields else 0

    def push(self, key, *values):
        return self._r.rpush(self._k(key), *[_text(v) for v in values])

    def pop(self, key, timeout=None):
        if not timeout:
            return self._r.lpop(self._k(key))
        got = self._r.blpop([self._k(key)], timeout=float(timeout))
        return None if got is None else got[1]

    def llen(self, key):
        return self._r.llen(self._k(key))

    def move(self, src, dst, timeout=None):
        ks, kd = self._k(src), self._k(dst)
        if not timeout:
            return self._r.lmove(ks, kd, "LEFT", "RIGHT")
        return self._r.blmove(ks, kd, float(timeout), "LEFT", "RIGHT")

    def lrem(self, key, value, count=1):
        return self._r.lrem(self._k(key), int(count), _text(value))

    def lrange(self, key, start=0, stop=-1):
        return self._r.lrange(self._k(key), int(start), int(stop))

    def keys(self, pattern="*"):
        n = len(self.prefix)
        return sorted(k[n:] for k in self._r.scan_iter(match=self.prefix + pattern,
                                                       count=500))

    def publish(self, channel, message):
        return self._r.publish(self._k(channel), _text(message))

    def subscribe(self, channels, callback):
        names = self._channels(channels)
        n = len(self.prefix)
        ps = self._r.pubsub(ignore_subscribe_messages=True)

        def handler(msg):
            try:
                callback(msg["channel"][n:], msg["data"])
            except Exception as exc:                # keep the subscription alive
                print(f"[kv] subscriber callback raised: {exc!r}")

        ps.subscribe(**{self._k(c): handler for c in names})
        t = ps.run_in_thread(sleep_time=0.2, daemon=True)

        def stop():
            t.stop()
            try:
                ps.close()
            except Exception:
                pass

        return Subscription(stop, t)

    def flush(self):
        """Drop every key under this prefix (tests)."""
        ks = list(self._r.scan_iter(match=self.prefix + "*", count=500))
        if ks:
            self._r.delete(*ks)


# --------------------------------------------------------------------------- #
# process default
# --------------------------------------------------------------------------- #
def open_kv(url=None, prefix=None):
    """A backend for `url` (Valkey) or, when it is empty, a new MemoryKV."""
    return ValkeyKV(url, prefix) if url else MemoryKV(prefix)


_default = None
_default_lock = threading.Lock()


def default():
    """The process-wide backend, chosen at first use from POL_VALKEY_URL."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = open_kv(os.environ.get("POL_VALKEY_URL", "").strip() or None)
    return _default


def reset(backend=None):
    """Forget the process backend (the next use re-reads the environment), or
    install `backend` in its place."""
    global _default
    with _default_lock:
        _default = backend


def _delegate(name):
    def call(*args, **kwargs):
        return getattr(default(), name)(*args, **kwargs)
    call.__name__ = name
    call.__doc__ = f"default().{name}(...)"
    return call


for _name in ("get", "set", "setnx", "delete", "exists", "expire", "ttl", "incr",
              "hget", "hset", "hgetall", "hdel", "push", "pop", "llen", "keys",
              "move", "lrem", "lrange",
              "publish", "subscribe", "get_json", "set_json", "ping"):
    globals()[_name] = _delegate(_name)
    __all__.append(_name)
del _name
