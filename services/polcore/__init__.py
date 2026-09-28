"""Shared server library for OpenLobby and the title services.

    polcore.db   PostgreSQL: the connection pool, transactions, advisory locks
                 and the numbered migrations under polcore/migrations/.
    polcore.kv   Live state that other processes need to see quickly (who is
                 online, sessions with a TTL, live rooms, the push queue):
                 Valkey when POL_VALKEY_URL is set, an in-process store when
                 it is not.

The rule between the two: anything that must survive a restart goes in the
database. Losing Valkey loses who is online and nothing else.

The name is the one the refactor plan gave the shared library. It is unrelated
to the client's polcore.dll, which the comments elsewhere in this tree cite by
address.
"""
