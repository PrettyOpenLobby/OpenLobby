"""The OpenLobby core, one module per concern.

    deps.py                Imports shared by the core modules: the optional service modules (accounts, polpro, ...) and the srvcore/authtoken names the old responders.py re-exported.
    patch.py               POLP patch responder (TCP 54000): the version check and its canned reply.
    redirect.py            IRC redirect tokens and login nicks: what the directory hands a client to reach the auth node.
    logingate.py           Login refusal and login-completion gate records.
    directory.py           Login directory responder (TCP 51240): redirects the client to the auth node.
    authcap.py             Auth node capture harness, and the address this server advertises.
    authnode.py            Auth node constants: the token-0 key trick, the session cipher IV, NoPad reply lines.
    pfc.py                 Content profiles and the PFC profile reply on the auth band.
    presence.py            Presence registry: which members are online, logout grace, presence status payloads.
    gamenotice.py          Game envelopes (NOTICE G<tag>G...) on the auth band: title dispatch and the POLpro classes.
    chatsession.py         ChatSession: one client's auth-band connection and its cipher state.
    roomregistry.py        RoomRegistry: the IRC-style chat rooms and who stands in them.
    friendroster.py        Sending a member's friend roster and slot layout on the auth band.
    pushchannel.py         The push channel: how live updates reach a client on its auth band.
    pushrecord.py          The field-list push record that paints a friend's picture, and presence broadcasts.
    memberstatus.py        Member status (online, away, invisible) as published across processes and framed in pushes.
    titlezone.py           Which title a member is in: leases, publishing across processes, expiry.
    pushspool.py           The push spool: cross-process delivery of pushes and their watchers.
    ircband.py             The auth band's IRC verbs (JOIN, PART, NOTICE, PRIVMSG, ...) and the XXL gate.
    authserv.py            The auth node responder: line I/O, account resolution, the login exchange.
    authresume.py          The session channel loop and re-attaching a session after an authserv restart.
    authkick.py            Administrator kick: the admin panel's request to disconnect a PlayOnline ID's live channels.
    lobbyrefuse.py         Refusing a lobby request: the error type a handler answers with instead of success.
    framing.py             Lobby band framing: headers, frame boundaries, reading one frame.
    lobbysession.py        Per-connection lobby session state, its on-disk mirror, and the session record.
    paylen.py              Lobby reply lengths per opcode and the constant-length fetch path tables.
    fetchpath.py           Resource fetch subjects and paths.
    lobbysearch.py         Member search: calibration, result payloads, TLV parsing.
    friendput.py           The friend list write (lobby 2:6): parsing, applying, replying.
    lobbybind.py           Which member a lobby connection belongs to: IV recovery, binding, arbitration, checksums.
    lobbyreply.py          Building a lobby reply: the opcode dispatch, framing, encryption, probe payloads.
    lobbyops.py            The lobby opcode table: one row per request opcode, naming what answers it.
    handlelists.py         Handle, character and list payloads served on the lobby band.
    friendgroups.py        Friend groups: configuration, membership, create/delete/class change, join and invite.
    characters.py          Character records and the character write (1:A).
    friendlist.py          The friend list as served: records, slot map, database rows.
    lobbymail.py           POL Message mail on the lobby band: mailbox payloads, minting, threads, notices.
    contentprofiles.py     Per-title content schemas, content codes and content profile records.
    profilerecord.py       The member profile record and profile TLV writes.
    lobbyrooms.py          Rooms as the lobby browses them: created, persistent, published and restored rooms.
    resourcestore.py       The resource store: blobs the client fetches and writes under /data/resources.
    pacing.py              Reply pacing: delays, linger, and PS2 burst pacing on the lobby band.
    lobbycapture.py        Capturing lobby writes: binding corroboration, active handle, the write dispatch.
    tlsrelay.py            Relaying TLS hellos and UCS CGI requests that arrive on the lobby band.
    portalauth.py          Portal HTTP authentication (x-MD5-pol), validators and cache headers.
    portalpages.py         Portal eras: which client build gets which pages; shim logs.
    lobbyserver.py         The lobby responder: HTTP on the lobby band and handle_lobby.
    worldserver.py         The world responder the lobby hands off to.
    serving.py             Listener plumbing: accepting connections and the per-connection thread.
    mailserver.py          POP3 and SMTP: the Viewer's mail client against the mail table.
    posture.py             Auth posture logging and the front-relay watchdog.
    boot.py                Binding the title plugins to the core and merging their tables.
    main.py                Entry point: modes, ports, and the listener threads.

responders.py (one directory up) is the entry point and the compatibility
facade over these modules.
"""

#: the modules above, in import order (deps first, boot and main last)
MODULES = (
    "deps",
    "patch",
    "redirect",
    "logingate",
    "directory",
    "authcap",
    "authnode",
    "pfc",
    "presence",
    "gamenotice",
    "chatsession",
    "roomregistry",
    "friendroster",
    "pushchannel",
    "pushrecord",
    "memberstatus",
    "titlezone",
    "pushspool",
    "ircband",
    "authserv",
    "authresume",
    "authkick",
    "lobbyrefuse",
    "framing",
    "lobbysession",
    "paylen",
    "fetchpath",
    "lobbysearch",
    "friendput",
    "lobbybind",
    "lobbyreply",
    "lobbyops",
    "handlelists",
    "friendgroups",
    "characters",
    "friendlist",
    "lobbymail",
    "contentprofiles",
    "profilerecord",
    "lobbyrooms",
    "resourcestore",
    "pacing",
    "lobbycapture",
    "tlsrelay",
    "portalauth",
    "portalpages",
    "lobbyserver",
    "worldserver",
    "serving",
    "mailserver",
    "posture",
    "boot",
    "main",
)
