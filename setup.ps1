<#
.SYNOPSIS
    First-run setup for an OpenLobby server on Windows with Docker Desktop.

.DESCRIPTION
    Checks git and Docker Desktop, clones OpenLobby when it is not run from a
    checkout, asks who will play and proposes the address settings for that,
    writes .env, starts the core stack, sets the admin panel's password and
    prints what to do next. Run again, it keeps the .env it finds.
    docs/setup.md explains every step.

    The deployer (installing games from the admin panel) needs Docker Engine
    on Linux and is not set up here. Run games with python tools\stack.py
    (docs/games.md), or run setup.sh on a Linux machine, a WSL2 distribution
    with Docker Engine, or a VPS.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Yes -Reach lan -DryRun
#>
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$DryRun,
    [string]$Dir,
    [string]$Repo = 'https://github.com/PrettyOpenLobby/OpenLobby.git',
    [ValidateSet('', 'lan', 'vpn', 'internet')]
    [string]$Reach = '',
    [string]$Address,
    [string]$PublicAddress,
    [switch]$LookupPublic,
    [switch]$Ps2,
    [switch]$NoPs2,
    [ValidateSet('', 'published', 'build')]
    [string]$Images = '',
    [switch]$ChangeAddress,
    [switch]$InstallTools,
    [switch]$InstallClient,
    [switch]$NoClient,
    [switch]$NoStart,
    [switch]$Help
)

# Native commands report through exit codes; 'Stop' would turn their stderr
# into terminating errors on Windows PowerShell 5.1.
$ErrorActionPreference = 'Continue'
Set-StrictMode -Version 2

$LookupUrl = 'https://api.ipify.org'

if ($Help) {
    Get-Help -Detailed $MyInvocation.MyCommand.Path
    @'
Options:
  -Yes              take the default for every question (no prompts)
  -DryRun           show what would be done; change nothing
  -Dir DIR          the OpenLobby checkout to use or create
                    (default: the checkout this script is in, else
                    %USERPROFILE%\openlobby-server\openlobby)
  -Repo URL         clone from URL instead of the GitHub repository
  -Reach lan|vpn|internet
                    who plays: this network only, friends over a VPN, or
                    anyone over the internet
  -Address ADDR     the address clients reach this machine on
  -PublicAddress ADDR  the public address (internet case)
  -LookupPublic     ask an outside service for the public address
  -Ps2 / -NoPs2     whether PlayStation 2 consoles will play
  -Images published|build   published images (default) or a local build
  -ChangeAddress    on an existing .env, redo the address questions
  -InstallTools     with -Yes, allow installing git and Docker Desktop
  -InstallClient / -NoClient  run (or skip) the hosts installer for this PC
  -NoStart          write .env and stop there
'@
    exit 0
}

if ($Reach -or $Address) { $ChangeAddress = $true }

# ------------------------------------------------------------------ helpers

function Say([string]$text) { Write-Host $text }
function Step([string]$text) { Write-Host ''; Write-Host "== $text" }
function Warn([string]$text) { Write-Warning $text }
function Fail([string]$text) { Write-Host "setup.ps1: $text" -ForegroundColor Red; exit 1 }

function Ask([string]$question, [string]$default) {
    if ($Yes) { return $default }
    if ($default) { $prompt = "$question [$default]" } else { $prompt = $question }
    $r = Read-Host $prompt
    if (-not $r) { return $default }
    return $r
}

function YesNo([string]$question, [bool]$default) {
    if ($Yes) { return $default }
    if ($default) { $hint = 'Y/n' } else { $hint = 'y/N' }
    $r = Read-Host "$question ($hint)"
    if (-not $r) { return $default }
    return ($r -match '^[Yy]')
}

function Have([string]$name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

# Run a native command that changes something; with -DryRun, print it.
function Run([string]$exe, [string[]]$argList) {
    if ($DryRun) { Say ("  would run: $exe " + ($argList -join ' ')); return 0 }
    & $exe @argList | Out-Host
    return $LASTEXITCODE
}

function New-Secret([int]$length, [string]$chars) {
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $sb = New-Object System.Text.StringBuilder
    $buf = New-Object byte[] 1
    $limit = 256 - (256 % $chars.Length)
    while ($sb.Length -lt $length) {
        $rng.GetBytes($buf)
        if ($buf[0] -lt $limit) { [void]$sb.Append($chars[$buf[0] % $chars.Length]) }
    }
    return $sb.ToString()
}
$Alnum = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'

function Test-IPv4([string]$ip) {
    if ($ip -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { return $false }
    foreach ($o in $ip.Split('.')) { if ([int]$o -gt 255) { return $false } }
    return $true
}

function Test-Private([string]$ip) {
    if (-not (Test-IPv4 $ip)) { return $false }
    $o = $ip.Split('.') | ForEach-Object { [int]$_ }
    if ($o[0] -eq 10) { return $true }
    if ($o[0] -eq 172 -and $o[1] -ge 16 -and $o[1] -le 31) { return $true }
    if ($o[0] -eq 192 -and $o[1] -eq 168) { return $true }
    return $false
}

function Test-Cgnat([string]$ip) {
    if (-not (Test-IPv4 $ip)) { return $false }
    $o = $ip.Split('.') | ForEach-Object { [int]$_ }
    return ($o[0] -eq 100 -and $o[1] -ge 64 -and $o[1] -le 127)
}

# 198.51.100.5, 24 -> 198.51.100.0/24
function Get-Network([string]$ip, [int]$bits) {
    $o = $ip.Split('.') | ForEach-Object { [uint64]$_ }
    [uint64]$n = ($o[0] -shl 24) -bor ($o[1] -shl 16) -bor ($o[2] -shl 8) -bor $o[3]
    if ($bits -eq 0) { [uint32]$mask = 0 } else { [uint32]$mask = [uint32](([uint64]4294967295 -shl (32 - $bits)) -band [uint64]4294967295) }
    $n = $n -band [uint64]$mask
    return ('{0}.{1}.{2}.{3}/{4}' -f (($n -shr 24) -band 255), (($n -shr 16) -band 255), (($n -shr 8) -band 255), ($n -band 255), $bits)
}

# .env as a list of lines; Get/Set keep comments and order
function Get-EnvValue([System.Collections.Generic.List[string]]$lines, [string]$key) {
    $val = ''
    foreach ($l in $lines) {
        if ($l.StartsWith("$key=")) { $val = $l.Substring($key.Length + 1).Trim('"', "'") }
    }
    return $val
}

function Set-EnvValue([System.Collections.Generic.List[string]]$lines, [string]$key, [string]$value) {
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i].StartsWith("$key=")) { $lines[$i] = "$key=$value"; return }
    }
    if (-not $value) { return }
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match ('^# ?' + [regex]::Escape($key) + '=')) { $lines[$i] = "$key=$value"; return }
    }
    $lines.Add("$key=$value")
}

function Read-Lines([string]$path) {
    $list = New-Object System.Collections.Generic.List[string]
    if (Test-Path -LiteralPath $path) {
        foreach ($l in [System.IO.File]::ReadAllLines($path)) { $list.Add($l.TrimEnd("`r")) }
    }
    return ,$list
}

# ------------------------------------------------------------------ where

Step 'OpenLobby setup'
if ($DryRun) { Say 'Dry run: nothing is changed, every change is printed instead.' }

$Checkout = ''
if (-not $Dir -and $PSScriptRoot) {
    if ((Test-Path (Join-Path $PSScriptRoot 'docker-compose.yml')) -and
        (Test-Path (Join-Path $PSScriptRoot '.env.example')) -and
        (Test-Path (Join-Path $PSScriptRoot 'tools\stack.py'))) {
        $Checkout = $PSScriptRoot
    }
}
$NeedClone = $false
if (-not $Checkout) {
    if (-not $Dir) {
        $Dir = Ask 'Directory for the OpenLobby checkout' (Join-Path $HOME 'openlobby-server\openlobby')
    }
    $Dir = [System.IO.Path]::GetFullPath($Dir).TrimEnd('\')
    if ((Test-Path (Join-Path $Dir 'docker-compose.yml')) -and (Test-Path (Join-Path $Dir '.env.example'))) {
        $Checkout = $Dir
    } elseif ((Test-Path $Dir) -and (Get-ChildItem -Force $Dir | Select-Object -First 1)) {
        Fail "$Dir exists and is not an OpenLobby checkout; pick another -Dir"
    } else {
        $Checkout = $Dir
        $NeedClone = $true
    }
}
Say "Checkout: $Checkout"

# ------------------------------------------------------------------ tools

Step 'Checking git and Docker Desktop'
$haveWinget = Have 'winget'
$missing = @()
if (-not (Have 'git')) { $missing += 'git' }
if (-not (Have 'docker')) { $missing += 'docker' }
if ($missing.Count -gt 0) {
    $cmds = @()
    if ($missing -contains 'git') { $cmds += 'winget install --id Git.Git -e --source winget' }
    if ($missing -contains 'docker') { $cmds += 'winget install --id Docker.DockerDesktop -e --source winget' }
    Say ('Missing: ' + ($missing -join ', '))
    if (-not $haveWinget) {
        Say 'Install them first (https://git-scm.com, https://docs.docker.com/desktop/), then run this script again.'
        exit 1
    }
    Say 'They can be installed with winget:'
    foreach ($c in $cmds) { Say "    $c" }
    $ok = $false
    if ($Yes) { $ok = [bool]$InstallTools } else { $ok = YesNo 'Run these commands now?' $false }
    if (-not $ok) { Say 'Nothing installed. Install them, then run this script again.'; exit 1 }
    foreach ($c in $cmds) {
        $parts = $c.Split(' ')
        $rc = Run $parts[0] $parts[1..($parts.Count - 1)]
        if ($rc -ne 0) { Fail "'$c' failed" }
    }
    if ($missing -contains 'docker') {
        Say 'Docker Desktop is installed. Start it (it may ask you to sign out or restart'
        Say 'Windows the first time), wait until it says it is running, then run this'
        Say 'script again in a new PowerShell window.'
        exit 0
    }
    Say 'git is installed; open a new PowerShell window so it is on PATH, then run this script again.'
    exit 0
}
Say ('git: ' + (git --version))
Say ('docker: ' + (docker --version))

$null = docker compose version 2>$null
if ($LASTEXITCODE -ne 0) { Fail 'docker compose (Compose v2) is missing; update Docker Desktop' }
$ComposeVer = ((docker compose version --short) | Out-String).Trim().TrimStart('v')
Say "compose: $ComposeVer"
$null = docker info 2>$null
if ($LASTEXITCODE -ne 0) {
    Say 'Docker Desktop is not running (docker info gets no answer). Start Docker'
    Say 'Desktop, wait until it says it is running, then run this script again.'
    exit 1
}

function Test-ComposeAtLeast([int]$maj, [int]$min) {
    $p = $ComposeVer.Split('.')
    if ($p.Count -lt 2) { return $false }
    $a = 0; $b = 0
    if (-not [int]::TryParse($p[0], [ref]$a) -or -not [int]::TryParse($p[1], [ref]$b)) { return $false }
    return ($a -gt $maj -or ($a -eq $maj -and $b -ge $min))
}

# ------------------------------------------------------------------ clone

if ($NeedClone) {
    Step 'Getting OpenLobby'
    $parent = Split-Path -Parent $Checkout
    if (-not $DryRun) { New-Item -ItemType Directory -Force -Path $parent | Out-Null } else { Say "  would create $parent" }
    $rc = Run 'git' @('clone', $Repo, $Checkout)
    if ($rc -ne 0) { Fail 'git clone failed' }
}

$EnvFile = Join-Path $Checkout '.env'
$Example = Join-Path $Checkout '.env.example'
$NewEnv = -not (Test-Path -LiteralPath $EnvFile)

# ------------------------------------------------------------------ addresses

$script:A = @{}

function Get-Addresses {
    $r = @{ Lan = ''; LanCidr = ''; Vpn = ''; VpnCidr = ''; VpnName = ''; Public = '' }
    $all = @()
    try { $all = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop) } catch { Warn 'Get-NetIPAddress failed, so addresses are not detected'; return $r }
    $defIf = $null
    try {
        $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop | Sort-Object RouteMetric | Select-Object -First 1
        if ($route) { $defIf = $route.InterfaceIndex }
    } catch { }
    foreach ($a in $all) {
        $ip = [string]$a.IPAddress
        if ($ip.StartsWith('127.') -or $ip.StartsWith('169.254.')) { continue }
        $alias = [string]$a.InterfaceAlias
        $desc = ''
        try { $desc = [string](Get-NetAdapter -InterfaceIndex $a.InterfaceIndex -ErrorAction Stop).InterfaceDescription } catch { }
        if ($alias -match '^(wg|wt|tailscale|zt)' -or $desc -match 'WireGuard|Tailscale|ZeroTier') {
            if (-not $r.Vpn) {
                $r.Vpn = $ip; $r.VpnName = $alias
                $bits = [int]$a.PrefixLength
                if ((Test-Cgnat $ip) -and $bits -ge 32) { $r.VpnCidr = Get-Network $ip 10 }
                elseif ($bits -ge 32) { $r.VpnCidr = Get-Network $ip 24 }
                else { $r.VpnCidr = Get-Network $ip $bits }
            }
            continue
        }
        if ($alias -match 'vEthernet|WSL|Loopback|docker') { continue }
        if (-not $r.Lan -or ($defIf -and $a.InterfaceIndex -eq $defIf)) {
            $r.Lan = $ip; $r.LanCidr = Get-Network $ip ([int]$a.PrefixLength)
        }
        if (-not (Test-Private $ip) -and -not (Test-Cgnat $ip) -and -not $r.Public) { $r.Public = $ip }
    }
    return $r
}

function Test-Port53Busy {
    try { return [bool](Get-NetUDPEndpoint -LocalPort 53 -ErrorAction Stop) } catch { return $false }
}

function Select-Addresses {
    Step 'Who will play on this server?'
    $d = Get-Addresses
    if ($d.Lan) { Say "This PC's local network address: $($d.Lan) ($($d.LanCidr))" }
    if ($d.Vpn) { Say "VPN interface $($d.VpnName): $($d.Vpn) ($($d.VpnCidr))" }
    $chosen = $Reach
    if (-not $chosen) {
        Say '  1) people on this network only (and this PC)'
        Say '  2) friends over a VPN (WireGuard, Tailscale, ZeroTier and the like)'
        Say '  3) anyone over the internet'
        $def = '1'; if ($d.Vpn) { $def = '2' }
        switch (Ask 'Choose 1, 2 or 3' $def) {
            '1' { $chosen = 'lan' } 'lan' { $chosen = 'lan' }
            '2' { $chosen = 'vpn' } 'vpn' { $chosen = 'vpn' }
            '3' { $chosen = 'internet' } 'internet' { $chosen = 'internet' }
            default { Fail 'expected 1, 2 or 3' }
        }
    }
    $script:ReachChosen = $chosen
    $wantPs2 = $null
    if ($Ps2) { $wantPs2 = $true } elseif ($NoPs2) { $wantPs2 = $false }
    $a = @{ POL_ADVERTISE = ''; POL_ADVERTISE_PUBLIC = ''; POL_ADVERTISE_LAN = ''; POL_DNS_BIND = ''; POL_DNS_CLIENTS = ''; POL_DNS_PUBLIC = '' }
    switch ($chosen) {
        'lan' {
            $adv = $Address; if (-not $adv) { $adv = $d.Lan }
            if (-not $adv) { $adv = '127.0.0.1' }
            $a.POL_ADVERTISE = $adv
            if (Test-Port53Busy) {
                Say 'Something on this PC already listens on port 53, so the DNS service is'
                Say "pinned to $adv with POL_DNS_BIND."
                $a.POL_DNS_BIND = $adv
            }
        }
        'vpn' {
            $adv = $Address; if (-not $adv) { $adv = $d.Vpn }
            if (-not $adv) { $adv = Ask "No VPN interface was found. This PC's address on the VPN" '' }
            $cidr = $d.VpnCidr
            if (-not $cidr -or $adv -ne $d.Vpn) { $cidr = Get-Network $adv 24 }
            $a.POL_ADVERTISE = $adv
            $a.POL_DNS_BIND = $adv
            $a.POL_DNS_CLIENTS = "127.0.0.0/8,$cidr"
            if ($null -eq $wantPs2) { $wantPs2 = YesNo "Will a PlayStation 2 on this PC's local network play too? (a console cannot join the VPN)" $false }
            if ($wantPs2 -and $d.Lan) {
                $a.POL_DNS_BIND = ''
                $a.POL_DNS_CLIENTS = "127.0.0.0/8,$cidr,$($d.LanCidr)"
                $a.POL_ADVERTISE_LAN = $d.Lan
            }
        }
        'internet' {
            $pub = $PublicAddress; if (-not $pub) { $pub = $Address }
            if (-not $pub -and $d.Public) { $pub = $d.Public }
            if (-not $pub) {
                Say 'This PC has no public address of its own, so it is probably behind a'
                Say "router. Its public address can be looked up by asking $LookupUrl"
                Say '(one HTTPS request; nothing else is sent).'
                $doit = [bool]$LookupPublic
                if (-not $doit -and -not $Yes) { $doit = YesNo 'Look it up there?' $false }
                if ($doit) {
                    try { $pub = ([string](Invoke-RestMethod -Uri $LookupUrl -TimeoutSec 10)).Trim() } catch { Warn 'the lookup failed' }
                    if (-not (Test-IPv4 $pub)) { $pub = '' } else { Say "The lookup says: $pub" }
                }
                if (-not $pub) { $pub = Ask 'The public address players will connect to' '' }
            }
            $a.POL_ADVERTISE = $pub
            if ($null -eq $wantPs2) { $wantPs2 = YesNo 'Will PlayStation 2 consoles play over the internet? (they need this server''s DNS open to the internet)' $false }
            if ($wantPs2) {
                $a.POL_ADVERTISE_PUBLIC = $pub
                $a.POL_DNS_PUBLIC = '1'
                Say 'Note: Docker Desktop usually hides the real client address from the DNS'
                Say 'service, so its public mode cannot tell internet clients apart here'
                Say '(docs/configuration.md, "DNS"). A Linux host is the better fit for this.'
            } elseif ($d.Lan -and (Test-Private $d.Lan)) {
                $a.POL_DNS_BIND = $d.Lan
            } else {
                $a.POL_DNS_BIND = '127.0.0.1'
            }
        }
    }
    if (-not (Test-IPv4 $a.POL_ADVERTISE)) { Fail "'$($a.POL_ADVERTISE)' is not an IPv4 address; give one with -Address" }
    $keys = 'POL_ADVERTISE', 'POL_ADVERTISE_PUBLIC', 'POL_ADVERTISE_LAN', 'POL_DNS_BIND', 'POL_DNS_CLIENTS', 'POL_DNS_PUBLIC'
    Say ''
    Say 'Proposed address settings for .env (docs/configuration.md):'
    foreach ($k in $keys) { Say "  $k=$($a[$k])" }
    if (-not $Yes -and -not (YesNo 'Use these?' $true)) {
        Say 'Enter a value, or - to leave a setting empty (the default).'
        foreach ($k in $keys) {
            $v = Ask "  $k" $a[$k]
            if ($v -eq '-') { $v = '' }
            $a[$k] = $v
        }
    }
    $script:A = $a
}

# ------------------------------------------------------------------ .env

Step 'Settings (.env)'
$script:ReachChosen = $Reach
if ($NewEnv) {
    $lines = Read-Lines $Example
    Select-Addresses
    $setAddr = $true
} else {
    $lines = Read-Lines $EnvFile
    Say "Found $EnvFile; keeping its values."
    Say ('Current address: POL_ADVERTISE=' + (Get-EnvValue $lines 'POL_ADVERTISE'))
    $setAddr = $false
    if ($ChangeAddress -or (-not $Yes -and (YesNo 'Change the address settings?' $false))) {
        Select-Addresses
        $setAddr = $true
    }
}
$before = ($lines -join "`n")
if ($setAddr) {
    foreach ($k in $script:A.Keys) { Set-EnvValue $lines $k $script:A[$k] }
}

$Published = $false
if ($NewEnv) {
    if (-not $Images) {
        Say ''
        Say 'Images: published images are pulled ready-made from ghcr.io (faster);'
        Say 'building compiles them here from this checkout (several minutes).'
        $r = Ask 'Use published images or build locally? (published/build)' 'published'
        if ($r -like 'b*') { $Images = 'build' } else { $Images = 'published' }
    }
    if ($Images -eq 'published' -and -not (Test-ComposeAtLeast 2 24)) {
        Warn "the published images need Docker Compose 2.24 or newer (this is $ComposeVer); building locally instead"
        $Images = 'build'
    }
    if ($Images -eq 'published') {
        # Compose splits COMPOSE_FILE on ';' on Windows and ':' elsewhere
        Set-EnvValue $lines 'COMPOSE_FILE' 'docker-compose.yml;docker-compose.ghcr.yml'
        $Published = $true
    }
} else {
    $Published = ((Get-EnvValue $lines 'COMPOSE_FILE') -like '*ghcr*')
}

$genDb = $false
if ($NewEnv -and -not (Get-EnvValue $lines 'POL_DB_PASSWORD')) {
    $project = Get-EnvValue $lines 'COMPOSE_PROJECT_NAME'
    if (-not $project) { $project = ((Split-Path -Leaf $Checkout).ToLower() -replace '[^a-z0-9_-]', '') }
    $null = docker volume inspect "${project}_pol-pgdata" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Warn "volume ${project}_pol-pgdata already exists, so POL_DB_PASSWORD is left as it was (empty = openlobby)"
    } else {
        Set-EnvValue $lines 'POL_DB_PASSWORD' (New-Secret 32 $Alnum)
        $genDb = $true
    }
}

Say ''
Say 'The deployer (installing games from the admin panel) needs Docker Engine on'
Say 'Linux, so it is not set up here. Run games with python tools\stack.py'
Say '(docs/games.md), or run setup.sh in WSL2 with Docker Engine or on a Linux VPS.'

$text = ($lines -join "`n") + "`n"
if ($DryRun) {
    Say ''
    Say "Dry run: the .env that would be written ($EnvFile), secrets hidden:"
    foreach ($l in $lines) {
        if ($l -match '^[A-Z_]+=.+') {
            if ($l -match '^(POL_DB_PASSWORD|POL_DEPLOYER_TOKEN|POL_ADMIN_PASSWORD)=') { $l = $Matches[1] + '=<secret>' }
            Say "    $l"
        }
    }
} elseif ($NewEnv -or ($before -ne ($lines -join "`n"))) {
    if (-not $NewEnv) { Copy-Item -LiteralPath $EnvFile -Destination "$EnvFile.bak" -Force }
    [System.IO.File]::WriteAllText($EnvFile, $text, (New-Object System.Text.UTF8Encoding $false))
    # readable by this user only
    $null = icacls $EnvFile /inheritance:r /grant:r "${env:USERNAME}:F" 2>$null
    Say "Wrote $EnvFile."
    if (-not $NewEnv) { Say "The previous one is in $EnvFile.bak." }
} else {
    Say "$EnvFile is unchanged."
}
if ($genDb -and -not $DryRun) { Say 'Generated a database password (POL_DB_PASSWORD, kept in .env).' }

if ($NoStart) {
    Say ''
    Say 'Not starting (-NoStart). Start it later with this script, or docs/setup.md, "By hand".'
    exit 0
}

# ------------------------------------------------------------------ start

Step 'Starting the stack'
if (-not $DryRun) { Set-Location -LiteralPath $Checkout }
if ((Test-Path (Join-Path $Checkout '.games')) -and (Get-Content (Join-Path $Checkout '.games') | Where-Object { $_.Trim() })) {
    Say 'Games are enabled in .games; this script starts the core only. Start the whole'
    Say 'stack with: python tools\stack.py compose up -d (add --ghcr for published images).'
    exit 0
}
if ($Published) {
    if ((Run 'docker' @('compose', 'pull')) -ne 0) { Fail 'pulling the images failed' }
    if ((Run 'docker' @('compose', 'up', '-d')) -ne 0) { Fail 'docker compose up failed; see the output above' }
} else {
    if ((Run 'docker' @('compose', 'up', '-d', '--build')) -ne 0) { Fail 'docker compose up failed; see the output above' }
}

if (-not $DryRun) {
    Say 'Waiting for the services to report healthy (up to 3 minutes)...'
    $deadline = (Get-Date).AddSeconds(180)
    while ($true) {
        $st = @(docker compose ps -a --format '{{.Service}} {{.State}} {{.Health}}' 2>$null)
        $pending = @()
        foreach ($row in $st) {
            $f = ([string]$row).Trim() -split '\s+'
            if ($f.Count -lt 2 -or -not $f[0]) { continue }
            $health = ''; if ($f.Count -ge 3) { $health = $f[2] }
            if ($f[1] -ne 'running' -or ($health -and $health -ne 'healthy')) { $pending += $f[0] }
        }
        if ($st.Count -gt 0 -and $pending.Count -eq 0) { Say 'All services are up.'; break }
        if ((Get-Date) -gt $deadline) { Warn ('still waiting on: ' + ($pending -join ' ') + ' (docker compose ps; docker compose logs <service>)'); break }
        Start-Sleep -Seconds 5
    }
}

# ------------------------------------------------------------------ admin

Step 'Admin panel password'
$AdminUser = Get-EnvValue $lines 'POL_ADMIN_USER'
if (-not $AdminUser) { $AdminUser = 'admin' }
$AdminPw = ''
$AdminState = ''

# Sends the password on stdin with a bare LF; a PowerShell pipe would add CR.
function Set-AdminPassword([string]$pw) {
    $exe = (Get-Command docker -CommandType Application | Select-Object -First 1).Source
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = 'compose exec -T admin python admin.py --set-password'
    $psi.WorkingDirectory = $Checkout
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $p.StandardInput.NewLine = "`n"
    $p.StandardInput.Write($pw + "`n")
    $p.StandardInput.Close()
    $out = $p.StandardOutput.ReadToEnd() + $p.StandardError.ReadToEnd()
    $p.WaitForExit()
    return $out
}

if ($DryRun) {
    Say '  would run: docker compose exec -T admin python admin.py --show-admin'
    Say '  and, when no credential is stored, generate a password and send it to'
    Say '  docker compose exec -T admin python admin.py --set-password'
} else {
    $shown = (docker compose exec -T admin python admin.py --show-admin 2>&1 | Out-String)
    if ($shown -match "user='([^']*)'") {
        $AdminUser = $Matches[1]; $AdminState = 'kept'
        Say "A password is already set for $AdminUser; leaving it alone."
    } elseif ($shown -match 'no stored credential') {
        $AdminPw = New-Secret 20 $Alnum
        $out = ''
        for ($i = 0; $i -lt 5; $i++) {
            $out = Set-AdminPassword $AdminPw
            if ($out -match 'credential set') { $AdminState = 'new'; break }
            Start-Sleep -Seconds 3
        }
        if ($AdminState -eq 'new') { Say "Set a new password for $AdminUser." }
        else {
            $AdminPw = ''
            Warn ('could not set the password: ' + $out.Trim())
            Say 'Set one by hand: docker compose exec admin python admin.py --set-password'
        }
    } else {
        Warn ('could not ask the admin service: ' + $shown.Trim())
        Say 'Set a password by hand: docker compose exec admin python admin.py --set-password'
    }
}

# ------------------------------------------------------------------ doctor

Step 'Checking the setup (tools/doctor.py)'
$py = $null
foreach ($n in 'py', 'python', 'python3') {
    $c = Get-Command $n -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $c) { continue }
    # the Microsoft Store placeholder answers with an error instead of a version
    $v = (& $c.Source -c 'import sys; print(sys.version_info[0])' 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -eq 0 -and $v -eq '3') { $py = $c.Source; break }
}
if ($py) {
    $rc = Run $py @('tools\doctor.py')
    if ($rc -ne 0) { Say '(doctor.py reported problems; each FAIL line says what to do)' }
} else {
    Say 'Python is not installed, so tools\doctor.py was skipped. Install Python 3'
    Say "and run it later from $Checkout."
}

# ------------------------------------------------------------------ client on this PC

$Adv = Get-EnvValue $lines 'POL_ADVERTISE'
if (-not $Adv) { $Adv = '127.0.0.1' }
$Ps2Dns = Get-EnvValue $lines 'POL_ADVERTISE_LAN'
if (-not $Ps2Dns) { $Ps2Dns = $Adv }
$installer = Join-Path $Checkout 'client\Install-PolConnect.ps1'

Step 'This PC as a player'
Say 'If you will also play on this PC, its hosts file has to point the PlayOnline'
Say "names at $Adv. client\Install-PolConnect.ps1 does that; it edits the system"
Say 'hosts file, so Windows asks for administrator rights (a UAC prompt) when it runs.'
$doClient = $false
if ($InstallClient) { $doClient = $true }
elseif (-not $NoClient -and -not $Yes) { $doClient = YesNo 'Run it for this PC now?' $false }
if ($doClient) {
    $argLine = "-NoProfile -ExecutionPolicy Bypass -File `"$installer`" -Server $Adv"
    if ($DryRun) {
        Say "  would run (elevated): powershell.exe $argLine"
    } else {
        try {
            $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $argLine -Verb RunAs -Wait -PassThru
            if ($p.ExitCode -eq 0) { Say 'The hosts block is in place.' } else { Warn "the installer exited with code $($p.ExitCode)" }
        } catch {
            Warn 'the elevated installer did not run (the UAC prompt was declined?)'
        }
    }
}

# ------------------------------------------------------------------ summary

$finalReach = $script:ReachChosen
if (-not $finalReach) {
    if ((Get-EnvValue $lines 'POL_DNS_CLIENTS') -or (Test-Cgnat $Adv)) { $finalReach = 'vpn' }
    elseif ((Test-Private $Adv) -or $Adv.StartsWith('127.')) { $finalReach = 'lan' }
    else { $finalReach = 'internet' }
}

Step 'Done'
Say 'Admin panel:'
Say '  http://127.0.0.1:8090 (on this PC only)'
if ($AdminState -eq 'new') {
    Say "  User: $AdminUser"
    Say "  Password: $AdminPw"
    Say '  This is the only time it is shown. Change it on the panel, Settings > Sign-in.'
} elseif ($AdminState -eq 'kept') {
    Say "  User: $AdminUser (password unchanged)"
}
Say ''
Say 'Players:'
Say '  PC: copy client\Install-PolConnect.ps1 to the player''s PC and run, in an'
Say '  elevated PowerShell:'
Say "    powershell -ExecutionPolicy Bypass -File .\Install-PolConnect.ps1 -Server $Adv"
Say "  PlayStation 2: set the console's DNS server to $Ps2Dns (docs/client.md)."
Say '  Accounts: create them on the admin panel, or registration codes for the'
Say '  in-client sign-up.'
Say ''
Say 'Games: python tools\stack.py (docs/games.md). Installing them from the admin'
Say 'panel needs the deployer, which runs on Linux only (setup.sh).'
Say ''
Say 'Firewall (docs/ports.md):'
switch ($finalReach) {
    'lan' {
        Say '  On a local network, allow the ports in docs/ports.md through Windows'
        Say '  Firewall for the Private profile if other machines cannot connect.'
    }
    'vpn' {
        Say '  Allow these from the VPN: TCP 80, 51200-51271, 51273-51310, 51330,'
        Say '  53001-53015, 54000 (25 and 110 for generic mail accounts), and 53/udp'
        Say '  for clients using this server''s DNS.'
    }
    'internet' {
        Say '  Forward or open TCP 80, 51200-51271, 51273-51310, 51330, 53001-53015'
        Say '  and 54000 (25 and 110 for generic mail accounts, 443 for PlayStation 2).'
        if (Get-EnvValue $lines 'POL_DNS_PUBLIC') { Say '  53/udp too, for the consoles.' } else { Say '  Keep 53/udp closed to the internet.' }
    }
}
Say ''
Say 'Run this script again any time; it keeps .env. docs/setup.md has the details.'
exit 0
