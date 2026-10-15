# EXE smoke test on the real data folder with automatic return to the pythonw harness.
# Run detached. Log: work\exe-smoke.log. Create work\exe-smoke.stop to end the live window early.
# Stops only dist\NotionTool.exe, its descendants and the harness Edge profile; other NotionTool.exe installs stay untouched.
param([int]$LiveSeconds = 150)
$ErrorActionPreference = 'Continue'
$root = (Split-Path -Parent $PSScriptRoot)
$exe = "$root\dist\NotionTool.exe"
$log = "$root\work\exe-smoke.log"
function L([string]$m) { Add-Content -Path $log -Value ('[' + (Get-Date -Format 'HH:mm:ss') + '] ' + $m) }
function Owner([int]$port) { $c = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -First 1; if ($c) { [int]$c.OwningProcess } else { 0 } }
function StopHarnessEdge { $pr = "$root\data\browser-profiles\"; @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($pr) }) | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }
function WebViewCount { @(Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" | Where-Object { $_.CommandLine -like '*--webview-exe-name=NotionTool.exe*' }).Count }
function ExeTree {
  $all = @(Get-CimInstance Win32_Process)
  $tree = @{}
  foreach ($p in $all) { if ($p.ExecutablePath -eq $exe) { $tree[[int]$p.ProcessId] = $p } }
  do {
    $added = $false
    foreach ($p in $all) { $id = [int]$p.ProcessId; $parent = [int]$p.ParentProcessId; if (-not $tree.ContainsKey($id) -and $tree.ContainsKey($parent) -and $p.CreationDate -ge $tree[$parent].CreationDate) { $tree[$id] = $p; $added = $true } }
  } while ($added)
  @($tree.Values)
}
function StopExe { $t = ExeTree; foreach ($p in $t) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }; L ('stopped exe tree: ' + (($t | ForEach-Object { $_.Name + ':' + $_.ProcessId }) -join ', ')) }
Remove-Item "$root\work\exe-smoke.stop" -ErrorAction SilentlyContinue
Start-Sleep 4
L ('start; exe ' + (Get-Item $exe).Length + ' bytes sha256 ' + (Get-FileHash $exe -Algorithm SHA256).Hash + '; NotionTool webviews before ' + (WebViewCount))
$old = Owner 8790
if ($old) { Stop-Process -Id $old -Force -ErrorAction SilentlyContinue; L "stopped harness pid $old" }
Start-Sleep 2
StopHarnessEdge
$p = Start-Process -FilePath $exe -ArgumentList '--data-dir', "$root\data" -WorkingDirectory $root -PassThru
L ('exe started pid ' + $p.Id)
$ok = $false
for ($i = 0; $i -lt 60; $i++) { Start-Sleep 1; $o = Owner 8790; if ($o) { $op = Get-CimInstance Win32_Process -Filter "ProcessId=$o"; if ($op.ExecutablePath -eq $exe) { $ok = $true; break } } }
L ('8790 owner ' + (Owner 8790) + ' is_exe=' + $ok + ' after ' + $i + 's; 53768 owner ' + (Owner 53768))
if ($ok) {
  try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8790/' -TimeoutSec 10; L ('GET / ' + $r.StatusCode + ' bytes=' + $r.RawContentLength) } catch { L ('GET / failed: ' + $_.Exception.Message) }
  Start-Sleep 5
  $t = ExeTree
  $ids = @($t | Where-Object { $_.ExecutablePath -eq $exe } | ForEach-Object { [int]$_.ProcessId })
  $win = (@(Get-Process -Id $ids -ErrorAction SilentlyContinue) | Where-Object { $_.MainWindowTitle } | Select-Object -First 1).MainWindowTitle
  $edge = @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" | Where-Object { $_.CommandLine -and $_.CommandLine.Contains("$root\data\browser-profiles\") -and $_.CommandLine -notlike '*--type=*' })
  $wv = @($t | Where-Object { $_.Name -eq 'msedgewebview2.exe' }).Count
  L ('exe processes ' + ($ids -join ',') + " window='" + $win + "' harness edge main processes " + $edge.Count + '; webview processes in exe tree ' + $wv)
  L ('live window ' + $LiveSeconds + 's')
  $deadline = (Get-Date).AddSeconds($LiveSeconds)
  while ((Get-Date) -lt $deadline -and -not (Test-Path "$root\work\exe-smoke.stop")) { Start-Sleep 2 }
}
StopExe
Start-Sleep 3
StopHarnessEdge
L ('exe stopped; remaining exe processes ' + @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $exe }).Count + '; NotionTool webviews now ' + (WebViewCount) + '; restarting harness')
# Run the restart script without a pipe: the new pythonw harness inherited the pipe handle and kept this script waiting until the harness exited.
$before = @(Get-Content "$root\work\restart.log" -ErrorAction SilentlyContinue).Count
$rs = Start-Process powershell -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "$root\work\restart-notiontool.ps1" -WindowStyle Hidden -PassThru
if (-not $rs.WaitForExit(120000)) { L 'restart script still running after 120 s' }
@(Get-Content "$root\work\restart.log" -ErrorAction SilentlyContinue) | Select-Object -Skip $before | ForEach-Object { L ('restart: ' + $_) }
L ('done; 8790 owner ' + (Owner 8790) + '; 53768 owner ' + (Owner 53768))
