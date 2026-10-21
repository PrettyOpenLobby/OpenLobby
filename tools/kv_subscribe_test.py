"""kv.subscribe survives the server closing the connection.

Offline: the Valkey client is a stub whose first pubsub object dies with a
ConnectionError (what a Valkey restart looks like from the subscriber), and
the second one delivers a message. The subscription must come back on its
own, deliver through the new object, and stop when closed.
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))
from polcore import kv  # noqa: E402


class ConnectionError_(Exception):
    pass


class FakePubSub:
    def __init__(self, owner, die):
        self.owner, self.die, self.handlers, self.closed = owner, die, {}, False

    def subscribe(self, **handlers):
        self.handlers.update(handlers)

    def get_message(self, ignore_subscribe_messages=True, timeout=0.0):
        if self.die:
            raise ConnectionError_("Connection closed by server.")
        try:
            msg = self.owner.inbox.get(timeout=timeout)
        except Exception:
            return None
        h = self.handlers.get(msg["channel"])
        if h:
            h(msg)
        return None

    def close(self):
        self.closed = True


class FakeValkey:
    def __init__(self):
        import queue
        self.inbox = queue.Queue()
        self.pubsubs = []

    def pubsub(self, ignore_subscribe_messages=True):
        ps = FakePubSub(self, die=(len(self.pubsubs) == 0))
        self.pubsubs.append(ps)
        return ps


fails = []


def check(cond, what):
    print(("  OK   " if cond else "  FAIL ") + what)
    if not cond:
        fails.append(what)


store = kv.ValkeyKV.__new__(kv.ValkeyKV)
store.prefix = "t:"
store._r = FakeValkey()
store.SUBSCRIBE_RETRY = (0.05, 0.2)
got = []
sub = store.subscribe("presence", lambda ch, data: got.append((ch, data)))
deadline = time.time() + 3
while len(store._r.pubsubs) < 2 and time.time() < deadline:
    time.sleep(0.02)
check(len(store._r.pubsubs) >= 2, f"a second pubsub object was opened after the first died ({len(store._r.pubsubs)})")
check(store._r.pubsubs[0].closed, "the dead pubsub object was closed")
store._r.inbox.put({"channel": "t:presence", "data": "zone:3"})
deadline = time.time() + 3
while not got and time.time() < deadline:
    time.sleep(0.02)
check(got == [("presence", "zone:3")], f"a message published after the restart is delivered ({got})")
check(sub._thread.is_alive(), "the subscriber thread is still alive")
sub.close()
check(not sub._thread.is_alive(), "close() ends the thread")
check(store._r.pubsubs[-1].closed, "close() closes the live pubsub object")
if fails:
    print(f"\nFAIL: {len(fails)}")
    sys.exit(1)
print("\nkv_subscribe_test: OK")
