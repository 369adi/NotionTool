# Restarts the NotionTool harness instance (Downloads copy) visibly and clears its stale Edge. Never touches other NotionTool installs.
$root=(Split-Path -Parent $PSScriptRoot)
$py='pythonw.exe'
$log="$root\work\restart.log"
$procs=Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*.exe' -and $_.CommandLine -match 'main\.py' }
foreach($p in $procs){
  $g=Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue
  $parent=Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)" -ErrorAction SilentlyContinue
  if($g.MainWindowTitle -eq 'NotionTool' -or ($parent -and $parent.CommandLine -match 'main\.py')){
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    if($parent -and $parent.Name -eq 'powershell.exe' -and $parent.CommandLine -match 'main\.py'){Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue}
  }
}
Start-Sleep 2
# Edge of the stopped instance keeps the harness profile locked ("Browser did not start its DevTools endpoint").
$profiles="$root\data\browser-profiles\"
$stale=@(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profiles) })
foreach($e in $stale){ Stop-Process -Id $e.ProcessId -Force -ErrorAction SilentlyContinue }
if($stale.Count){ Add-Content $log ("["+(Get-Date)+"] stopped "+$stale.Count+" stale harness Edge processes") }
$n=Start-Process -FilePath $py -ArgumentList 'main.py' -WorkingDirectory $root -RedirectStandardOutput "$root\run.log" -RedirectStandardError "$root\run.err.log" -PassThru
Add-Content $log ("["+(Get-Date)+"] restarted pid "+$n.Id)
for($i=0;$i -lt 30;$i++){ Start-Sleep 1; $w=Get-Process -Id $n.Id -ErrorAction SilentlyContinue; if($w -and $w.MainWindowTitle){ (New-Object -ComObject WScript.Shell).AppActivate($n.Id) | Out-Null; break } }
$port='8790 not owned by new pid'
for($i=0;$i -lt 30;$i++){ $c=Get-NetTCPConnection -State Listen -LocalPort 8790 -ErrorAction SilentlyContinue | Select-Object -First 1; if($c -and $c.OwningProcess -eq $n.Id){ $port='8790 ok'; break }; Start-Sleep 1 }
Add-Content $log ("["+(Get-Date)+"] pid "+$n.Id+" window="+$w.MainWindowTitle+" port="+$port)
"pid=$($n.Id) window=$($w.MainWindowTitle) port=$port"
