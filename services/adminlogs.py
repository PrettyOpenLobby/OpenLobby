"""Settings > Logs: one area of the server's logs as a text file, to send with a report. Owner
only (admin.py keeps the routes out of every moderator permission).

    GET /api/logexport/areas                         {"areas": [{"id", "name"}], "lines": bool}
    GET /api/logexport?area=<id>&lines=N&addresses=1  the file

Where the logs come from:

  1. the deployer, when one is set up (a self-hosted server): GET /api/logs/export, which
     blanks secrets and shortens players' addresses itself (deployer/logexport.py);
  2. else the host's ops runner (prod's pol-ops): each group it lists, asked for and read
     back as admin.py's game Logs tab does (adminops.logs_request / logs_read). It keeps a
     fixed number of lines, so "lines" is not offered; its own redaction blanks secrets, and
     addresses are masked here unless asked for.

"all" is every area in one file.
"""
import time

import admindeploy
import adminops

OPS_WAIT = 25.0          # seconds to wait for the runner to write one group's logs
OPS_NAMES = {"patch": "Patch service"}


def _ops_areas(data_dir):
    st = adminops.ops_status(data_dir)
    if st is None or st["stale"]:
        return None
    names = {c.get("id"): c.get("name") for c in st.get("checks") or [] if isinstance(c, dict)}
    return [{"id": g, "name": names.get(g) or OPS_NAMES.get(g) or g} for g in st.get("logs") or []]


def areas(data_dir):
    """(status, {"areas", "lines", "source"}); 503 when nothing here can read logs."""
    if admindeploy.configured() is not None:
        st, out = admindeploy.log_areas()
        if st == 200 and isinstance(out.get("areas"), list):
            return 200, {"areas": [{"id": a["id"], "name": a["name"]} for a in out["areas"]
                                   if isinstance(a, dict) and a.get("id")],
                         "lines": True, "source": "deployer"}
        return st, {"error": out.get("error") or "Couldn't get the log list. Try again."}
    got = _ops_areas(data_dir)
    if got is None:
        return 503, {"error": "No logs on this server."}
    return 200, {"areas": got, "lines": False, "source": "ops"}


def _ops_group(data_dir, group, actor, addresses):
    """One group's logs from the runner, freshly asked for, as text lines."""
    asked = time.time()
    ok, msg = adminops.logs_request(data_dir, group, actor)
    if not ok:
        return ["(%s)" % msg]
    end = asked + OPS_WAIT
    doc = None
    while time.time() < end:
        doc = adminops.logs_read(data_dir, group, addresses)
        if doc and float(doc.get("at") or 0) >= asked - 1:
            break
        time.sleep(0.5)
    else:
        return ["(the logs did not arrive in time; try again)"]
    out = []
    for c in doc.get("containers") or []:
        out += ["", "-- %s" % c.get("name")]
        if c.get("missing"):
            out.append("(not running here)")
            continue
        out += [str(ln) for ln in c.get("lines") or []] or ["(no log)"]
    return out


def export(data_dir, area, lines, addresses, actor):
    """(200, bytes, file name) or (status, {"error"}, None)."""
    stamp = time.strftime("%Y%m%d-%H%M", time.gmtime())
    name = "openlobby-logs-%s-%s.txt" % (area if area.replace("-", "").replace("_", "").isalnum() else "logs", stamp)
    if admindeploy.configured() is not None:
        st, data = admindeploy.log_export(area, lines, addresses)
        return (st, data, name) if st == 200 else (st, data, None)
    have = _ops_areas(data_dir)
    if have is None:
        return 503, {"error": "No logs on this server."}, None
    by_id = {a["id"]: a for a in have}
    want = have if area == "all" else [by_id[area]] if area in by_id else None
    if not want:
        return 400, {"error": "No logs for that."}, None
    text = ["OpenLobby logs, %s" % time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "Areas: %s" % ", ".join(a["name"] for a in want),
            "Secrets are blanked%s." % ("" if addresses else ", and addresses hidden")]
    for a in want:
        text += ["", "=" * 78, a["name"], "=" * 78] + _ops_group(data_dir, a["id"], actor, addresses)
    return 200, ("\n".join(text) + "\n").encode("utf-8"), name
