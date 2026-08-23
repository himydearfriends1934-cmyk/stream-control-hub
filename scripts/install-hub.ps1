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

function Read-EnvFileValues([string]$Path) {
  $values = @{}
  if (-not (Test-Path -LiteralPath $Path)) {
    return $values
  }
  $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue
  if ($null -eq $raw) {
    return $values
  }
  $normalized = $raw.Replace("\r\n", "`n").Replace("\n", "`n").Replace("\r", "`n").Replace("`r`n", "`n").Replace("`r", "`n")
  $pattern = [regex]'(?:STREAM_[A-Z0-9_]+|YOUTUBE_[A-Z0-9_]+)='
  foreach ($rawLine in ($normalized -split "`n")) {
    $trimmed = $rawLine.Trim()
    if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $rawLine.Contains("=")) {
      continue
    }
    $matches = $pattern.Matches($rawLine)
    $segments = @()
    if ($matches.Count -gt 1) {
      for ($i = 0; $i -lt $matches.Count; $i++) {
        $start = $matches[$i].Index
        $end = if ($i + 1 -lt $matches.Count) { $matches[$i + 1].Index } else { $rawLine.Length }
        $segments += $rawLine.Substring($start, $end - $start)
      }
    } else {
      $segments = @($rawLine)
    }
    foreach ($segment in $segments) {
      $parts = $segment.Split("=", 2)
      if ($parts.Count -ne 2) {
        continue
      }
      $key = $parts[0].Trim()
      if ($key -and -not $values.ContainsKey($key)) {
        $values[$key] = $parts[1].Trim().Trim('"').Trim("'")
      }
    }
  }
  return $values
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
$mediaDir = Join-Path $InstallDir "media"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
New-Item -ItemType Directory -Force -Path $mediaDir | Out-Null
if (-not (Test-Path $nodesFile)) {
  "[]" | Set-Content -LiteralPath $nodesFile -Encoding UTF8
}
foreach ($legacyDir in @((Join-Path $InstallDir "agent_data\media"), (Join-Path $InstallDir "data\media"))) {
  if (-not (Test-Path -LiteralPath $legacyDir -PathType Container)) { continue }
  if ([System.IO.Path]::GetFullPath($legacyDir) -eq [System.IO.Path]::GetFullPath($mediaDir)) { continue }
  Get-ChildItem -LiteralPath $legacyDir -File -Force -ErrorAction SilentlyContinue | ForEach-Object {
    $destination = Join-Path $mediaDir $_.Name
    if (Test-Path -LiteralPath $destination) {
      $same = $false
      try {
        $same = ((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash -eq (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash)
      } catch {}
      if ($same) {
        Remove-Item -LiteralPath $_.FullName -Force
        return
      }
      $counter = 1
      do {
        $candidate = Join-Path $mediaDir ("{0}-legacy-{1}{2}" -f $_.BaseName, $counter, $_.Extension)
        $counter++
      } while (Test-Path -LiteralPath $candidate)
      $destination = $candidate
    }
    Move-Item -LiteralPath $_.FullName -Destination $destination
  }
  Remove-Item -LiteralPath $legacyDir -Force -ErrorAction SilentlyContinue
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
  $existingValues = Read-EnvFileValues $envFile
  if ($existingValues.ContainsKey("STREAM_HUB_CONTROL_TOKEN")) { $token = $existingValues["STREAM_HUB_CONTROL_TOKEN"] }
  if ($existingValues.ContainsKey("STREAM_HUB_HOST")) { $existingHost = $existingValues["STREAM_HUB_HOST"] }
  if ($existingValues.ContainsKey("STREAM_HUB_PORT")) {
    $parsedPort = 0
    if ([int]::TryParse($existingValues["STREAM_HUB_PORT"], [ref]$parsedPort)) {
      $existingPort = $parsedPort
    }
  }
  if ($existingValues.ContainsKey("STREAM_HUB_TRUSTED_REMOTE_WRITES")) { $existingTrustedRemoteWrites = $existingValues["STREAM_HUB_TRUSTED_REMOTE_WRITES"] }
  if ($existingValues.ContainsKey("YOUTUBE_CLIENT_ID")) { $existingYoutubeClientId = $existingValues["YOUTUBE_CLIENT_ID"] }
  if ($existingValues.ContainsKey("YOUTUBE_CLIENT_SECRET")) { $existingYoutubeClientSecret = $existingValues["YOUTUBE_CLIENT_SECRET"] }
  if ($existingValues.ContainsKey("YOUTUBE_CREDENTIAL_FILE")) { $existingYoutubeCredentialFile = $existingValues["YOUTUBE_CREDENTIAL_FILE"] }
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
  "STREAM_HUB_TRUSTED_REMOTE_WRITES=$TrustedRemoteWrites",
  "STREAM_MEDIA_DIR=$mediaDir"
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
