param(
  [string]$ConfigPath = "$PSScriptRoot\node-update-config.json",
  [string[]]$Node,
  [switch]$ValidateOnly,
  [switch]$ResetCredential,
  [switch]$Reboot,
  [switch]$StopOnError
)

$ErrorActionPreference = "Stop"

function Get-PropertyValue {
  param($Object, [string]$Name, $Default)
  if ($null -ne $Object -and $Object.PSObject.Properties.Name -contains $Name) {
    return $Object.$Name
  }
  return $Default
}

function Resolve-NodeSettings {
  param([pscustomobject]$FleetConfig, [pscustomobject]$NodeConfig, [int]$Index)

  $hostName = [string](Get-PropertyValue $NodeConfig "host" "")
  $displayName = [string](Get-PropertyValue $NodeConfig "name" $hostName)
  if ([string]::IsNullOrWhiteSpace($hostName)) {
    throw "Node $($Index + 1) is missing 'host'."
  }
  if ([string]::IsNullOrWhiteSpace($displayName)) {
    throw "Node $($Index + 1) is missing both 'name' and 'host'."
  }

  $defaultServices = @("mesh-bbs.service", "bacon-web-admin.service")
  $services = @(Get-PropertyValue $NodeConfig "services" (Get-PropertyValue $FleetConfig "services" $defaultServices))
  if ($services.Count -eq 0) {
    throw "Node '$displayName' must specify at least one service."
  }

  [pscustomobject]@{
    Name = $displayName
    Host = $hostName
    Port = [int](Get-PropertyValue $NodeConfig "port" 22)
    Enabled = [bool](Get-PropertyValue $NodeConfig "enabled" $true)
    RepoPath = [string](Get-PropertyValue $NodeConfig "repoPath" (Get-PropertyValue $FleetConfig "repoPath" "~/TC2-BaconBS-mesh"))
    Branch = [string](Get-PropertyValue $NodeConfig "branch" (Get-PropertyValue $FleetConfig "branch" "main"))
    Services = $services
  }
}

function Read-FleetConfig {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "Config file not found: $Path"
  }
  try {
    $config = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
  }
  catch {
    throw "Config file is not valid JSON: $($_.Exception.Message)"
  }
  if (-not $config.nodes -or @($config.nodes).Count -lt 1) {
    throw "Config must include at least one node in 'nodes'."
  }

  $resolved = for ($index = 0; $index -lt @($config.nodes).Count; $index++) {
    Resolve-NodeSettings -FleetConfig $config -NodeConfig @($config.nodes)[$index] -Index $index
  }
  $duplicates = @($resolved | Group-Object Name | Where-Object Count -gt 1)
  if ($duplicates.Count -gt 0) {
    throw "Node names must be unique; duplicate(s): $($duplicates.Name -join ', ')"
  }
  return @($resolved)
}

function Ensure-PoshSshModule {
  if (-not (Get-Module -ListAvailable -Name Posh-SSH)) {
    Write-Host "Posh-SSH module not found. Installing for current user..."
    Install-Module -Name Posh-SSH -Scope CurrentUser -Force -AllowClobber
  }
  Import-Module Posh-SSH -ErrorAction Stop
}

function Get-OrCreateCredential {
  param([bool]$ForceReset)
  $directory = Join-Path $env:APPDATA "TC2-BaconBS"
  $credentialPath = Join-Path $directory "node-update-cred.xml"
  if (-not (Test-Path $directory)) {
    New-Item -ItemType Directory -Path $directory | Out-Null
  }
  if ($ForceReset -and (Test-Path $credentialPath)) {
    Remove-Item $credentialPath -Force
  }
  if (Test-Path $credentialPath) {
    return Import-Clixml -Path $credentialPath
  }
  Write-Host "No saved credential found. Enter the SSH username/password (saved for future runs)."
  $credential = Get-Credential -Message "SSH credential for fleet deployment"
  $credential | Export-Clixml -Path $credentialPath
  return $credential
}

function Quote-BashArgument {
  param([string]$Value)
  $singleQuote = [string][char]39
  $doubleQuote = [string][char]34
  $escapedQuote = $singleQuote + $doubleQuote + $singleQuote + $doubleQuote + $singleQuote
  return $singleQuote + $Value.Replace($singleQuote, $escapedQuote) + $singleQuote
}

function ConvertTo-BashPathExpression {
  param([string]$Value)
  if ($Value -eq "~") {
    return '"$HOME"'
  }
  if ($Value.StartsWith("~/")) {
    return '"$HOME"/' + (Quote-BashArgument $Value.Substring(2))
  }
  return Quote-BashArgument $Value
}

function Invoke-NodeDeployment {
  param([pscustomobject]$Settings, [pscredential]$Credential, [bool]$ShouldReboot)

  Write-Host "[$($Settings.Name)] connecting to $($Settings.Host):$($Settings.Port) ..."
  $session = New-SSHSession -ComputerName $Settings.Host -Port $Settings.Port -Credential $Credential -AcceptKey
  try {
    $repoPath = ConvertTo-BashPathExpression $Settings.RepoPath
    if ($ShouldReboot) {
      $command = "cd $repoPath && git fetch --all --prune && git checkout $(Quote-BashArgument $Settings.Branch) && git pull --ff-only origin $(Quote-BashArgument $Settings.Branch) && sudo /sbin/shutdown -r now"
      Write-Host "[$($Settings.Name)] pulling $($Settings.Branch) and rebooting ..."
    }
    else {
      $serviceList = $Settings.Services -join ' '
      $command = "bash $repoPath/scripts/remote-node-update.sh --repo-path $repoPath --branch $(Quote-BashArgument $Settings.Branch) --services $(Quote-BashArgument $serviceList)"
      Write-Host "[$($Settings.Name)] deploying $($Settings.Branch) ..."
    }

    try {
      $result = Invoke-SSHCommand -SSHSession $session -Command $command -TimeOut 1200
    }
    catch {
      if ($ShouldReboot) {
        Write-Host "[$($Settings.Name)] reboot command issued (connection closed)"
        return
      }
      throw
    }
    if ($result.Output) {
      $result.Output | ForEach-Object { Write-Host "  $_" }
    }
    if ($result.Error) {
      $result.Error | ForEach-Object { Write-Host "  [stderr] $_" }
    }
    if ($result.ExitStatus -ne 0) {
      throw "remote command exited with code $($result.ExitStatus)"
    }
  }
  finally {
    Remove-SSHSession -SSHSession $session -ErrorAction SilentlyContinue | Out-Null
  }
}

$nodes = Read-FleetConfig -Path $ConfigPath
if ($Node.Count -gt 0) {
  $unknownNames = @($Node | Where-Object { $_ -notin $nodes.Name })
  if ($unknownNames.Count -gt 0) {
    throw "Unknown node name(s): $($unknownNames -join ', ')"
  }
  $nodes = @($nodes | Where-Object Name -in $Node)
}
else {
  $nodes = @($nodes | Where-Object Enabled)
}
if ($nodes.Count -eq 0) {
  throw "No enabled nodes selected."
}

Write-Host "Fleet config valid: $($nodes.Count) node(s) selected."
$nodes | ForEach-Object {
  Write-Host "  $($_.Name): $($_.Host):$($_.Port), branch=$($_.Branch), repo=$($_.RepoPath)"
}
if ($ValidateOnly) {
  return
}

Ensure-PoshSshModule
$credential = Get-OrCreateCredential -ForceReset:$ResetCredential.IsPresent
$failed = @()
foreach ($settings in $nodes) {
  try {
    Invoke-NodeDeployment -Settings $settings -Credential $credential -ShouldReboot:$Reboot.IsPresent
    Write-Host "[$($settings.Name)] succeeded"
  }
  catch {
    $failed += $settings.Name
    Write-Error "[$($settings.Name)] failed: $($_.Exception.Message)" -ErrorAction Continue
    if ($StopOnError) {
      throw
    }
  }
}

if ($failed.Count -gt 0) {
  throw "Fleet deployment failed on $($failed.Count) node(s): $($failed -join ', ')"
}
Write-Host "Fleet deployment completed on all $($nodes.Count) node(s)."