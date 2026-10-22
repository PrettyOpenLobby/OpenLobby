# .polpatch files

The patch service serves patch trees (bundles) that you build from a client
you have installed. A `.polpatch` is one of those bundles packed into a single
file, so you can build it on the PC where the title is installed and carry it
to the machine that runs your server. [patching.md](patching.md) explains what
a bundle is and what the patch service does with it; this page is about the
file.

A `.polpatch` holds Square Enix's client files. Use it for your own servers.
Do not share it or post it anywhere.

## What is inside

A `.polpatch` is an ordinary ZIP archive, so Windows can open it if you want
to look. It holds two things:

- `polpatch.json`, a short description: which title and region it is for,
  which versions it carries, and the size and SHA-256 checksum of every file
  in it.
- `bundle/`, the bundle itself, laid out exactly as the patch service reads
  it from `mirrors/<REGION>-<PRODUCT>/`.

There are two kinds. A full `.polpatch` is a whole bundle and can be installed
on a server that does not have that title yet. An update carries one new
version on top of a bundle the server already has, and only the files that
changed in it.

Versions are written `YYYYMMDD_X`, a date and one more letter or digit, for
example `20040714_0`. Newer versions sort after older ones.

## Three ways to make one

You need Python 3.9 or newer on the PC that has the title installed, and a
copy of this repository (download it as a ZIP from the repository page and
unpack it anywhere).

### Double-click on Windows

Open the `tools\patch` folder and double-click `polpatch.cmd`. You can also
drag the title's install folder onto it.

1. If Python is missing it says so and tells you where to get it. Tick
   "Add python.exe to PATH" when you install it, then run `polpatch.cmd`
   again.
2. Type or paste the title's install folder, or drag the folder into the
   window, and press Enter. For Tetra Master that is a folder such as
   `C:\Program Files (x86)\PlayOnline\SquareEnix\TetraMaster`.
3. Pick the title from the first list and the client and region from the
   second, by typing the number.
4. Press Enter to save the file next to `polpatch.cmd`, or type another
   folder.
5. The version is read from the install. If that does not work (a title that
   has never been patched does not record one), you are asked to type it.

The file is named after the region, product and version, for example
`W2U-0002-20040714_0.polpatch`. The window stays open until you press a key,
so you can read what it did.

If the install folder has no `file.txt` (the list of files most titles keep),
you are asked whether to take every file in the folder instead. Say yes only
if the folder holds nothing but the title: saved settings and IDs in it would
be sent to every client of your server.

### build-full on the command line

This does the same as the double-click route, with the choices given as
options. It runs on any system with Python:

```
python tools/patch/polpatch.py build-full --source "D:/Games/PlayOnline/SquareEnix/TetraMaster" \
    --region W2U --product 0002 -o TetraMaster-W2U.polpatch
```

The options are those of `polsynth.py` in [patching.md](patching.md):
`--version` (the default reads it from the install), `--alias` for a second
region code that uses the same files, `--walk` and `--exclude` when there is
no `file.txt`, and `--note` for a line of your own that is kept in the file.
Audit the install with `polsynth.py --verify-list` first, as patching.md
describes.

### from-bundle, for a bundle you already have

If you have already built a bundle in a `mirrors` folder, this packs it,
every version it holds, into one file:

```
python tools/patch/polpatch.py from-bundle --bundle mirrors/W2U-0002 -o W2U-0002.polpatch
```

## Making an update

To publish a new version on top of one you have already made, put the
changed and added files in a folder, laid out the way they sit in the
install, and build an update against the full file (or against the bundle
folder):

```
python tools/patch/polpatch.py build-update --base TetraMaster-W2U.polpatch \
    --files stage/tetra --version 20040714_0 -o TetraMaster-W2U-20040714_0.polpatch
```

The new version has to be newer than the one it is built on. The rules of
`polpublish.py` apply: a file cannot be deleted through a patch, and if you
change files that the title's `file.txt` describes, publish a matching
`file.txt` with them (see "Adding a version" in patching.md).

## Checking a file

```
python tools/patch/polpatch.py inspect TetraMaster-W2U.polpatch
python tools/patch/polpatch.py verify TetraMaster-W2U.polpatch
```

`inspect` shows the title, region, versions and size without reading the
whole file. `verify` reads all of it: it checks every file against its
checksum, checks that the bundle is complete and consistent, and unpacks the
files the clients will download to make sure they match the patch list. It ends
with `[PASS]`, or lists each problem and exits with code 1. Every file the
build commands write has already passed this check.

## Importing it on your server

Copy the file to the machine that runs the server. The admin panel will offer
an import for it; until then, or if you prefer the command line, use
`polpatch.py apply` from your copy of this repository:

```
python tools/patch/polpatch.py apply TetraMaster-W2U.polpatch --mirrors mirrors
docker compose restart patch
```

`--mirrors` is the folder the patch service serves: `mirrors/` next to
`docker-compose.yml`, or wherever `POL_MIRRORS` in `.env` points. The patch
service reads its bundles once at startup, hence the restart.

`apply` runs the same checks as `verify` and changes nothing if any of them
fails. It unpacks the file into a temporary folder inside the mirrors folder,
checks it again there, and only then moves it into place. What else it
refuses, each time with a sentence that says why:

- an update for a title the server does not have yet (import the full file
  first);
- an update that is not built on the version the server has now. Import
  updates in the order they were made;
- the same update twice;
- a full file that does not include every version the server already has.
  Importing never removes a version.

Before an update replaces the bundle's `patch.cfg`, `patchlist.raw`,
`meta.json` and `manifest.json`, the old copies are saved under
`mirrors/.polpatch-backup/`. The patch service ignores that folder.

Then check the result the way patching.md describes under "Checking the
result".

## Limits

Because a `.polpatch` may come from somewhere you do not fully trust, `verify`
and `apply` refuse anything that is not a bundle: entries outside `bundle/`,
paths that climb out of it, symbolic links, files the description does not
list, and files whose size or checksum differ from it. They also refuse a file
that unpacks to more than 16 GiB in all, holds a single file over 4 GiB, or
holds more than 500000 files. For a larger title, raise the limits with
`--max-total-gib`, `--max-file-gib` and `--max-entries`.
