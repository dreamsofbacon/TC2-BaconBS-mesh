param(
  [string]$ConfigPath = "$PSScriptRoot\node-update-config.json",
  [switch]$ResetCredential
)

$ErrorActionPreference = "Stop"

function Ensure-PoshSshModule {
  if (-not (Get-Module -ListAvailable -Name Posh-SSH)) {
    Write-Host "Posh-SSH module not found. Installing for current user..."
    Install-Module -Name Posh-SSH -Scope CurrentUser -Force -AllowClobber
  }
  Import-Module Posh-SSH -ErrorAction Stop
}

function Get-StoredCredentialPath {
  $dir = Join-Path $env:APPDATA "TC2-BaconBS"
  if (-not (Test-Path $dir)) {
    New-Item -ItemType Directory -Path $dir | Out-Null
  }
  return Join-Path $dir "node-update-cred.xml"
}

function Get-OrCreateCredential {
  param([string]$CredPath, [bool]$ForceReset)

  if ($ForceReset -and (Test-Path $CredPath)) {
    Remove-Item $CredPath -Force
  }

  if (Test-Path $CredPath) {
    return Import-Clixml -Path $CredPath
  }

  Write-Host "No saved credential found. Enter SSH username/password (saved for next run)."
  $cred = Get-Credential -Message "SSH credential for remote update"
  $cred | Export-Clixml -Path $CredPath
  return $cred
}

function Invoke-RemoteUpdate {
  param(
    [pscustomobject]$Node,
    [pscredential]$Credential,
    [pscustomobject]$Config
  )

  $port = if ($Node.PSObject.Properties.Name -contains "port") { [int]$Node.port } else { 22 }
  $remoteScript = if ($Config.PSObject.Properties.Name -contains "remoteScriptPath") { $Config.remoteScriptPath } else { "~/remote-node-update.sh" }
  $repoPath = if ($Config.PSObject.Properties.Name -contains "repoPath") { $Config.repoPath } else { "~/TC2-BaconBS-mesh" }
  $branch = if ($Config.PSObject.Properties.Name -contains "branch") { $Config.branch } else { "main" }
  $services = if ($Config.PSObject.Properties.Name -contains "services") { ($Config.services -join ' ') } else { "mesh-bbs.service bacon-web-admin.service" }

  Write-Host "Connecting to $($Node.host):$port ..."
  $session = New-SSHSession -ComputerName $Node.host -Port $port -Credential $Credential -AcceptKey
  try {
    $command = "bash '$remoteScript' --repo-path '$repoPath' --branch '$branch' --services '$services'"
    $result = Invoke-SSHCommand -SSHSession $session -Command $command -TimeOut 1200

    if ($result.Output) {
      Write-Host "[$($Node.host)] output:"
      $result.Output | ForEach-Object { Write-Host "  $_" }
    }

    if ($result.ExitStatus -ne 0) {
      throw "Remote update failed on $($Node.host) with exit code $($result.ExitStatus)"
    }

    Write-Host "[$($Node.host)] update succeeded"
  }
  finally {
    Remove-SSHSession -SSHSession $session | Out-Null
  }
}

if (-not (Test-Path $ConfigPath)) {
  throw "Config file not found: $ConfigPath"
}

$config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
if (-not $config.nodes -or $config.nodes.Count -lt 1) {
  throw "Config must include at least one node in 'nodes'."
}

Ensure-PoshSshModule
$credPath = Get-StoredCredentialPath
$credential = Get-OrCreateCredential -CredPath $credPath -ForceReset:$ResetCredential.IsPresent

foreach ($node in $config.nodes) {
  Invoke-RemoteUpdate -Node $node -Credential $credential -Config $config
}

Write-Host "All node updates completed."
