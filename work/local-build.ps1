# Local EXE build without push. Log: work\build.log; one line per build in work\build-history.log.
param([string]$Python = 'python.exe')
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$p = Start-Process cmd.exe -ArgumentList "/c `"$Python`" -m PyInstaller --noconfirm NotionTool.spec > work\build.log 2>&1" -WorkingDirectory $root -WindowStyle Hidden -Wait -PassThru
if ($p.ExitCode -ne 0) { Write-Output ('build failed (exit ' + $p.ExitCode + '); see work\build.log'); exit $p.ExitCode }
$exe = Get-Item (Join-Path $root 'dist\NotionTool.exe')
$line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] dist\NotionTool.exe ' + $exe.Length + ' bytes sha256 ' + (Get-FileHash $exe.FullName -Algorithm SHA256).Hash
Add-Content -Path (Join-Path $root 'work\build-history.log') -Value $line
Write-Output $line
