#Requires -Version 5.1
<#
.SYNOPSIS
  Install or upgrade the Baton Agent Orchestrator CLI (`jev`).
.DESCRIPTION
  One-command install (Windows PowerShell):
    irm https://raw.githubusercontent.com/Gane2506/baton/main/install.ps1 | iex

  Options via environment variables (a piped script cannot take arguments):
    $env:JEV_REF = "v0.3.0"        # this version (PyPI) or git ref (GitHub)
    $env:JEV_NO_DASHBOARD = "1"    # skip the web-dashboard extra

  Safe to re-run: installs when missing, upgrades when present. Installs from
  PyPI when the package is published there, otherwise from GitHub. Installs for
  the current user only, never system-wide. Verifies with `jev --version` and
  `jev doctor` at the end.
#>
[CmdletBinding()]
param(
  [string]$Ref = $env:JEV_REF,
  [bool]$NoDashboard = ($env:JEV_NO_DASHBOARD -eq '1')
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Repo = 'Gane2506/baton'
$Pkg = 'baton-agent-orchestrator'
$PyPIUrl = "https://pypi.org/pypi/$Pkg/json"

# Returns @{ Exe = 'py'; Args = @('-3.11') } for the first Python 3.11+ found,
# or $null. Kept as Exe + Args so invocation is `& $Py.Exe @($Py.Args) ...`,
# which is valid in Windows PowerShell 5.1 (bare `& @array` is not).
function Find-Python {
  $candidates = @(@('py', '-3.11'), @('py', '-3'), @('python', $null))
  foreach ($c in $candidates) {
    $invokeArgs = @()
    if ($c[1]) { $invokeArgs = @($c[1]) }
    try {
      $out = & $c[0] @invokeArgs --version 2>&1
      if (($LASTEXITCODE -eq 0) -and ($out -match 'Python (\d+)\.(\d+)')) {
        if ((([int]$Matches[1] -eq 3) -and ([int]$Matches[2] -ge 11)) -or ([int]$Matches[1] -ge 4)) {
          return @{ Exe = $c[0]; Args = $invokeArgs }
        }
      }
    } catch { }
  }
  return $null
}

$Py = Find-Python
if (-not $Py) {
  Write-Host 'error: no Python 3.11+ found. Install Python 3.11 or newer first: https://www.python.org/downloads/'
  exit 1
}
$pyVer = ((& $Py.Exe @($Py.Args) --version 2>&1) -replace 'Python ', '')
Write-Host "Python $pyVer"

$Installer = $null
if (Get-Command pipx -ErrorAction SilentlyContinue) { $Installer = 'pipx' }
else {
  try { & $Py.Exe @($Py.Args) -m pip --version | Out-Null; $Installer = 'pip' } catch { }
}
if (-not $Installer) {
  Write-Host 'error: neither pipx nor pip found. Install pipx (https://pipx.pypa.io/) or make sure pip is available for Python.'
  exit 1
}
Write-Host "Installer: $Installer"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  Write-Warning 'git not found on PATH; jev needs it to work with repos.'
}

# Source: PyPI when published, else GitHub.
$extra = ''
if (-not $NoDashboard) { $extra = '[dashboard]' }
$onPyPI = $false
try { Invoke-RestMethod -Uri $PyPIUrl -TimeoutSec 15 | Out-Null; $onPyPI = $true } catch { }
if ($onPyPI) {
  $spec = "$Pkg$extra"
  if ($Ref) { $spec = "$spec==$Ref" }
  $source = 'PyPI'
} else {
  $gref = if ($Ref) { $Ref } else { 'main' }
  $spec = "$Pkg$extra @ git+https://github.com/$Repo@$gref"
  $source = "GitHub ($Repo@$gref)"
}
Write-Host "Source: $source"

# Install or upgrade (idempotent).
if ($Installer -eq 'pipx') {
  $pipxList = pipx list --short 2>$null
  $known = $pipxList -match "^$Pkg( |$)"
  if ($known) { Write-Host 'Upgrading existing pipx install...'; pipx upgrade $Pkg }
  else { pipx install $spec }
  pipx ensurepath 2>$null | Out-Null
} else {
  $pipArgs = @($Py.Args) + @('-m', 'pip', 'install', '--user', '--upgrade', $spec)
  & $Py.Exe @pipArgs
}

# Verify. `jev` may not be on PATH yet in this shell (pipx ensurepath only
# affects future shells). The install still counts as a success when the
# binary is verifiable by absolute path — print the PATH line instead of failing.
$jevExe = $null
$jevCmd = Get-Command jev -ErrorAction SilentlyContinue
if ($jevCmd) { $jevExe = $jevCmd.Source }
if (-not $jevExe) {
  $userScripts = (& $Py.Exe @($Py.Args) -c 'import site, os; print(os.path.join(site.getuserbase(), "Scripts"))')
  $pipxBin = Join-Path $env:USERPROFILE '.local\bin'
  foreach ($dir in @($userScripts, $pipxBin)) {
    $candidate = Join-Path $dir 'jev.exe'
    if (Test-Path $candidate) { $jevExe = $candidate; break }
  }
  if (-not $jevExe) {
    Write-Host "error: installed, but no jev.exe found in $userScripts or $pipxBin."
    exit 1
  }
  $jevDir = Split-Path $jevExe -Parent
  Write-Host 'Note: installed, but `jev` is not on PATH yet in this shell.'
  Write-Host 'Add this directory to your PATH (Settings > System > About > Advanced system settings > Environment Variables) and restart PowerShell:'
  Write-Host "  $jevDir"
}
Write-Host "OK: $(& $jevExe --version)"

# `jev doctor` checks the environment (workers, API keys, config). On a fresh
# machine some checks fail until you configure a worker — that is expected and
# does not fail the install.
if (Get-Command git -ErrorAction SilentlyContinue) {
  $doctorDir = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName())
  New-Item -ItemType Directory -Path $doctorDir | Out-Null
  try {
    Push-Location $doctorDir
    try {
      git init -q | Out-Null
      & $jevExe doctor
      if ($LASTEXITCODE -eq 0) { Write-Host 'OK: jev doctor passes.' }
      else {
        Write-Host 'Note: jev doctor reports setup still needed (worker CLIs, API keys).'
        Write-Host 'That is expected on a fresh machine — see `jev doctor` output above.'
      }
    } finally { Pop-Location }
  } finally { Remove-Item -Recurse -Force $doctorDir }
} else {
  Write-Host 'Note: skipping `jev doctor` (git not installed).'
}

Write-Host 'Done. Next: cd into a git repo, run `jev init`, then `jev --help`.'

# --- autostart ---------------------------------------------------------------
# Offer the reboot-survival step (`jev dashboard --install-service`) when this
# directory is a git repo — with the user's yes, never silently (deploy Phase 3,
# issue #341). A piped install cannot prompt, so without an interactive console
# print the command to run later instead. A service install failure never fails
# the install.
$repoDir = $null
if (Get-Command git -ErrorAction SilentlyContinue) {
  $repoDir = (& git rev-parse --show-toplevel 2>$null)
  if ($LASTEXITCODE -ne 0) { $repoDir = $null }
}
if ($repoDir -and $repoDir.Replace('/', '\') -ne $env:USERPROFILE) {
  if (-not [Console]::IsInputRedirected) {
    $answer = Read-Host "Install the dashboard as a service that starts at logon for $repoDir? [y/N]"
    if ($answer -match '^[yY]([eE][sS])?$') {
      try {
        & $jevExe dashboard --install-service --repo $repoDir
        if ($LASTEXITCODE -eq 0) { Write-Host 'OK: dashboard service installed.' }
        else { Write-Host "Note: service install failed — run 'jev dashboard --install-service --repo $repoDir' by hand." }
      } catch {
        Write-Host "Note: service install failed — run 'jev dashboard --install-service --repo $repoDir' by hand."
      }
    }
  } else {
    Write-Host "Next: to start the dashboard at logon, run 'jev dashboard --install-service --repo $repoDir'."
  }
} else {
  Write-Host "Next: to start the dashboard at logon, run 'jev dashboard --install-service --repo <your-repo>' after 'jev init'."
}
