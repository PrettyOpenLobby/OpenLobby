<#
.SYNOPSIS
    Point a PlayOnline Viewer install at a private server.

.DESCRIPTION
    Writes the server's address into the Windows hosts file for every name the
    Viewer and its titles resolve, and nothing else.

    The Viewer is left completely unmodified. Its shipped env.dat already names
    every backend host (pt008, wh000, ci000, pp000, ...), so redirecting those
    names is sufficient on its own: no patched client, no injected DLL, no
    resolver running on the machine.

    Two of those names, ci000.pol.com (login) and pp000.pol.com (lobby), are
    hardcoded in polcore.dll rather than read from env.dat, which is why a hosts
    file is the mechanism rather than a regenerated env.dat.

    Everything written goes inside a marked block, so -Uninstall is exact and
    re-running is idempotent rather than additive.

.PARAMETER Server
    The server's address. An IPv4 literal is used as-is; a hostname is resolved
    once, now, and its address written (hosts cannot alias name to name). If
    the server later moves, re-run this script.

.PARAMETER EnvDat
    Optional. A generated env.dat (see tools/gen_env.py) to install
    into the Viewer's data\utf\. Only needed when the server is reached by its
    own domain rather than by redirected pol.com names; the plain hosts setup
    does not require it. The existing file is backed up first.

.PARAMETER HostsList
    Optional path to a polhosts.txt (see tools/gen_hosts.py). When omitted, a
    polhosts.txt next to this script is used if there is one, else the
    built-in list below.

.PARAMETER Uninstall
    Remove the block and restore any env.dat this script replaced.

.PARAMETER DryRun
    Print what would change and exit without writing.

.EXAMPLE
    .\Install-PolConnect.ps1 -Server 203.0.113.10

.EXAMPLE
    .\Install-PolConnect.ps1 -Uninstall

.NOTES
    Requires an elevated PowerShell: the hosts file is not user-writable.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Server,
    [string]$EnvDat,
    [string]$HostsList,
    [switch]$Uninstall,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$BeginMark = '# >>> polconnect begin >>>'
$EndMark   = '# <<< polconnect end <<<'
$HostsPath = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'

# Fallback list, used when no polhosts.txt is supplied. Keep in sync with
# tools/gen_hosts.py, which generates the authoritative version from
# config/server.yaml:  python tools/gen_hosts.py --names-only -o client/polhosts.txt
$DefaultNames = @(
    # patch servers: the Viewer's version check, then one per title
    'pt008.pol.com', 'pt002.pol.com', 'pt004.pol.com', 'pt007.pol.com'
    'pt011.pol.com', 'pt012.pol.com'
    'pc001w2u.pol.com', 'pc001w20.pol.com', 'pc015w2u.pol.com', 'pc015w20.pol.com'
    'pc002w2u.pol.com', 'pc002w20.pol.com', 'pc002ps2.pol.com', 'pc002p2u.pol.com'
    'pc003ps2.pol.com', 'pc003p2u.pol.com', 'pc001ps2.pol.com', 'pc001p2u.pol.com'
    'pc004ps2.pol.com', 'pc004p2u.pol.com', 'pc010ps2.pol.com', 'pc010p2u.pol.com'
    'pc004w20.pol.com', 'pc004w2u.pol.com', 'pc011w20.pol.com', 'pc011w2u.pol.com'
    'pc014w20.pol.com', 'pc014w2u.pol.com'
    # login directory, sign-up and account hosts
    'ci000.pol.com', 'userctl.pol.com', 'ucsgate.pol.com'
    'account.square-enix.com', 'secure.square-enix.com'
    # title world servers
    'gi000.pol.com', 'gi003.pol.com'
    'fmo00.pol.com', 'fmo01.pol.com', 'fmo02.pol.com', 'fmo03.pol.com', 'fmo04.pol.com'
    'f-earth00.pol.com', 'ffxi00.pol.com', 'ffxi015.pol.com', 'kel-1001.pol.com'
    # login pool, GM service, console network check
    'pp000.pol.com', 'pp001.pol.com', 'pp002.pol.com', 'pp003.pol.com'
    'gd000.pol.com', 'gd001.pol.com', 'gm000.pol.com', 'gm001.pol.com'
    'gate1.jp.dnas.playstation.org', 'gate1.us.dnas.playstation.org'
    # portal, sign-up, mail, server info
    'wh000.pol.com', 'ucs.pol.com', 'usercte.pol.com'
    'ma000.pol.com', 'po000.pol.com'
    'info.playonline.com', 'info.sqex.net'
)

# A polhosts.txt shipped next to this script wins over the built-in list.
if (-not $HostsList -and $PSScriptRoot) {
    $side = Join-Path $PSScriptRoot 'polhosts.txt'
    if (Test-Path $side) { $HostsList = $side }
}

function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-ViewerPath {
    # InstallFolder is keyed by content id; 1000 is the Viewer itself. Checked
    # across all three regional hives, since a machine may have any of them.
    $hives = @('PlayOnlineUS', 'PlayOnlineEU', 'PlayOnline')
    foreach ($h in $hives) {
        foreach ($w in @('HKLM:\SOFTWARE\WOW6432Node', 'HKLM:\SOFTWARE')) {
            $k = "$w\$h\InstallFolder"
            if (Test-Path $k) {
                $v = (Get-Item $k).GetValue('1000')
                if ($v -and (Test-Path $v)) { return $v.TrimEnd('\') }
            }
        }
    }
    return $null
}

function Get-Names {
    if ($HostsList) {
        if (-not (Test-Path $HostsList)) { throw "no such hosts list: $HostsList" }
        $out = @()
        foreach ($line in Get-Content $HostsList) {
            $t = $line.Trim()
            if (-not $t -or $t.StartsWith('#')) { continue }
            # Accept both "name  # why" and "addr name  # why".
            $t = ($t -split '#')[0].Trim()
            $parts = $t -split '\s+'
            if ($parts.Count -ge 2) { $out += $parts[1] } else { $out += $parts[0] }
        }
        return $out
    }
    return $DefaultNames
}

function Read-HostsLines {
    if (Test-Path $HostsPath) { return @(Get-Content $HostsPath) }
    return @()
}

function Remove-Block([string[]]$lines) {
    $out = @()
    $inside = $false
    foreach ($l in $lines) {
        if ($l.Trim() -eq $BeginMark) { $inside = $true; continue }
        if ($l.Trim() -eq $EndMark)   { $inside = $false; continue }
        if (-not $inside) { $out += $l }
    }
    # Trim trailing blank lines so repeated runs do not grow the file.
    while ($out.Count -gt 0 -and $out[-1].Trim() -eq '') {
        $out = $out[0..($out.Count - 2)]
    }
    return $out
}

function Backup-File([string]$path) {
    if (-not (Test-Path $path)) { return $null }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $bak = "$path.polconnect-$stamp"
    Copy-Item $path $bak -Force
    return $bak
}

# --- preflight ---------------------------------------------------------------

if (-not $Uninstall -and -not $Server) {
    throw "give -Server <address>, or use -Uninstall"
}
if (-not (Test-Elevated) -and -not $DryRun) {
    throw "must run elevated: the hosts file is not user-writable. Right-click PowerShell -> Run as administrator."
}

$viewer = Get-ViewerPath
if ($viewer) {
    Write-Host "Viewer install : $viewer"
} else {
    Write-Host "Viewer install : not found in registry (hosts changes still apply)" -ForegroundColor Yellow
}

$running = @(Get-Process -Name 'pol' -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    Write-Host "NOTE: pol.exe is running (pid $($running[0].Id)). Restart it after this completes." -ForegroundColor Yellow
}

# --- uninstall ---------------------------------------------------------------

if ($Uninstall) {
    $lines = Read-HostsLines
    $had = ($lines | Where-Object { $_.Trim() -eq $BeginMark }).Count -gt 0
    if (-not $had) {
        Write-Host "No polconnect block present; nothing to remove."
    } else {
        $new = Remove-Block $lines
        if ($DryRun) {
            Write-Host "[dry run] would remove the polconnect block from $HostsPath"
        } else {
            $bak = Backup-File $HostsPath
            Set-Content -Path $HostsPath -Value $new -Encoding ascii
            Write-Host "Removed the polconnect block (backup: $bak)" -ForegroundColor Green
        }
    }

    if ($viewer) {
        $envPath = Join-Path $viewer 'data\utf\env.dat'
        $saved = Get-ChildItem "$envPath.polconnect-*" -ErrorAction SilentlyContinue |
                 Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($saved) {
            if ($DryRun) {
                Write-Host "[dry run] would restore env.dat from $($saved.Name)"
            } else {
                Copy-Item $saved.FullName $envPath -Force
                Write-Host "Restored env.dat from $($saved.Name)" -ForegroundColor Green
            }
        }
    }

    if (-not $DryRun) { & ipconfig /flushdns | Out-Null }
    return
}

# --- resolve the server ------------------------------------------------------

$addr = $null
$parsed = [ref]([ipaddress]'0.0.0.0')
if ([ipaddress]::TryParse($Server, $parsed)) {
    $addr = $Server
} else {
    Write-Host "Resolving $Server ..."
    $r = @(Resolve-DnsName -Name $Server -Type A -ErrorAction SilentlyContinue |
           Where-Object { $_.IPAddress })
    if ($r.Count -eq 0) { throw "could not resolve '$Server' to an IPv4 address" }
    $addr = $r[0].IPAddress
    Write-Host "  -> $addr  (written literally; re-run this script if it changes)"
}

$names = Get-Names
if ($HostsList) { Write-Host "Host list       : $HostsList" }
Write-Host "Redirecting     : $($names.Count) hostnames -> $addr"

# --- conflict check ----------------------------------------------------------

$lines = Read-HostsLines
$stripped = Remove-Block $lines
$conflicts = @()
foreach ($l in $stripped) {
    $t = ($l -split '#')[0].Trim()
    if (-not $t) { continue }
    $parts = $t -split '\s+'
    if ($parts.Count -lt 2) { continue }
    foreach ($n in $names) {
        if ($parts[1..($parts.Count - 1)] -contains $n) { $conflicts += $l }
    }
}
if ($conflicts.Count -gt 0) {
    Write-Host "WARNING: entries for these names already exist outside the polconnect block:" -ForegroundColor Yellow
    $conflicts | Select-Object -First 10 | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
    Write-Host "  Windows uses the FIRST match, so those will win. Remove them if the client misbehaves." -ForegroundColor Yellow
}

# --- build and write ---------------------------------------------------------

$block = @($BeginMark,
           "# PlayOnline private server: $Server",
           "# Written $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'). Remove with Install-PolConnect.ps1 -Uninstall")
$width = ($names | Measure-Object -Property Length -Maximum).Maximum
foreach ($n in $names) { $block += ("{0,-15} {1}" -f $addr, $n) }
$block += $EndMark

$final = @()
$final += $stripped
if ($final.Count -gt 0) { $final += '' }
$final += $block

if ($DryRun) {
    Write-Host ""
    Write-Host "[dry run] would write to $HostsPath :" -ForegroundColor Cyan
    $block | Select-Object -First 8 | ForEach-Object { Write-Host "    $_" }
    Write-Host "    ... $($names.Count) entries total"
} else {
    $bak = Backup-File $HostsPath
    Set-Content -Path $HostsPath -Value $final -Encoding ascii
    Write-Host "Wrote hosts block (backup: $bak)" -ForegroundColor Green
}

# --- optional env.dat --------------------------------------------------------

if ($EnvDat) {
    if (-not (Test-Path $EnvDat)) { throw "no such env.dat: $EnvDat" }
    if (-not $viewer) { throw "-EnvDat given but the Viewer install was not found" }
    $dest = Join-Path $viewer 'data\utf\env.dat'
    if ($DryRun) {
        Write-Host "[dry run] would install $EnvDat -> $dest"
    } else {
        $bak = Backup-File $dest
        Copy-Item $EnvDat $dest -Force
        Write-Host "Installed env.dat -> $dest (backup: $bak)" -ForegroundColor Green
    }
}

if (-not $DryRun) {
    & ipconfig /flushdns | Out-Null
    Write-Host ""
    Write-Host "Done. Launch the PlayOnline Viewer and log in." -ForegroundColor Green
    Write-Host "Revert at any time with:  .\Install-PolConnect.ps1 -Uninstall"
}
