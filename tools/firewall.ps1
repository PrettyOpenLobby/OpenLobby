<#
.SYNOPSIS
    Lets players through Windows Firewall to an OpenLobby server, or takes
    those rules away again.

.DESCRIPTION
    setup.ps1 runs this with administrator rights (one UAC prompt). Every rule
    it makes belongs to one rule group, OpenLobby by default, so they can be
    listed and removed together:

        Get-NetFirewallRule -Group OpenLobby
        Remove-NetFirewallRule -Group OpenLobby

    Each rule's description is the spec it was made from, so setup.ps1 can
    tell whether the rules in place are the ones it wants.

    Each run first removes the group's rules, then adds the ones it is given,
    so running it again replaces them.

    -Rules is a list of rules separated by ';', each one
    PROTOCOL:PORTS:PROFILES:REMOTE with commas inside a field, for example
    UDP:53:Private:LocalSubnet;TCP:80,51200-51271:Private:LocalSubnet

    -PrivateInterface N switches the network on interface N to the Private
    profile. -UnblockDocker turns off the inbound Block rules Windows made for
    Docker Desktop's port forwarder when its "Allow access" question was
    cancelled; a Block rule wins over any Allow rule. -Remove only removes the
    group. -Log FILE writes what was done, since the elevated window closes at
    once. docs/ports.md, "Windows Firewall".

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\firewall.ps1 -Remove
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Group = 'OpenLobby',
    [string]$Rules = '',
    [int]$PrivateInterface = 0,
    [switch]$UnblockDocker,
    [switch]$Remove,
    [string]$Log = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$done = New-Object System.Collections.Generic.List[string]
function Note([string]$text) { $done.Add($text); Write-Host $text }

# The programs Docker Desktop publishes container ports through, by version:
# com.docker.backend.exe (current releases) and vpnkit.exe (older ones).
$DockerForwarders = '\\(com\.docker\.backend|com\.docker\.vpnkit|vpnkit)\.exe$'

function ConvertTo-Rule([string]$spec, [int]$n) {
    $f = $spec.Split(':')
    if ($f.Count -ne 4) { throw "bad rule '$spec' (want PROTOCOL:PORTS:PROFILES:REMOTE)" }
    $proto = $f[0].ToUpper()
    if ($proto -ne 'TCP' -and $proto -ne 'UDP') { throw "bad protocol in '$spec'" }
    $ports = @($f[1].Split(',') | Where-Object { $_ })
    foreach ($p in $ports) { if ($p -notmatch '^\d{1,5}(-\d{1,5})?$') { throw "bad port '$p' in '$spec'" } }
    $profiles = @($f[2].Split(',') | Where-Object { $_ })
    foreach ($p in $profiles) { if ($p -notmatch '^(Private|Public|Domain)$') { throw "bad profile '$p' in '$spec'" } }
    $remote = @($f[3].Split(',') | Where-Object { $_ })
    foreach ($r in $remote) { if ($r -notmatch '^(Any|LocalSubnet|\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?)$') { throw "bad remote address '$r' in '$spec'" } }
    $what = 'TCP'
    if ($proto -eq 'UDP') { $what = 'UDP' }
    return @{
        Spec = $spec
        Name = "$Group-$n"
        DisplayName = ("$Group $what " + ($ports -join ', ') + ' (' + ($profiles -join ', ') + ')')
        Protocol = $proto
        LocalPort = [string[]]$ports
        Profile = ($profiles -join ',')
        RemoteAddress = [string[]]$remote
    }
}

# Preference variables do not reach the NetSecurity cmdlets (they are module
# functions), so -ErrorAction and -WhatIf are passed to each one.
$wi = [bool]$WhatIfPreference
$would = ''
if ($wi) { $would = 'would be ' }

$code = 0
try {
    # validate everything before changing anything
    $specs = @()
    $n = 0
    foreach ($s in $Rules.Split(';')) {
        if (-not $s.Trim()) { continue }
        $n++
        $specs += ,(ConvertTo-Rule $s.Trim() $n)
    }

    $old = @(Get-NetFirewallRule -Group $Group -ErrorAction SilentlyContinue)
    if ($old.Count -gt 0) {
        $old | Remove-NetFirewallRule -ErrorAction Stop -WhatIf:$wi
        Note "$($would)removed: the $($old.Count) rule(s) in group $Group"
    }
    if (-not $Remove) {
        foreach ($r in $specs) {
            New-NetFirewallRule -Name $r.Name -DisplayName $r.DisplayName -Group $Group -Description $r.Spec `
                -Direction Inbound -Action Allow -Protocol $r.Protocol -LocalPort $r.LocalPort `
                -Profile $r.Profile -RemoteAddress $r.RemoteAddress -ErrorAction Stop -WhatIf:$wi | Out-Null
            Note ("$($would)added: " + $r.DisplayName + ', from ' + ($r.RemoteAddress -join ', '))
        }
    }
    if ($PrivateInterface -gt 0) {
        Set-NetConnectionProfile -InterfaceIndex $PrivateInterface -NetworkCategory Private -ErrorAction Stop -WhatIf:$wi
        Note "$($would)set to Private: the network on interface $PrivateInterface"
    }
    if ($UnblockDocker) {
        $blocks = @(Get-NetFirewallApplicationFilter -ErrorAction Stop | Where-Object { $_.Program -match $DockerForwarders } |
            Get-NetFirewallRule | Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' -and $_.Enabled -eq 'True' })
        foreach ($b in $blocks) {
            $b | Disable-NetFirewallRule -ErrorAction Stop -WhatIf:$wi
            Note ("$($would)turned off: block rule " + $b.DisplayName + ' (' + $b.Profile + ')')
        }
    }
} catch {
    Note ("error: " + $_.Exception.Message)
    $code = 1
} finally {
    if ($Log) {
        try { [System.IO.File]::WriteAllLines($Log, [string[]]$done) } catch { }
    }
}
exit $code
