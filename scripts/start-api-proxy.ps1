# Starts the OpenAI-compatible NotionTool API proxy (http://127.0.0.1:8793/v1).
# Every chat request runs as one task in a NotionTool Welcome-chat Coworker.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\start-api-proxy.ps1 [-Port 8793] [-ProxyKey secret] [-Python path\to\python.exe]
# The NotionTool host must be running. Python: -Python, else $env:NT_PYTHON, else 'python' from PATH.
param(
    [int]$Port = 8793,
    [string]$ProxyKey = $env:NT_PROXY_KEY,
    [int]$Timeout = 600,
    [string]$Python = $(if ($env:NT_PYTHON) { $env:NT_PYTHON } else { 'python' })
)
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$cli = @('-m', 'notiontool.api_proxy', '--port', "$Port", '--timeout', "$Timeout")
if ($ProxyKey) { $cli += @('--proxy-key', $ProxyKey) }
& $Python @cli
exit $LASTEXITCODE
