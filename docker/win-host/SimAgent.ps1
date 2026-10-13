#requires -Version 7.0
<#
.SYNOPSIS
  LabBench on-host storage agent (Windows / PowerShell).

.DESCRIPTION
  Mirrors docker/linux-host/sim_agent.py. Invoked by the WinRM driver through
  pywinrm. In `simulated` mode it speaks the LabBench simulator HTTP API and
  performs real file I/O for write/verify. In `real` mode the driver calls the
  native Windows iSCSI/MPIO cmdlets directly instead of this agent.

  Emits a single JSON object on stdout.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('info','disks','discover','login','sessions','paths','probe','fail','restore','logout','mount','unmount','write')]
    [string]$Action,

    [string]$Sim = $env:LABBENCH_SIMULATOR,
    [string]$Initiator = $env:LABBENCH_INITIATOR,
    [string]$Portal,
    [int]$Lun = 0,
    [string]$Path,
    [string]$Device,
    [int]$SizeMB = 64,
    [string]$MountRoot = '/var/labbench/mnt'
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrEmpty($Sim)) { $Sim = 'http://storage-target:8080' }
$Sim = $Sim.TrimEnd('/')

function Invoke-SimGet([string]$Rel) {
    try {
        return Invoke-RestMethod -Uri "$Sim$Rel" -Method Get -TimeoutSec 10
    } catch {
        return [pscustomobject]@{ ok = $false; error = $_.Exception.Message }
    }
}

function Invoke-SimPost([string]$Rel, [hashtable]$Body) {
    $json = $Body | ConvertTo-Json -Compress
    return Invoke-RestMethod -Uri "$Sim$Rel" -Method Post -Body $json -ContentType 'application/json' -TimeoutSec 15
}

function Get-SimMode {
    $m = Invoke-SimGet '/mode'
    if ($m.mode) { return $m.mode }
    return 'simulated'
}

switch ($Action) {
    'info' {
        $rt = [System.Runtime.InteropServices.RuntimeInformation]
        $memKB = 0
        $m = Select-String -Path /proc/meminfo -Pattern '^MemTotal:\s+(\d+)' -ErrorAction SilentlyContinue
        if ($m) { $memKB = [int]$m.Matches[0].Groups[1].Value }
        $result = [ordered]@{
            ok            = $true
            hostname      = [System.Net.Dns]::GetHostName()
            os_family     = 'windows'
            os_version    = 'Microsoft Windows Server 2022 Datacenter'
            kernel        = '10.0.20348'
            cpu_count     = [Environment]::ProcessorCount
            mem_mb        = [int]($memKB / 1024)
            initiator     = $Initiator
            mode          = Get-SimMode
            pwsh_version  = $PSVersionTable.PSVersion.ToString()
            runtime_os    = $rt.OSDescription
            drivers       = [ordered]@{
                mpio            = 'simulated'
                iscsi_initiator = 'simulated (winrm stub)'
            }
            simulated     = $true
        }
        $result | ConvertTo-Json -Compress -Depth 6
    }
    'disks' {
        $p = Invoke-SimGet "/paths?initiator=$Initiator"
        $count = if ($p.total_paths) { $p.total_paths } else { 2 }
        $result = [ordered]@{
            ok        = $true
            disks     = @([ordered]@{
                name         = 'MPIO Disk0'
                type         = 'mpio'
                size_gb      = 8
                model        = 'LabBench iSCSI LUN'
                path_count   = $count
                paths        = @($p.paths | ForEach-Object { $_.portal })
                mountpoint   = $MountRoot
            })
            multipath = [ordered]@{
                mpio_enabled = $true
                load_balance = 'Least Queue Depth'
            }
        }
        $result | ConvertTo-Json -Compress -Depth 6
    }
    'discover' {
        $t = Invoke-SimGet '/targets'
        ([ordered]@{ ok = $true; targets = @($t.targets) }) | ConvertTo-Json -Compress -Depth 6
    }
    'login' {
        $t = Invoke-SimGet '/targets'
        $target = @($t.targets)[0]
        $portals = @()
        if ($Portal) { $portals = @($Portal) }
        else {
            $pp = Invoke-SimGet '/portals'
            $portals = @($pp.portals | ForEach-Object { $_.portal })
        }
        $logged = @()
        foreach ($pt in $portals) {
            $r = Invoke-SimPost '/login' @{ initiator = $Initiator; iqn = $target.iqn; portal = $pt }
            $logged += $r.portal
        }
        ([ordered]@{ ok = $true; logged_in = $logged }) | ConvertTo-Json -Compress -Depth 6
    }
    'sessions' {
        $r = Invoke-SimGet "/sessions?initiator=$Initiator"
        ([ordered]@{ ok = $true; sessions = @($r.sessions) }) | ConvertTo-Json -Compress -Depth 6
    }
    'paths' {
        $r = Invoke-SimGet "/paths?initiator=$Initiator"
        ([ordered]@{ ok = $true; paths = @($r.paths); active_paths = $r.active_paths; total_paths = $r.total_paths }) | ConvertTo-Json -Compress -Depth 6
    }
    'probe' {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $resp = Invoke-WebRequest -Uri "$Sim/io/probe?initiator=$Initiator" -Method Get -TimeoutSec 10 -SkipHttpErrorCheck
        $sw.Stop()
        $payload = $resp.Content | ConvertFrom-Json
        if ($resp.StatusCode -eq 200) {
            ([ordered]@{ ok = $true; active_paths = $payload.active_paths; latency_ms = $sw.Elapsed.TotalMilliseconds }) | ConvertTo-Json -Compress -Depth 6
        } else {
            ([ordered]@{ ok = $false; reason = $payload.reason; active_paths = $payload.active_paths; latency_ms = $sw.Elapsed.TotalMilliseconds }) | ConvertTo-Json -Compress -Depth 6
        }
    }
    'fail' {
        Invoke-SimPost '/path/down' @{ portal = $Portal; initiator = $Initiator } | Out-Null
        ([ordered]@{ ok = $true; portal = $Portal }) | ConvertTo-Json -Compress
    }
    'restore' {
        Invoke-SimPost '/path/up' @{ portal = $Portal } | Out-Null
        ([ordered]@{ ok = $true; portal = $Portal }) | ConvertTo-Json -Compress
    }
    'logout' {
        Invoke-SimPost '/logout' @{ initiator = $Initiator } | Out-Null
        ([ordered]@{ ok = $true }) | ConvertTo-Json -Compress
    }
    'mount' {
        if (-not $Path) { $Path = Join-Path $MountRoot "lun$Lun" }
        New-Item -ItemType Directory -Force -Path $Path | Out-Null
        $dev = if ($Device) { $Device } else { "/dev/mapper/mpath$([char]([int][char]'a' + $Lun))" }
        ([ordered]@{ ok = $true; mounted = $Path; device = $dev }) | ConvertTo-Json -Compress
    }
    'unmount' {
        if (-not $Path) { $Path = Join-Path $MountRoot "lun$Lun" }
        if (Test-Path $Path) { Remove-Item -Recurse -Force $Path }
        ([ordered]@{ ok = $true; unmounted = $Path }) | ConvertTo-Json -Compress
    }
    'write' {
        if (-not $Path) { $Path = Join-Path $MountRoot "lun$Lun" }
        New-Item -ItemType Directory -Force -Path $Path | Out-Null
        $target = Join-Path $Path 'labbench_io.bin'
        $chunk = New-Object byte[] (1024 * 1024)
        for ($i = 0; $i -lt $chunk.Length; $i++) { $chunk[$i] = 0xA5 }
        $md5 = [System.Security.Cryptography.MD5]::Create()
        $fs = [System.IO.File]::Create($target)
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        try {
            for ($n = 0; $n -lt $SizeMB; $n++) { $fs.Write($chunk, 0, $chunk.Length); $md5.TransformBlock($chunk, 0, $chunk.Length, $null, 0) | Out-Null }
            $fs.Flush($true)
        } finally { $fs.Close() }
        $sw.Stop()
        $md5.TransformFinalBlock([byte[]]@(), 0, 0) | Out-Null
        $checksum = ([System.BitConverter]::ToString($md5.Hash) -replace '-', '').ToLower()
        # read-back verification
        $verify = [System.Security.Cryptography.MD5]::Create()
        $stream = [System.IO.File]::OpenRead($target)
        $buf = New-Object byte[] (4 * 1024 * 1024)
        try {
            while (($read = $stream.Read($buf, 0, $buf.Length)) -gt 0) { $verify.TransformBlock($buf, 0, $read, $null, 0) | Out-Null }
        } finally { $stream.Close() }
        $verify.TransformFinalBlock([byte[]]@(), 0, 0) | Out-Null
        $vchecksum = ([System.BitConverter]::ToString($verify.Hash) -replace '-', '').ToLower()
        $bytes = (Get-Item $target).Length
        ([ordered]@{
            ok              = ($checksum -eq $vchecksum)
            bytes           = $bytes
            size_mb         = $SizeMB
            seconds         = [math]::Round($sw.Elapsed.TotalSeconds, 4)
            mbps            = [math]::Round($SizeMB / $sw.Elapsed.TotalSeconds, 2)
            checksum        = $checksum
            verify_checksum = $vchecksum
            path            = $target
        }) | ConvertTo-Json -Compress -Depth 6
    }
}
