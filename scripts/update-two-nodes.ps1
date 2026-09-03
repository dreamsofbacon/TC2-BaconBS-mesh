param(
  [string]$ConfigPath = "$PSScriptRoot\node-update-config.json",
  [switch]$ResetCredential,
  [switch]$Reboot
)

$ErrorActionPreference = "Stop"
Write-Warning "update-two-nodes.ps1 is retained for compatibility; use deploy-fleet.ps1 for node selection and validation."
& "$PSScriptRoot\deploy-fleet.ps1" @PSBoundParameters
