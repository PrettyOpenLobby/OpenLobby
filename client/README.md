# Install-PolConnect: point a PC Viewer at your server

One script, one argument. From an elevated PowerShell (right-click
PowerShell, Run as administrator), in this folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\Install-PolConnect.ps1 -Server 203.0.113.10
```

Use the address you set as `POL_ADVERTISE` on the server. Then launch the
Viewer. `docs/client.md` covers the rest of client setup, including the
PlayStation 2.

## What it does

The Viewer is left byte-for-byte unmodified. Its shipped `env.dat` already
names every backend it uses (`pt008` patch, `wh000` portal, `ucs`, `ma000`,
`po000`, `gd001`/`gm001` GM), so pointing those names at a different machine
is enough on its own. There is no patched client, no injected DLL, and no
resolver running on the machine.

Two names are not in `env.dat`: `ci000.pol.com` (login directory) and the
`pp%03d.pol.com` lobby pool are built from templates inside `polcore.dll`.
That is why this is a hosts file rather than a regenerated `env.dat`: those
two can't be redirected any other way without modifying the client.

Everything written goes inside a marked block:

```
# >>> polconnect begin >>>
203.0.113.10    pt008.pol.com
...
# <<< polconnect end <<<
```

Re-running updates the block in place instead of appending, and `-Uninstall`
removes exactly what was added. The previous `hosts` is copied to
`hosts.polconnect-<timestamp>` before every write.

## Options

| Flag | Purpose |
|---|---|
| `-Server <addr>` | Server IPv4, or a hostname (resolved once, at install; re-run if it moves) |
| `-DryRun` | Print the changes and exit. Needs no elevation. |
| `-Uninstall` | Remove the block, restore any `env.dat` this script replaced |
| `-HostsList <path>` | Use a generated `polhosts.txt` instead of the built-in list |
| `-EnvDat <path>` | Also install a generated `env.dat` (see below) |

The Viewer install is found through
`HKLM\SOFTWARE\WOW6432Node\PlayOnline*\InstallFolder\1000`, across the US, EU
and JP keys. The hosts change applies even when no install is found.

## Keeping the host list current

The built-in list is a copy of what the server config redirects today. The
authoritative list is generated from `config/server.yaml`, so it can't drift
as hosts are added. From the repository root:

```
python tools/gen_hosts.py --names-only -o client/polhosts.txt
```

A `polhosts.txt` next to the script is picked up automatically; `-HostsList`
names one somewhere else. Hand players the two files together.

## When you need `-EnvDat`

Only if you want the client to reach your server by its own domain rather
than by redirected `pol.com` names, which needs a domain of your own. Generate
the file on the server side from the `env.dat` your own Viewer install shipped
with (it is under the Viewer's `data\utf\`). `--dump` lists the keys it holds;
each `--set` replaces one:

```
python tools/gen_env.py <shipped env.dat> --dump
python tools/gen_env.py <shipped env.dat> -o env.dat \
    --set POL_MAINMENU_URL=http://portal.example.org/pml/main/index.pml
```

The two template names above still need the hosts block whatever `env.dat`
says.

`gen_env.py` re-encrypts and verifies the round trip; a file reporting
`verify: ok` is one the client's loader will accept. The plain hosts setup
does not need this.

## Limits

- It needs elevation, once. The hosts file is not user-writable.
- It is machine-wide. Every user and every process on that PC resolves those
  names to your server until `-Uninstall`.
- The address is written literally. Passing a hostname resolves it at install
  time; a hosts file cannot alias one name to another. If the server moves,
  re-run.
- Close the Viewer before running it, or restart the Viewer afterwards. The
  script warns when `pol.exe` is running.
