<#
.SYNOPSIS
    First-run setup for an OpenLobby server on Windows with Docker Desktop.

.DESCRIPTION
    Checks git and Docker Desktop, clones OpenLobby when it is not run from a
    checkout, asks who will play and proposes the address settings for that,
    writes .env, starts the core stack, sets the admin panel's password and
    prints what to do next. Run again, it keeps the .env it finds.
    docs/setup.md explains every step.

    On Windows Firewall it lets the players' ports in from the local network
    (one rule group, OpenLobby, made with administrator rights after a UAC
    prompt), and says when the network is set to Public.

    It also sets up the deployer, which lets the admin panel install and
    update games and OpenLobby itself (docs/deployer.md), unless told not to.

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
    [ValidatePattern('^[A-Za-z0-9._-]*$')]
    [string]$AdminUser = '',
    [switch]$ChangeAddress,
    [switch]$Deployer,
    [switch]$NoDeployer,
    [string]$Clients = '',
    [ValidateSet('', 'stable', 'dev')]
    [string]$Channel = '',
    [string]$Registry = '',
    [string]$CodeFile = '',
    [switch]$InstallTools,
    [switch]$InstallClient,
    [switch]$NoClient,
    [switch]$NoFirewall,
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
  -AdminUser NAME   the admin panel's user name on a new .env (default: admin)
  -ChangeAddress    on an existing .env, redo the address questions
  -Deployer / -NoDeployer  run (or leave off) the deployer, which lets the
                    admin panel install and update games (docs/deployer.md)
  -Clients DIR|none folder of your own client installs for the deployer
                    to import game data from (default: clients beside the
                    checkout; none: uploads only)
  -Channel stable|dev  the update channel (docs/deployer.md, "Dev channel"):
                    dev follows the developers' preview builds and asks for
                    an access code (not shown); needs the deployer
  -Registry HOST/PATH  advanced: the registry a plain token is for (kept in .env
                    as POL_DEV_REGISTRY); an access code carries its own
  -CodeFile FILE    with -Yes: read the access code from FILE's first line
  -InstallTools    with -Yes, allow installing git and Docker Desktop
  -InstallClient / -NoClient  run (or skip) the hosts installer for this PC
  -NoFirewall       leave Windows Firewall alone (by default setup lets
                    players through it, with one UAC prompt; docs/ports.md)
  -NoStart          write .env and stop there
'@
    exit 0
}

if ($Reach -or $Address) { $ChangeAddress = $true }
$RegistryPattern = '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)*$'
if ($Registry -and $Registry -cnotmatch $RegistryPattern) {
    Write-Host 'setup.ps1: -Registry looks like registry.example.org/team' -ForegroundColor Red
    exit 2
}

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

# Python 3's path, or $null
function Find-Python {
    foreach ($n in 'py', 'python', 'python3') {
        $c = Get-Command $n -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $c) { continue }
        # the Microsoft Store placeholder answers with an error instead of a version
        $v = (& $c.Source -c 'import sys; print(sys.version_info[0])' 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -eq 0 -and $v -eq '3') { return $c.Source }
    }
    return $null
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
# git is needed only to clone; a checkout unpacked from a download runs without it
if ($NeedClone -and -not (Have 'git')) { $missing += 'git' }
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
if (Have 'git') { Say ('git: ' + (git --version)) }
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

# What holds the ports a start needs, said plainly: a start that fails on a taken
# port prints Docker's own error above, which names the port but not the program.
function Show-PortHolders {
    $held = @()
    try {
        foreach ($e in @(Get-NetUDPEndpoint -LocalPort 53 -ErrorAction Stop)) {
            $held += ,@('UDP', 53, [string]$e.LocalAddress, [int]$e.OwningProcess)
        }
    } catch { }
    foreach ($port in 80, 443) {
        try {
            foreach ($e in @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction Stop)) {
                $held += ,@('TCP', $port, [string]$e.LocalAddress, [int]$e.OwningProcess)
            }
        } catch { }
    }
    foreach ($h in $held) {
        $name = "process $($h[3])"
        try { $name = (Get-Process -Id $h[3] -ErrorAction Stop).ProcessName + " (process $($h[3]))" } catch { }
        if ($name -match '^(com\.docker\.backend|vpnkit)') { $name = 'Docker Desktop, for another container (docker ps lists them)' }
        Say "  $($h[0]) port $($h[1]) on $($h[2]) is taken by $name."
        if ($h[1] -eq 53 -and $name -like 'svchost*') {
            Say '    That is usually Internet Connection Sharing or the Mobile hotspot. Turn the'
            Say '    hotspot off (Settings > Network and internet), or keep it and run this'
            Say '    script again with -ChangeAddress: it then pins the DNS service to this'
            Say '    PC''s network address (POL_DNS_BIND), which works beside it.'
        }
    }
}

function Fail-Up {
    Say ''
    Say 'Ports OpenLobby needs that another program holds:'
    Show-PortHolders
    Fail 'docker compose up failed; see the output above'
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
# The admin panel's user name: on a new .env only (a stored credential keeps its own)
if ($NewEnv -and $AdminUser) { Set-EnvValue $lines 'POL_ADMIN_USER' $AdminUser }

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

# ------------------------------------------------------------------ deployer

# Docker Desktop runs the Docker daemon in a Linux VM, which sees the Windows folder
# C:\x as /run/desktop/mnt/host/c/x. The deployer gets the checkouts mounted at that
# path, so the paths docker compose hands the daemon from inside it name the same
# folders (docs/deployer.md, "Docker Desktop"). '' for a folder with no drive letter.
function ConvertTo-DaemonPath([string]$path) {
    if ($path -notmatch '^([A-Za-z]):\\(.*)$') { return '' }
    $rest = $Matches[2].TrimEnd('\') -replace '\\', '/'
    $p = '/run/desktop/mnt/host/' + $Matches[1].ToLower()
    if ($rest) { $p += '/' + $rest }
    return $p
}

$Parent = Split-Path -Parent $Checkout
$UseDeployer = $false
$genToken = $false
$hasToken = [bool](Get-EnvValue $lines 'POL_DEPLOYER_TOKEN')
if ($hasToken -and -not $NoDeployer) {
    $UseDeployer = $true
} elseif ($NewEnv -or $Deployer) {
    $why = ''
    $dockerOs = ((docker info --format '{{.OperatingSystem}}' 2>$null) | Out-String).Trim()
    if ($dockerOs -notmatch 'Docker Desktop') {
        $why = "this Docker is not Docker Desktop ($dockerOs), so where its daemon sees this folder is not known"
    } elseif (-not (ConvertTo-DaemonPath $Parent)) {
        $why = "$Parent is not on a drive letter"
    } elseif ($Parent.Contains('$')) {
        $why = "$Parent has a dollar sign, which Docker Compose would read as a variable"
    }
    if ($NoDeployer) {
    } elseif ($why) {
        Say "The deployer (installing games from the admin panel) is left off: $why."
    } elseif ($Deployer) {
        $UseDeployer = $true
    } else {
        Say ''
        Say 'The deployer lets the admin panel install, enable and update games and'
        Say 'OpenLobby itself (docs/deployer.md). It holds the Docker socket, which makes'
        Say 'it as powerful as Docker Desktop itself; it publishes no port and needs a token.'
        $UseDeployer = YesNo 'Run the deployer?' $true
    }
    if ($UseDeployer) {
        Set-EnvValue $lines 'POL_DEPLOYER_TOKEN' (New-Secret 43 ($Alnum + '_-'))
        $genToken = $true
    }
} elseif (-not $hasToken) {
    Say 'The deployer is not set up in this .env; run with -Deployer to add it.'
}
if ($UseDeployer) {
    if (-not (Get-EnvValue $lines 'POL_DEPLOY_ROOT')) { Set-EnvValue $lines 'POL_DEPLOY_ROOT' (ConvertTo-DaemonPath $Parent) }
    if ((Split-Path -Leaf $Checkout) -ne 'openlobby' -and -not (Get-EnvValue $lines 'POL_DEPLOY_CORE')) {
        Set-EnvValue $lines 'POL_DEPLOY_CORE' (ConvertTo-DaemonPath $Checkout)
    }
    if ($Published -and -not (Get-EnvValue $lines 'POL_DEPLOY_GHCR')) { Set-EnvValue $lines 'POL_DEPLOY_GHCR' '1' }
    if (-not (Get-EnvValue $lines 'POL_DEPLOY_CLIENTS') -and $Clients -ne 'none') {
        if (-not $Clients) {
            Say ''
            Say 'Optional: a folder for your own client installs (copies of your game'
            Say 'install folders). When a game needs data from your client, the admin'
            Say 'panel can pick an install from this folder to import it from. The'
            Say 'deployer only reads it. Without one, the panel takes uploaded archives'
            Say 'instead. Enter - to skip.'
            $Clients = Ask 'Client installs folder' (Join-Path $Parent 'clients')
        }
        if ($Clients -and $Clients -ne '-' -and $Clients -ne 'none') {
            $Clients = [System.IO.Path]::GetFullPath($Clients).TrimEnd('\')
            $cpath = ConvertTo-DaemonPath $Clients
            if (-not $cpath -or $Clients.Contains('$')) {
                Warn "$Clients cannot be handed to the deployer (no drive letter, or a dollar sign); uploads only"
            } else {
                Set-EnvValue $lines 'POL_DEPLOY_CLIENTS' $cpath
                if ($DryRun) { Say "  would create $Clients" } else { New-Item -ItemType Directory -Force -Path $Clients | Out-Null }
            }
        }
    }
}

# The update channel (docs/deployer.md, "Dev channel"). The deployer signs in to the dev
# builds and keeps the access code; this script only hands it over, on stdin.
$DevCode = ''
if ($Channel -eq 'dev') {
    if (-not $UseDeployer) { Fail 'the dev channel is run by the deployer; run with -Deployer' }
    # an access code carries where the dev builds are; -Registry is only for a plain token
    if ($Registry) { Set-EnvValue $lines 'POL_DEV_REGISTRY' $Registry }
    if ($CodeFile) {
        if (-not (Test-Path -LiteralPath $CodeFile -PathType Leaf)) { Fail "cannot read the access code file $CodeFile" }
        $DevCode = [string](Get-Content -LiteralPath $CodeFile -TotalCount 1)
    } elseif ($Yes -or $DryRun) {
        if (-not $DryRun) { Fail '-Channel dev with -Yes needs -CodeFile (the access code is not taken on the command line)' }
    } else {
        $sec = Read-Host 'Access code (not shown)' -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
        try { $DevCode = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    }
    $DevCode = $DevCode.Trim()
    if (-not $DryRun -and -not $DevCode) { Fail 'no access code was given' }
}

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
if ($genToken -and -not $DryRun) { Say 'Generated a deployer token (POL_DEPLOYER_TOKEN, kept in .env).' }

if ($NoStart) {
    Say ''
    Say 'Not starting (-NoStart). Start it later with this script, or docs/setup.md, "By hand".'
    exit 0
}

# ------------------------------------------------------------------ start

# Enabled games (one key per line in .games, # comments) start through tools\stack.py, so
# their compose files come along and login keeps their title plugins; a plain docker
# compose up would rebuild login without them.
$Games = @()
$gamesFile = Join-Path $Checkout '.games'
if (Test-Path -LiteralPath $gamesFile) {
    $Games = @(Get-Content -LiteralPath $gamesFile | ForEach-Object { ($_ -replace '#.*$', '').Trim() } | Where-Object { $_ })
}
$py = Find-Python
if ($Games.Count -gt 0 -and $py) {
    # The data some games keep in Docker volumes (FFXI's maps): created and filled once,
    # left alone when it is already there (docs/games.md, "Volumes").
    Step 'Game data'
    if ((Run $py @((Join-Path $Checkout 'tools\stack.py'), 'volumes')) -ne 0) {
        Warn 'some game data could not be set up (the lines above say why); those games will not start until it is. Run this script again to retry.'
    }
}

Step 'Starting the stack'
if (-not $DryRun) { Set-Location -LiteralPath $Checkout }
# The network every install on this PC shares (docs/installs.md); the admin panel joins
# it, so it must exist before compose starts anything.
$null = docker network inspect openlobby 2>$null
if ($LASTEXITCODE -ne 0) {
    if ((Run 'docker' @('network', 'create', 'openlobby')) -ne 0) {
        # another install may have made it in between; only a network still missing is an error
        $null = docker network inspect openlobby 2>$null
        if ($LASTEXITCODE -ne 0) { Fail 'could not create the Docker network openlobby' }
    }
}
if ($UseDeployer) {
    # built here from services\deployer (the first time takes a few minutes)
    if ((Run 'docker' @('compose', '--profile', 'deployer', 'up', '-d', 'deployer')) -ne 0) {
        Warn 'the deployer did not start; docker compose --profile deployer logs deployer says why'
    }
}
$started = $true
if ($Games.Count -gt 0 -and -not $py) {
    Warn ('games are enabled (' + ($Games -join ', ') + ') and starting them needs Python 3 for tools\stack.py, which is not installed, so the stack was left as it is')
    Say 'Install Python 3 and run this script again, or restart OpenLobby from the admin'
    Say 'panel, Settings > Install games.'
    $started = $false
} elseif ($Games.Count -gt 0) {
    Say ('Enabled games: ' + ($Games -join ', ') + ' (started through tools\stack.py)')
    $sc = @('tools\stack.py', 'compose')
    if ($Published) {
        $sc += '--ghcr'
        if ((Run $py ($sc + @('pull', '--ignore-buildable', '--ignore-pull-failures'))) -ne 0) { Fail 'pulling the images failed' }
        if ((Run $py ($sc + @('up', '-d'))) -ne 0) { Fail-Up }
    } else {
        if ((Run $py ($sc + @('up', '-d', '--build'))) -ne 0) { Fail-Up }
    }
} elseif ($Published) {
    if ((Run 'docker' @('compose', 'pull')) -ne 0) { Fail 'pulling the images failed' }
    if ((Run 'docker' @('compose', 'up', '-d')) -ne 0) { Fail-Up }
} else {
    if ((Run 'docker' @('compose', 'up', '-d', '--build')) -ne 0) { Fail-Up }
}

if ($started -and -not $DryRun) {
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
if ($UseDeployer -and -not $DryRun) {
    # The deployer checks when it starts that Docker sees this folder where it does, and
    # runs no docker compose command when it does not; its healthcheck says which.
    $dh = ''
    $dok = $false
    for ($i = 0; $i -lt 12; $i++) {
        $dh = (docker compose --profile deployer exec -T deployer python /app/deployer.py --healthcheck 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -eq 0) { $dok = $true; break }
        if ($dh -match 'POL_DEPLOY_ROOT') { break }
        Start-Sleep -Seconds 5
    }
    if ($dok -and $dh -match 'paths ok') { Say 'The deployer is running, and Docker sees this folder where the deployer does.' }
    elseif ($dok) { Say 'The deployer is running.' }
    else {
        Warn ('the deployer is not ready: ' + $dh)
        Say 'docker compose --profile deployer logs deployer says more (docs/deployer.md).'
    }
}

# The update channel: switched by the deployer, the access code handed to it on stdin
$onDev = $false
$chFile = Join-Path $Checkout '.channel'
if (Test-Path -LiteralPath $chFile) { $onDev = [bool](Select-String -LiteralPath $chFile -Pattern '"channel": "dev"' -Quiet) }
if ($Channel -eq 'dev' -or ($Channel -eq 'stable' -and $onDev)) {
    Step "Update channel: $Channel"
    $chArgs = @('compose', '--profile', 'deployer', 'exec', '-T', 'deployer', 'python', '/app/deployer.py', '--channel', $Channel)
    if ($DryRun) {
        $how = ''; if ($Channel -eq 'dev') { $how = ' (the access code on stdin)' }
        Say ('  would run: docker ' + ($chArgs -join ' ') + $how)
    } elseif (-not $UseDeployer) {
        Warn 'the update channel is switched by the deployer, which is not set up here (-Deployer)'
    } else {
        $chOut = ''
        for ($i = 0; $i -lt 12; $i++) {
            $chOut = ($DevCode | docker @chArgs 2>&1 | Out-String).Trim()
            $chRc = $LASTEXITCODE
            if ($chOut -notmatch 'not answering|is not running') { break }
            Start-Sleep -Seconds 5
        }
        foreach ($l in ($chOut -split "`r?`n")) { Say "  $l" }
        if ($chRc -ne 0) { Warn "the switch to the $Channel channel did not finish; Settings > Install games > Update channel shows why and can try again" }
    }
    $DevCode = ''
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
    # the admin service may still be starting on a slow first run: ask for up to a minute
    for ($i = 0; $i -lt 12; $i++) {
        $shown = (docker compose exec -T admin python admin.py --show-admin 2>&1 | Out-String)
        if ($shown -match "user='|no stored credential") { break }
        Start-Sleep -Seconds 5
    }
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

# ------------------------------------------------------------------ firewall

$Adv = Get-EnvValue $lines 'POL_ADVERTISE'
if (-not $Adv) { $Adv = '127.0.0.1' }
$Ps2Dns = Get-EnvValue $lines 'POL_ADVERTISE_LAN'
if (-not $Ps2Dns) { $Ps2Dns = $Adv }

$finalReach = $script:ReachChosen
if (-not $finalReach) {
    if ((Get-EnvValue $lines 'POL_DNS_CLIENTS') -or (Test-Cgnat $Adv)) { $finalReach = 'vpn' }
    elseif ((Test-Private $Adv) -or $Adv.StartsWith('127.')) { $finalReach = 'lan' }
    else { $finalReach = 'internet' }
}

$FwGroup = 'OpenLobby'
$FwState = ''

# The rules for the ports players reach (docs/ports.md), as tools\firewall.ps1 takes
# them: PROTOCOL:PORTS:PROFILES:REMOTE. Private networks, from the local network (and
# the VPN's range, on any network type, for friends on a VPN); everyone only for the
# internet case. 443 only where a PlayStation 2 may play: always on a local network,
# where nothing else says whether one will.
function Get-FirewallSpecs {
    $consoles = ($finalReach -eq 'lan') -or [bool]$Ps2 -or [bool](Get-EnvValue $lines 'POL_ADVERTISE_LAN') -or
        ((Get-EnvValue $lines 'POL_DNS_PUBLIC') -eq '1')
    $tcp = @('25', '80', '110')
    if ($consoles) { $tcp += '443' }
    $tcp += '51200-51271', '51273-51310', '51330', '53001-53015', '54000'
    $tcp = $tcp -join ','
    $vpn = @()
    foreach ($c in (Get-EnvValue $lines 'POL_DNS_CLIENTS').Split(',')) {
        $c = $c.Trim()
        if ($c -and -not $c.StartsWith('127.') -and $c -match '^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$') { $vpn += $c }
    }
    $near = (@('LocalSubnet') + $vpn) -join ','
    $specs = @()
    if ($finalReach -eq 'internet') {
        $dnsFrom = 'LocalSubnet'
        if ((Get-EnvValue $lines 'POL_DNS_PUBLIC') -eq '1') { $dnsFrom = 'Any' }
        $specs += "UDP:53:Private:$dnsFrom", "TCP:${tcp}:Private:Any"
        $gmFrom = 'Any'
    } else {
        $specs += "UDP:53:Private:$near", "TCP:${tcp}:Private:$near"
        if ($vpn.Count -gt 0) {
            $v = $vpn -join ','
            $specs += "UDP:53:Public:$v", "TCP:${tcp}:Public:$v"
        }
        $gmFrom = $near
    }
    if ((Get-EnvValue $lines 'POL_GM_CALL') -eq '1') { $specs += "UDP:51112:Private:$gmFrom" }
    return ,$specs
}

# The address consoles use as their DNS server, whose network type matters
function Get-ConsoleAddress {
    $a = Get-EnvValue $lines 'POL_ADVERTISE_LAN'
    if ($a) { return $a }
    if ($finalReach -eq 'lan') { return $Adv }
    $b = Get-EnvValue $lines 'POL_DNS_BIND'
    if (Test-Private $b) { return $b }
    return (Get-Addresses).Lan
}

function Format-Spec([string]$spec) {
    $f = $spec.Split(':')
    $from = $f[3] -replace 'LocalSubnet', 'the local network' -replace ',', ', '
    if ($from -eq 'Any') { $from = 'anywhere' }
    return ("  $($f[0]) " + ($f[1] -replace ',', ', ') + " from $from, on $($f[2]) networks")
}

Step 'Windows Firewall'
if ($NoFirewall) {
    $FwState = 'skipped'
    Say 'Left alone (-NoFirewall). docs/ports.md lists the ports to allow.'
} elseif (-not (Get-Command New-NetFirewallRule -ErrorAction SilentlyContinue)) {
    $FwState = 'skipped'
    Say 'This Windows has no firewall cmdlets, so the firewall was left alone (docs/ports.md).'
} else {
    $specs = Get-FirewallSpecs
    $todo = @()
    $have = @()
    try { $have = @(Get-NetFirewallRule -Group $FwGroup -ErrorAction Stop | ForEach-Object { [string]$_.Description }) } catch { }
    $same = ((@($have | Sort-Object) -join ';') -eq (@($specs | Sort-Object) -join ';'))
    if ($same) {
        Say "The rules that let players in are in place (rule group $FwGroup)."
    } else {
        $todo += "let players in (rule group $FwGroup)"
    }

    # Windows blocks other devices on a network it calls Public
    $privateIf = 0
    $conAddr = Get-ConsoleAddress
    if (Test-IPv4 $conAddr) {
        $ip = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { [string]$_.IPAddress -eq $conAddr }) | Select-Object -First 1
        if (-not $ip) {
            Warn "$conAddr is not an address of this PC right now, so a console pointed at it reaches nothing. If the router gave this PC a new address, run this script again with -ChangeAddress."
        } else {
            $cat = ''
            try { $cat = [string](Get-NetConnectionProfile -InterfaceIndex $ip.InterfaceIndex -ErrorAction Stop).NetworkCategory } catch { }
            Say "Consoles reach this PC at $conAddr, on $($ip.InterfaceAlias) (network type: $cat)."
            if ($cat -eq 'Public') {
                Say "Windows treats the network on $($ip.InterfaceAlias) as Public, a network it does not"
                Say 'trust, and keeps other devices on it from reaching this PC. A home network'
                Say 'should be Private: Settings > Network and internet > (the network) > Network'
                Say 'profile type > Private network.'
                if (YesNo "Switch $($ip.InterfaceAlias) to Private now?" ($finalReach -eq 'lan')) {
                    $privateIf = [int]$ip.InterfaceIndex
                    $todo += "switch $($ip.InterfaceAlias) to Private"
                }
            }
        }
    }

    # "Allow access" cancelled for Docker Desktop leaves block rules, and a block rule
    # wins over every allow rule
    $blocks = @()
    try {
        $blocks = @(Get-NetFirewallApplicationFilter -ErrorAction Stop |
            Where-Object { [string]$_.Program -match '\\(com\.docker\.backend|com\.docker\.vpnkit|vpnkit)\.exe$' } |
            Get-NetFirewallRule -ErrorAction Stop |
            Where-Object { [string]$_.Direction -eq 'Inbound' -and [string]$_.Action -eq 'Block' -and [string]$_.Enabled -eq 'True' })
    } catch { }
    if ($blocks.Count -gt 0) {
        Say 'Windows Firewall has rules that block Docker Desktop, made when its "Allow'
        Say 'access" question was cancelled. They stop every player, whatever else allows:'
        foreach ($b in $blocks) { Say "  $($b.DisplayName) ($($b.Profile))" }
        $todo += 'turn those block rules off'
    }

    $fw = Join-Path $Checkout 'tools\firewall.ps1'
    if ($todo.Count -eq 0) {
        $FwState = 'kept'
    } elseif (-not (Test-Path -LiteralPath $fw)) {
        $FwState = 'failed'
        Warn "$fw is missing, so the firewall was left alone; allow the ports in docs/ports.md by hand"
    } else {
        Say ('Setup will ' + ($todo -join ', and ') + '.')
        if (-not $same) {
            Say 'The ports, as docs/ports.md lists them:'
            foreach ($sp in $specs) { Say (Format-Spec $sp) }
        }
        Say 'Windows asks for administrator rights for this (a UAC prompt).'
        $argLine = "-NoProfile -ExecutionPolicy Bypass -File `"$fw`" -Group $FwGroup -Rules `"$($specs -join ';')`""
        if ($privateIf) { $argLine += " -PrivateInterface $privateIf" }
        if ($blocks.Count -gt 0) { $argLine += ' -UnblockDocker' }
        if ($DryRun) {
            Say "  would run (elevated): powershell.exe $argLine"
        } elseif (-not (YesNo 'Do this now?' $true)) {
            $FwState = 'failed'
            Say 'Left as it is. docs/ports.md lists the ports to allow by hand.'
        } else {
            $fwLog = Join-Path ([System.IO.Path]::GetTempPath()) ('openlobby-firewall-' + [guid]::NewGuid().ToString('N') + '.txt')
            $argLine += " -Log `"$fwLog`""
            try {
                $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $argLine -Verb RunAs -WindowStyle Hidden -Wait -PassThru
                if (Test-Path -LiteralPath $fwLog) {
                    foreach ($l in [System.IO.File]::ReadAllLines($fwLog)) { Say "  $l" }
                    Remove-Item -LiteralPath $fwLog -Force -ErrorAction SilentlyContinue
                }
                if ($p -and $p.ExitCode -eq 0) {
                    $FwState = 'done'
                    Say 'Done.'
                } else {
                    $FwState = 'failed'
                    Warn 'the firewall step did not finish (the lines above say why); run this script again to retry'
                }
            } catch {
                $FwState = 'failed'
                Warn 'the firewall was left as it is (the UAC prompt was declined?); run this script again to retry'
            }
        }
    }
}

# ------------------------------------------------------------------ doctor

Step 'Checking the setup (tools/doctor.py)'
if ($py) {
    $rc = Run $py @('tools\doctor.py')
    if ($rc -ne 0) { Say '(doctor.py reported problems; each FAIL line says what to do)' }
} else {
    Say 'Python is not installed, so tools\doctor.py was skipped. Install Python 3'
    Say "and run it later from $Checkout."
}

# ------------------------------------------------------------------ client on this PC

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

Step 'Done'
Say 'Admin panel:'
Say '  http://127.0.0.1:8090 (on this PC only)'
if ($AdminState -eq 'new') {
    Say "  User: $AdminUser"
    Say "  Password: $AdminPw"
    Say '  This is the only time it is shown. Change it on the panel, Settings > Sign-in.'
} elseif ($AdminState -eq 'kept') {
    Say "  User: $AdminUser (password unchanged)"
} elseif (-not $DryRun) {
    Say '  No password is set yet, and the panel lets nobody in until one is.'
    Say '  There is no default. Set one now (it asks for it), in PowerShell:'
    Say "    cd `"$Checkout`"; docker compose exec admin python admin.py --set-password"
    Say "  Then sign in as $AdminUser."
}
Say ''
Say 'Players:'
Say '  PC: copy client\Install-PolConnect.ps1 to the player''s PC and run, in an'
Say '  elevated PowerShell:'
Say "    powershell -ExecutionPolicy Bypass -File .\Install-PolConnect.ps1 -Server $Adv"
Say "  PlayStation 2: in the console's network settings, set the primary DNS server"
Say "  to $Ps2Dns and leave the secondary one empty (docs/client.md)."
Say '  Accounts: create them on the admin panel, or registration codes for the'
Say '  in-client sign-up.'
Say ''
if ($UseDeployer) {
    Say 'Games and updates: on the admin panel, Settings > Install games.'
} else {
    Say 'Games: python tools\stack.py (docs/games.md), or run this script with -Deployer'
    Say 'to install and update them from the admin panel.'
}
Say ''
Say 'Firewall (docs/ports.md):'
switch ($FwState) {
    'done' { Say "  Windows Firewall lets players in now (rule group $FwGroup)." }
    'kept' { Say "  Windows Firewall already lets players in (rule group $FwGroup)." }
    'skipped' { Say '  Windows Firewall was left alone.' }
    'failed' { Say '  Windows Firewall was NOT changed; run this script again to retry.' }
}
Say '  To check that consoles can reach this server: OpenLobby Setup, "Check that'
Say '  consoles can reach this server", or python tools\doctor.py in this folder.'
switch ($finalReach) {
    'lan' {
        if ($FwState -ne 'done' -and $FwState -ne 'kept') {
            Say '  On a local network, allow the ports in docs/ports.md through Windows'
            Say '  Firewall for the Private profile if other machines cannot connect.'
        }
    }
    'vpn' {
        if ($FwState -ne 'done' -and $FwState -ne 'kept') {
            Say '  Allow these from the VPN: TCP 80, 51200-51271, 51273-51310, 51330,'
            Say '  53001-53015, 54000 (25 and 110 for generic mail accounts), and 53/udp'
            Say '  for clients using this server''s DNS.'
        }
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
