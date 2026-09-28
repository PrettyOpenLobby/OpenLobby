"""Binding the title plugins to the core and merging their tables."""
import titles                   # the title-plugin seam (services/titles.py)  # noqa: E402
import clientbuilds
from srvcore import log
from .deps import accounts, polpro
from . import authcap, authnode, chatsession, contentprofiles, fetchpath, gamenotice, handlelists, ircband, lobbymail, lobbyrooms, lobbysession, pacing, paylen, pfc, presence, resourcestore, roomregistry, titlezone



# --------------------------------------------------------------------------- #
# Title plugins (services/titles.py)
# --------------------------------------------------------------------------- #
# The core hands each title the plumbing it may use, then loads the modules
# named in POL_TITLES. A title's constant-length resource fetches, its fresh
# resource blobs and its POLpro reply templates merge into the core's tables
# here, once, at import -- so a build with no titles runs the generic paths
# and a build with one runs exactly the code that used to be inline.
titles.bind_core(
    log=log, NoPad=authnode.NoPad, PRESENCE=presence.PRESENCE, ROOMS=roomregistry.ROOMS, accounts=accounts,
    polpro=polpro, RESOURCE_DIR=resourcestore.RESOURCE_DIR,
    _game_notice_line=gamenotice._game_notice_line, _irc_host=ircband._irc_host,
    _session_get=lobbysession._session_get, _session_sid=lobbysession._session_sid,
    _session_handle_id=lobbysession._session_handle_id, _sess_member_id=chatsession._sess_member_id,
    _member_content_id=contentprofiles._member_content_id, _member_display_name=handlelists._member_display_name,
    _member_primary_handle=handlelists._member_primary_handle, _live_rooms=lobbyrooms._live_rooms,
    _room_of_member=roomregistry._room_of_member, _resource_file=resourcestore._resource_file,
    _resource_read_file=resourcestore._resource_read_file, _resource_stored=resourcestore._resource_stored,
    _fetch_subject=fetchpath._fetch_subject, _peer_is_ps2=pacing._peer_is_ps2, _self_ip=authcap._self_ip,
    _mail_mint=lobbymail._mail_mint, _member_still_present=presence._member_still_present,
    _title_zone=titlezone._title_zone, _title_zone_lease=titlezone._title_zone_lease,
    _content_profiles=pfc._content_profiles, _peer_build=pacing._peer_build,
    _client_builds=clientbuilds.for_address)
_TITLES_LOADED = titles.load()
paylen._FETCH_PATHLEN.update(titles.fetch_pathlen())
resourcestore.RESOURCE_INIT.update(titles.resource_init())
resourcestore._RESOURCE_WRITE_LEN.update(titles.resource_write_len())
resourcestore._RESOURCE_WRITE_PATHS = resourcestore._RESOURCE_WRITE_PATHS + tuple(
    p for p in resourcestore._RESOURCE_WRITE_LEN if not p.startswith(resourcestore._RESOURCE_WRITE_PATHS))
if polpro is not None:
    polpro.EXTRA_SPEC_FILES.extend(titles.polpro_spec_files())
