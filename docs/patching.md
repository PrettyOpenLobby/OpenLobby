# Patching

Before the Viewer logs in, and before a title starts, the client asks a patch
server whether it is up to date. The `patch` service in `docker-compose.yml`
(`services/polserver2.py`) answers those checks. This page covers what it does
out of the box, how to give it real patch trees built from your own installed
client, and how to check the result.

## With no patch trees

The compose file starts the patch service with `POLP_CURRENT_ALL=1`. With that
set, every version check for a tree that is not under `mirrors/` is answered
"you are up to date" by echoing back the version the client sent. No update is
offered, so the client never asks for a file list, and the Viewer and every
title launch with no patch files at all.

A few related settings, all read from `.env`:

- `POLP_CURRENT_ALL=0` turns that off. A check for a tree you do not have is
  then rejected, which the client reports as a dead patch server (POL-1160 or
  POL-1161 depending on where it happens). To answer only some trees as
  current, list them in `POLP_CURRENT` as `REGION/PROD` pairs, for example
  `W2U/1000,W2U/0001`. With `POLP_CURRENT_ALL=0`, no `POLP_CURRENT` and an
  empty `mirrors/`, the service exits at startup and says why.
- `POLP_FALLBACK_VERSION` is the Viewer version (product 1000 only) reported
  when a check arrives with an empty version, which a Viewer logging in again
  sometimes sends. Set it to the Viewer build you support, for example
  `20130104_0`.
- `POLP_CONSOLE_LIST_CAP` (default `20130601_E`) leaves rows newer than that
  version out of the file lists of any PS2, P2U, X2U or XB2 Viewer tree you
  supply. It has no effect on trees you build for titles.

## Ports and service codes

Each patch tree is identified by a region code and a four-digit product code,
both of which the client sends inside every request. The port is derived from
the product: the Viewer itself (product `1000`) is on 54000 for every region,
and a title is on 53000 plus its content id.

| region | client |
| --- | --- |
| `W2U` | Windows, North America and Europe |
| `W20` | Windows, Japan |
| `P2U` | PlayStation 2, North America |
| `PS2` | PlayStation 2, Japan |
| `X2U` | Xbox 360, North America and Europe |
| `XB2` | Xbox 360, Japan |

| product | title | port |
| --- | --- | --- |
| `1000` | PlayOnline Viewer | 54000 |
| `0001` | FINAL FANTASY XI | 53001 |
| `0002` | Tetra Master | 53002 |
| `0003` | Janhourou | 53003 |
| `0004` | Front Mission Online | 53004 |
| `0011` | Fantasy Earth | 53011 |
| `0014` | PlayOnline Friend List | 53014 |

The compose file publishes 54000 and 53001-53015. A tree on any other port
needs that port added to the `patch` service's `ports:` list.

The client finds the patch server by name: `pt008.pol.com` (and the other
`ptNNN` names) for the Viewer, and `pc<id><region>.pol.com` for a title, for
example `pc002w2u.pol.com` for Tetra Master on a Western PC. The bundled DNS
service only answers for the names listed under `dns.redirect_only` in
`config/server.yaml`, so check that the names for any title you serve are
there. The version-check reply then tells the client which address to download
from, and the client dials that address directly. Set `POL_ADVERTISE` in
`.env` to an address the client can reach.

## Building a bundle from your own install

A patch tree served by `polserver2.py` is a directory called a bundle. You can
build one from a title you have installed with `tools/patch/polsynth.py`. Run
the tools from a checkout of this repository on any machine with Python 3 and
read access to the install. numpy is used when it is installed and makes the
checksum step much faster on large titles; it is optional.

Audit the install against its own inventory first. Most titles keep a
`file.txt` in their install directory listing every file with its size and
hash, and this checks that what is on disk still matches it:

```
python tools/patch/polsynth.py --source "D:/Games/PlayOnline/SquareEnix/TetraMaster" --verify-list
```

A damaged or missing file is listed and the exit code is 1. Fix the install
before building from it, or the damage is published to every client.

Then build the bundle into the mirrors directory:

```
python tools/patch/polsynth.py --source "D:/Games/PlayOnline/SquareEnix/TetraMaster" \
    --out mirrors/W2U-0002 --region W2U --product 0002
```

What the options do:

- `--version` is the single version the bundle publishes. The default, `auto`,
  reads it from the install's own `patch.ver`, which is what the client
  believes it is running. A title that has never been patched stores
  `0000000000` there, which the protocol does not accept as a version; pass
  `--version YYYYMMDD_0` yourself in that case, using a date the title's own
  files give you.
- `--alias W20` makes the same bundle answer for a second region code. Only do
  this when the two regions' installs really are the same files.
- `--walk` takes every file under `--source` when there is no `file.txt`. Use
  `--exclude` to leave out anything that is per-user (saved settings, IDs).
  `patch.ver` is always left out, because it is per-install client state and
  is encrypted under that machine's install key.
- `--port` overrides the default of 53000 plus the product code.

A bundle built this way carries one version and every file whole, so it can
bring a client from any earlier version to that one in a single step. It
cannot install a title from nothing: `file.txt` lists the data files, and the
executables and launchers belong to the title's installer.

## What is in a bundle

```
mirrors/
  W2U-0002/
    meta.json
    patch.cfg
    patchlist.raw
    manifest.json
    blobs/<version>/Direct/<path>.slc
    blobs/<version>/Indirect/<path>.olc
```

The directory name is only a convention; the server reads the region, product
and port from `meta.json`. A directory is loaded as a bundle when it contains
`patchlist.raw`.

`meta.json` holds `region`, `product` and `port`; `latest_version`, which the
server reports as the newest build in its version-check reply; and
`oldest_version`, the floor the reply's status string is computed against. A
client claiming a version at or above `oldest_version` is told "registered",
meaning this tree can patch it from there. polsynth sets the floor to
`00000000_0` because its bundles ship every file whole. `cmd8_token` is a unix
timestamp sent in the same reply, and `region_aliases` lists extra region codes
the bundle answers for. The remaining fields are counts and a `synthesised`
record of where the bundle came from; the server does not need them.

`patch.cfg` is the plaintext file index. It is a list of blocks, one per file,
followed by `end`:

```
file data/gP001.dat {
20030429_0 150000 -13764 -1495958702 20030429_0/Direct/data/gP001.dat.slc 1702
20040714_0 150212 -13690 1188232871 20040714_0/Direct/data/gP001.dat.slc 1711
}

end
```

Each row is one version of that file: the version, the file's size in bytes,
the sum of its bytes read as signed 8-bit values, the first four bytes of its
MD5 read as a little-endian 32-bit integer (written signed), the path of the
blob that carries it, and that blob's size. A row can also name an Indirect
blob (`.olc`, a delta against the previous version) and its size; polsynth and
polpublish write Direct blobs only. The client uses the size and both checksums
to verify every file it downloads. Paths may contain spaces.

`patchlist.raw` is the same text compressed into an `.slc` container (method
2, below). This is what the server sends for the file-list request, as is. The
tools keep it in step with `patch.cfg`; if you edit `patch.cfg` by hand, it is
`patchlist.raw` that clients see.

`manifest.json` is `patch.cfg` parsed into JSON, one object per row. It is a
cache and can be deleted; `services/polbundle.py` rebuilds the rows from
`patch.cfg` when it is missing, and `python services/polbundle.py <bundle>`
writes it again.

Each blob is an `.slc` container: one method byte followed by the payload.
Method 1 is the file stored as is, method 3 is a zlib stream, and method 2 is
a bit-level LZSS used for the file list. polsynth picks whichever of 1 and 3 is
smaller for each file. The blob's path under `blobs/` is exactly the path the
client asks for, taken from `patch.cfg`.

## Where to put it

The `patch` service mounts `${POL_MIRRORS:-./mirrors}` read-only at `/mirrors`
and serves every bundle it finds there. Build into `mirrors/` next to
`docker-compose.yml`, or set `POL_MIRRORS` in `.env` to wherever your bundles
live. Bundles are read once at startup, so after adding or changing one:

```
docker compose restart patch
docker compose logs patch
```

The startup log lists each bundle it loaded with its port and latest version.
To check that every blob a bundle's index names is present and the right size:

```
python services/polserver2.py mirrors --check
```

A tree you serve is answered from the bundle; every other tree still gets the
"up to date" answer while `POLP_CURRENT_ALL=1`.

## Adding a version

`tools/patch/polpublish.py` adds a new version on top of an existing bundle:
a newer build of the title, a translation, or a fix of your own. Clients that
already have the previous version then download only the files that changed.

```
python tools/patch/polpublish.py --bundle mirrors/W2U-0002 --status
python tools/patch/polpublish.py --bundle mirrors/W2U-0002 --version 20040714_0 \
    --from "D:/Games/newer/TetraMaster" --dry-run
python tools/patch/polpublish.py --bundle mirrors/W2U-0002 --version 20040714_0 \
    --from "D:/Games/newer/TetraMaster"
```

`--from` takes a whole newer copy of the title and publishes every file whose
bytes differ from what the bundle currently serves. `--overlay DIR` takes only
the changed files, laid out as they sit in the install. A file the bundle did
not have before is added and reported, since in an `--overlay` run that is
usually a typo in a path. There is no way to delete a file through a patch
list, so a file missing from the source is reported and stays.

The new version must sort after the bundle's current latest. The client
compares versions as strings, so publishing an older one would tell every
client it is already up to date. Adding an older build after newer ones is
what `--backfill` is for; it adds the rows and leaves `latest_version` alone.

If you change files that the title's `file.txt` describes, publish a matching
`file.txt` with them, or the client's Check Files will see a mismatch and put
the old files back. `tools/patch/polmanifest.py` writes one from what the
bundle will serve:

```
python tools/patch/polmanifest.py --bundle mirrors/W2U-0002 \
    --base "D:/Games/PlayOnline/SquareEnix/TetraMaster/file.txt" \
    --overlay stage/tetra --write
python tools/patch/polpublish.py --bundle mirrors/W2U-0002 --version 20040714_0 \
    --overlay stage/tetra
```

Restart the patch service after publishing.

### Reverting

The first publish onto a bundle copies `patch.cfg`, `patchlist.raw`,
`meta.json` and `manifest.json` to `<name>.prepublish`, and later publishes
leave those copies alone. `--revert` restores them and deletes the blobs of
every version added since. That removes all the versions you have published
with polpublish, back to the bundle as polsynth built it. It does not undo
only the last one.

Always look first:

```
python tools/patch/polpublish.py --bundle mirrors/W2U-0002 --revert --dry-run
```

This lists every version that would be removed and changes nothing. To remove
just the latest of several versions, keep a copy of the bundle directory from
before you published it and put that back.

## Checking the result

`tools/patch/polselfcheck.py` talks to a running patch server the way a client
does: it asks for the version, fetches the file list, then downloads every
blob in ranged reads over one connection and checks each one against the
list. With `--reference` it also compares the newest version of every file
with the install the bundle was built from. Use `--reference`: without it the
check only proves the bundle agrees with itself.

Run a separate copy of the server for the check, on ports that do not clash
with the one in Docker. `--bind-offset` adds a number to every port:

```
python services/polserver2.py mirrors --advertise 127.0.0.1 --bind-offset 1000 --quiet
python tools/patch/polselfcheck.py --bundle mirrors/W2U-0002 --port 54002 \
    --reference "D:/Games/PlayOnline/SquareEnix/TetraMaster"
```

Port 54002 here is the bundle's port, 53002, plus the offset. A good bundle
ends with `[PASS] 0 problem(s)`; anything else lists each file and what was
wrong with it. Run that server without `--pin`, `--list-cap`, `--slim-list`
or `POLP_CONSOLE_LIST_CAP`, since those change the replies on purpose, and
restart it after every publish, since it reads `latest_version` at startup.
Stop it when you are done.
