"""Imports shared by the core modules: the optional service modules (accounts, polpro, ...) and the srvcore/authtoken names the old responders.py re-exported."""
import datetime
import hashlib
import itertools
import json
import os
import re
import secrets
import select
import socket
import struct
import sys
import time
import threading

import titles                   # the title-plugin seam (services/titles.py)  # noqa: E402
import titles as _titles_mod    # for functions that keep a local named `titles`  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from srvcore import shim_build_hidden  # noqa: E402
from srvcore import (  # re-exported: responders.X is srvcore.X
    CONFIG_PATH,
    LOG_DIR,
    load_config,
    _stamp,
    _LOG_LOCK,
    _LOG_FILES,
    _LOG_MAX,
    _LOG_KEEP,
    _log_handle_locked,
    log,
    _StderrTee,
    _stderr_capture,
    install_stderr_capture,
    hexdump,
    _CAP_LOCK,
    _CAP_SEEN,
    _CAP_KEEP,
    _LOGIN_TRACE,
    _LOGIN_TRACE_MAX,
    _arm_stack_dumps,
    _trace_begin,
    _trace,
    _trace_done,
    _trace_dump,
    save_capture,
    expand_ports,
    bind_peer,
    advertise_for,
)
from authtoken import (  # re-exported: responders.X is authtoken.X
    TOKEN_ALPHABET,
    _ACCT_STATUS,
    _CONST_48,
    _CONST_END,
    token_encode,
    _nonce_counter,
    _next_nonce,
    build_redirect_token,
    build_session_token,
    session_token_key,
    _STAMPS_LOCK,
    _STAMPS,
    _STAMP_TTL,
    _STAMP_KEEP,
    _STAMP_KEY,
    _STAMP_VER_KEY,
    _stamps_save_locked,
    load_stamps,
    _STAMPS_VER,
    _stamps_load_remote,
    _stamps_refresh,
    remember_stamp,
)

# Validated session cipher (reversed from polcore.dll). For a token0 connection the
# client derives K=0 and IV=modulus[0:8] (a fixed SE-key constant), so the whole
# keystream is known to us.
import sessioncrypt
try:
    import gmchat               # GM Call chat rooms; optional -- absent = no GM relay
except ImportError:
    gmchat = None
try:
    import contentlist          # lobby workstream dep; optional for directory/authserv
except ImportError:
    contentlist = None
try:
    import pmlfallback          # built-in portal pages when www/ has no file
except ImportError:
    pmlfallback = None
try:
    import kbserve              # the Q&A knowledge base, /polapps/s/s.kb.pml.*
except ImportError:
    kbserve = None
try:
    import extmail              # mail to/from the internet; inert until configured
except ImportError:
    extmail = None
try:
    import accounts             # account DB; optional -- absent = old stateless behaviour
except ImportError:
    accounts = None
try:
    # The plaintext profile/ranking channel on the same envelope -- see polpro.py.
    import polpro
except ImportError:
    polpro = None
try:
    # TESTER ISSUE REPORTS -- the report chord's landing pad. Optional in the
    # same way as everything else here: absent = the endpoint 503s and nothing
    # else on this door changes.
    import issuereport
except ImportError:
    issuereport = None
try:
    # THE PER-LOGIN CONTENT AUTH VALUE minted on 4:5 -- see contentauth.py.
    # Optional like the rest: absent, 4:5 echoes exactly as it always has.
    import contentauth
except ImportError:
    contentauth = None


# The server side of x-MD5-pol, solved by the shim worker and verified against the
# wire (pol_digest self-check passes both response and rspauth). Import if present;
# fall back to inline copies so the container never hard-depends on tools/.
try:
    import pol_digest  # noqa: E402  (tools/ is on the path in some layouts)
except ImportError:
    pol_digest = None
