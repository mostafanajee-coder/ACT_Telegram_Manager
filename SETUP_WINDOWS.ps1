# ACT Telegram Manager - user-initiated Windows install. Never authenticates to Telegram.
# Run in PowerShell:  powershell -NoProfile -ExecutionPolicy Bypass -File .\SETUP_WINDOWS.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    throw 'uv was not found. Install uv from https://docs.astral.sh/uv/getting-started/installation/ and retry.'
}
Write-Host 'Installing locked dependencies for CPython 3.11 locally (network may be used ONLY for packages).'
& $uv.Source sync --frozen --python 3.11 --no-dev
if ($LASTEXITCODE -ne 0) { throw 'uv sync failed. No Telegram authentication was attempted.' }
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) { throw 'Expected virtual environment Python was not created.' }
Write-Host 'Checking local readiness:'
& $py (Join-Path $root 'act_local.py') doctor
if ($LASTEXITCODE -ne 0) { throw 'Local doctor failed.' }
Write-Host 'Running disposable OFFLINE demonstration:'
& $py (Join-Path $root 'act_local.py') demo
if ($LASTEXITCODE -ne 0) { throw 'Offline demonstration failed.' }
if (-not (Test-Path -LiteralPath (Join-Path $root 'act_config.json'))) {
    Write-Host 'Creating deny-all policy configuration (no Telegram access).'
    & $py (Join-Path $root 'act_local.py') init
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create config.' }
}
Write-Host 'Setup complete: NO login, NO real messages, NO Codex configuration changes.'
Write-Host 'Next: read ACT_LOCAL_WINDOWS_AR.md. Credentials MUST remain on this computer.'
