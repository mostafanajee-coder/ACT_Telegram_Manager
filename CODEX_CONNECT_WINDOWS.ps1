# Preview by default. Use -Apply to make the requested change to LOCAL Codex MCP config.
param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root '.venv\Scripts\python.exe'
$server = Join-Path $root 'act_server.py'
if (-not (Test-Path -LiteralPath $py)) { throw 'Run SETUP_WINDOWS.ps1 first; no .venv Python.' }
if (-not (Test-Path -LiteralPath $server)) { throw 'Cannot find act_server.py.' }
if (-not $Apply) {
    Write-Host 'PREVIEW ONLY. To register the local server, execute:'
    Write-Host ('  codex mcp add act-telegram -- "' + $py + '" "' + $server + '"')
    Write-Host 'Then: codex mcp list'
    Write-Host 'The server runs with YOUR Windows user permissions; MCP restrictions are not an OS sandbox.'
    exit 0
}
$codex = Get-Command codex -ErrorAction SilentlyContinue
if (-not $codex) { throw 'Codex CLI not installed/on PATH. No configuration changed.' }
& $codex.Source mcp add act-telegram -- $py $server
if ($LASTEXITCODE -ne 0) { throw 'Codex MCP registration failed.' }
& $codex.Source mcp list
if ($LASTEXITCODE -ne 0) { throw 'Codex MCP list verification failed.' }
Write-Host 'Registered ACT MCP. Restart Codex to load the tools.'
