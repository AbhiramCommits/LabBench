<#
.SYNOPSIS
  LabBench Windows lab-configuration collector.

.DESCRIPTION
  Emits a single JSON object describing the host OS, disk topology, MPIO
  configuration, iSCSI initiator and driver versions. Mirrors the schema of
  scripts/collect_lab_config.sh.
#>
[CmdletBinding()]
param(
    [string]$Sim = $env:LABBENCH_SIMULATOR,
    [string]$Initiator = $env:LABBENCH_INITIATOR,
    [string]$Agent = '/opt/labbench/SimAgent.ps1'
)

$ErrorActionPreference = 'Continue'
if ([string]::IsNullOrEmpty($Sim)) { $Sim = 'http://storage-target:8080' }

function Get-AgentJson([string]$Action) {
    if (Test-Path $Agent) {
        try {
            $raw = & $Agent -Action $Action -Sim $Sim -Initiator $Initiator 2>$null
            if ($raw) { return ($raw | ConvertFrom-Json) }
        } catch { }
    }
    return $null
}

$paths = Get-AgentJson 'paths'
$disks = Get-AgentJson 'disks'

$diskList = @()
if ($disks -and $disks.disks) {
    foreach ($d in $disks.disks) {
        $diskList += [ordered]@{
            name       = $d.name
            size_gb    = $d.size_gb
            model      = $d.model
            path_count = $d.path_count
            paths      = @($d.paths)
            mountpoint = $d.mountpoint
        }
    }
}

$osVersion = 'Microsoft Windows Server 2022 Datacenter'
$kernel = '10.0.20348'
$isWindowsReal = $false
if (Get-Variable -Name IsWindows -ErrorAction SilentlyContinue) { $isWindowsReal = $IsWindows }
$diskTimeout = $null
if ($isWindowsReal) {
    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        $osVersion = $os.Caption
        $kernel = "$($os.Version)"
        $diskTimeout = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\Disk').TimeOutValue
    } catch { }
} else {
    $tuning = '/var/labbench/win/tuning.json'
    if (Test-Path $tuning) {
        try { $diskTimeout = (Get-Content -Raw $tuning | ConvertFrom-Json).disk_timeout_s } catch { }
    }
}

$out = [ordered]@{
    hostname        = [System.Net.Dns]::GetHostName()
    os_family       = 'windows'
    os_version      = $osVersion
    kernel          = $kernel
    cpu_count       = [Environment]::ProcessorCount
    mem_mb          = 0
    disks           = $diskList
    multipath       = [ordered]@{
        enabled      = $true
        mpio_enabled = $true
        load_balance = 'Least Queue Depth'
    }
    initiator       = $Initiator
    disk_timeout_s  = $diskTimeout
    drivers         = [ordered]@{
        mpio            = 'simulated'
        iscsi_initiator = 'simulated (winrm stub)'
        pwsh            = $PSVersionTable.PSVersion.ToString()
    }
    simulated       = (-not $isWindowsReal)
    timestamp       = (Get-Date).ToUniversalTime().ToString('o')
}
$out | ConvertTo-Json -Compress -Depth 6
