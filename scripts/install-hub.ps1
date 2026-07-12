param(
  [string]$InstallDir = "$env:USERPROFILE\stream-control-hub",
  [string]$RepoUrl = "https://github.com/himydearfriends1934-cmyk/stream-control-hub.git",
  [string]$Branch = "main",
  [string]$HostName = "",
  [int]$Port = 0,
  [string]$TrustedRemoteWrites = "",
  [string]$TailscaleAuthKey = "",
  [string]$TailscaleHostname = "stream-control-hub",
  [ValidateSet("install", "uninstall")]
  [string]$Action = "",
  [switch]$Uninstall,
  [switch]$RemoveData,
  [switch]$NoStart
)

$ErrorActionPreference = "Stop"

if ($Uninstall) { $Action = "uninstall" }
if (-not $Action) { $Action = $env:STREAM_HUB_ACTION }
if (-not $Action) { $Action = "install" }
if (-not $RemoveData -and $env:STREAM_HUB_REMOVE_DATA -match "^(1|true|yes)$") { $RemoveData = $true }

function New-Token {
  $bytes = New-Object byte[] 32
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    $rng.GetBytes($bytes)
  } finally {
    $rng.Dispose()
  }
  return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Require-Command($name) {
  if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
    throw "$name is required. Install it and run this installer again."
  }
}

function Stop-HubProcesses {
  $resolved = [System.IO.Path]::GetFullPath($InstallDir)
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
      $_.CommandLine -and
      $_.CommandLine.Contains($resolved) -and
      ($_.CommandLine -match "stream_control_hub|run-hub\.ps1")
    } |
    ForEach-Object {
      Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Uninstall-Hub {
  Stop-HubProcesses
  if (-not (Test-Path -LiteralPath $InstallDir)) {
    Write-Host "Stream Control Hub is not installed at: $InstallDir"
    return
  }
  if ($RemoveData) {
    Remove-Item -LiteralPath $InstallDir -Recurse -Force
    Write-Host "Stream Control Hub uninstalled. Data removed: $InstallDir"
    return
  }
  foreach ($name in @(".venv", ".git", "stream_control_hub", "scripts", "config", "requirements.txt", "README.md", "run-hub.ps1")) {
    Remove-Item -LiteralPath (Join-Path $InstallDir $name) -Recurse -Force -ErrorAction SilentlyContinue
  }
  Write-Host "Stream Control Hub uninstalled. Data preserved in: $InstallDir"
  Write-Host "Use -RemoveData or STREAM_HUB_REMOVE_DATA=1 to remove saved data and local config too."
}

if ($Action -eq "uninstall") {
  Uninstall-Hub
  exit 0
}

Require-Command git
Require-Command python

if (Test-Path $InstallDir) {
  if (-not (Test-Path (Join-Path $InstallDir ".git"))) {
    throw "InstallDir exists but is not a git checkout: $InstallDir"
  }
  git -C $InstallDir fetch origin $Branch
  git -C $InstallDir checkout $Branch
  git -C $InstallDir pull --ff-only origin $Branch
} else {
  git clone --branch $Branch $RepoUrl $InstallDir
}

$venv = Join-Path $InstallDir ".venv"
$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $python)) {
  python -m venv $venv
}
& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $InstallDir "requirements.txt")

$dataDir = Join-Path $InstallDir "data"
$nodesFile = Join-Path $dataDir "nodes.local.json"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
if (-not (Test-Path $nodesFile)) {
  "[]" | Set-Content -LiteralPath $nodesFile -Encoding UTF8
}

$envFile = Join-Path $InstallDir ".env"
$token = ""
$existingHost = ""
$existingPort = 0
$existingTrustedRemoteWrites = ""
$existingYoutubeClientId = ""
$existingYoutubeClientSecret = ""
$existingYoutubeCredentialFile = ""
if (Test-Path $envFile) {
  $existing = Select-String -LiteralPath $envFile -Pattern "^STREAM_HUB_CONTROL_TOKEN=(.+)$" -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($existing) { $token = $existing.Matches[0].Groups[1].Value }
  $existing = Select-String -LiteralPath $envFile -Pattern "^STREAM_HUB_HOST=(.+)$" -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($existing) { $existingHost = $existing.Matches[0].Groups[1].Value }
  $existing = Select-String -LiteralPath $envFile -Pattern "^STREAM_HUB_PORT=(\d+)$" -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($existing) { $existingPort = [int]$existing.Matches[0].Groups[1].Value }
  $existing = Select-String -LiteralPath $envFile -Pattern "^STREAM_HUB_TRUSTED_REMOTE_WRITES=(.+)$" -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($existing) { $existingTrustedRemoteWrites = $existing.Matches[0].Groups[1].Value }
  $existing = Select-String -LiteralPath $envFile -Pattern "^YOUTUBE_CLIENT_ID=(.*)$" -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($existing) { $existingYoutubeClientId = $existing.Matches[0].Groups[1].Value }
  $existing = Select-String -LiteralPath $envFile -Pattern "^YOUTUBE_CLIENT_SECRET=(.*)$" -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($existing) { $existingYoutubeClientSecret = $existing.Matches[0].Groups[1].Value }
  $existing = Select-String -LiteralPath $envFile -Pattern "^YOUTUBE_CREDENTIAL_FILE=(.*)$" -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($existing) { $existingYoutubeCredentialFile = $existing.Matches[0].Groups[1].Value }
}
if (-not $token) { $token = New-Token }
if (-not $HostName) { $HostName = if ($existingHost) { $existingHost } else { "127.0.0.1" } }
if ($Port -le 0) { $Port = if ($existingPort -gt 0) { $existingPort } else { 8788 } }
if (-not $TrustedRemoteWrites) {
  $TrustedRemoteWrites = if ($existingTrustedRemoteWrites) { $existingTrustedRemoteWrites } else { "0" }
}
$youtubeClientId = if ($env:YOUTUBE_CLIENT_ID) { $env:YOUTUBE_CLIENT_ID } else { $existingYoutubeClientId }
$youtubeClientSecret = if ($env:YOUTUBE_CLIENT_SECRET) { $env:YOUTUBE_CLIENT_SECRET } else { $existingYoutubeClientSecret }
$youtubeCredentialFile = if ($env:YOUTUBE_CREDENTIAL_FILE) { $env:YOUTUBE_CREDENTIAL_FILE } elseif ($existingYoutubeCredentialFile) { $existingYoutubeCredentialFile } else { Join-Path $dataDir "youtube_credentials.json" }
if ($TrustedRemoteWrites -match "^(?i:1|true|yes)$") {
  $TrustedRemoteWrites = "1"
} elseif ($TrustedRemoteWrites -match "^(?i:0|false|no)$") {
  $TrustedRemoteWrites = "0"
} else {
  throw "TrustedRemoteWrites must be 0 or 1."
}

@(
  "STREAM_HUB_CONTROL_TOKEN=$token",
  "STREAM_HUB_NODES_FILE=$nodesFile",
  "STREAM_HUB_HOST=$HostName",
  "STREAM_HUB_PORT=$Port",
  "STREAM_HUB_TRUSTED_REMOTE_WRITES=$TrustedRemoteWrites"
  "YOUTUBE_CLIENT_ID=$youtubeClientId"
  "YOUTUBE_CLIENT_SECRET=$youtubeClientSecret"
  "YOUTUBE_CREDENTIAL_FILE=$youtubeCredentialFile"
) | Set-Content -LiteralPath $envFile -Encoding UTF8

$runScript = Join-Path $InstallDir "run-hub.ps1"
@(
  '$ErrorActionPreference = "Stop"',
  "Set-Location -LiteralPath `"$InstallDir`"",
  "& `"$python`" -m stream_control_hub"
) | Set-Content -LiteralPath $runScript -Encoding UTF8

if ($TailscaleAuthKey) {
  if (Get-Command tailscale -ErrorAction SilentlyContinue) {
    tailscale up --auth-key $TailscaleAuthKey --hostname $TailscaleHostname --accept-dns=false
  } else {
    Write-Warning "tailscale is not installed. Install Tailscale, then use the Hub Tailscale panel or rerun with -TailscaleAuthKey."
  }
}

if (-not $NoStart) {
  Start-Process powershell -WindowStyle Hidden -ArgumentList @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $runScript
  )
}

Write-Host "Stream Control Hub installed."
Write-Host "Open: http://127.0.0.1:$Port/?token=$token"
Write-Host "Nodes file: $nodesFile"
Write-Host "Trusted remote writes: $TrustedRemoteWrites"
