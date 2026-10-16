# The one restart path for the harness (I10): optional rebuild, stop ALL old instances, start exactly one,
# verify that the new instance owns port 8790 and is the only one. Run it detached; it outlives the app.
param([ValidateSet('exe', 'source')][string]$Mode = 'exe', [switch]$Rebuild)
$root = 'C:\Users\liket\Downloads\NotionTool-harness\NotionTool'
$py = 'C:\Users\liket\AppData\Local\Programs\Python\Python312\python.exe'
$pyw = 'C:\Users\liket\AppData\Local\Programs\Python\Python312\pythonw.exe'
$log = "$root\work\restart.log"
function L($m) { Add-Content $log ("[" + (Get-Date) + "] restart-harness: " + $m) }
Set-Location $root
L "begin mode=$Mode rebuild=$Rebuild"
if ($Rebuild -and $Mode -eq 'exe') {
    Remove-Item "$root\work\newdist\NotionTool.exe" -Force -ErrorAction SilentlyContinue
    & $py -m PyInstaller --noconfirm --distpath "$root\work\newdist" --workpath "$root\work\pyibuild" NotionTool.spec *> "$root\work\pyinstaller.log"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path "$root\work\newdist\NotionTool.exe")) { L "build FAILED exit=$LASTEXITCODE (old app keeps running)"; exit 1 }
    L ('build ok ' + (Get-Item "$root\work\newdist\NotionTool.exe").Length + ' bytes')
}
& "$root\work\stop-harness.ps1"
if ($LASTEXITCODE -ne 0) { L 'abort: an old instance is still alive or a harness port is busy'; exit 1 }
if ($Rebuild -and $Mode -eq 'exe') {
    Copy-Item "$root\dist\NotionTool.exe" "$root\work\NotionTool.exe.bak" -Force
    $copied = $false
    for ($i = 0; $i -lt 10; $i++) { try { Copy-Item "$root\work\newdist\NotionTool.exe" "$root\dist\NotionTool.exe" -Force -ErrorAction Stop; $copied = $true; break } catch { Start-Sleep 1 } }
    if (-not $copied) { L 'copy of the new EXE failed; starting the previous EXE' } else { L 'new EXE copied to dist' }
}
if ($Mode -eq 'exe') {
    $n = Start-Process -FilePath "$root\dist\NotionTool.exe" -ArgumentList '--data-dir', "`"$root\data`"" -WorkingDirectory "$root\dist" -PassThru
} else {
    $n = Start-Process -FilePath $pyw -ArgumentList "`"$root\main.py`"" -WorkingDirectory $root -RedirectStandardOutput "$root\run.log" -RedirectStandardError "$root\run.err.log" -PassThru
}
L "started pid $($n.Id)"
# PyInstaller onefile: the started PID is the launcher; the app may listen from its child.
$state = '8790 not listening'; $owner = 0
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep 1
    $c = Get-NetTCPConnection -State Listen -LocalPort 8790 -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $c) { continue }
    $op = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)" -ErrorAction SilentlyContinue
    if ($c.OwningProcess -eq $n.Id -or ($op -and $op.ParentProcessId -eq $n.Id)) { $owner = $c.OwningProcess; $state = '8790 ok'; break }
    $state = "8790 owned by other pid $($c.OwningProcess)"
}
$procs = @(& "$root\work\stop-harness.ps1" -List)
$ids = @($procs | ForEach-Object { [int]($_.Split(':')[1]) })
$roots = @($procs | Where-Object { $ids -notcontains [int]($_.Split(':')[2]) })
L "pid $($n.Id) owner=$owner $state instances=$($roots.Count) [$($procs -join ',')]"
"pid=$($n.Id) owner=$owner state=$state instances=$($roots.Count)"
if ($state -ne '8790 ok' -or $roots.Count -ne 1) { exit 1 }
exit 0
