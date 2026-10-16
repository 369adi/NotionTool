# Stops the harness EXE (dist\NotionTool.exe, same data folder) and starts the harness from source via restart-notiontool.ps1.
# Falls back to the EXE if the source instance does not own port 8790. Never touches other NotionTool installs (only this exact EXE path).
$root='C:\Users\liket\Downloads\NotionTool-harness\NotionTool'
$log="$root\work\switch-to-source.log"
function L($m){ Add-Content $log ("["+(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')+"] "+$m) }
Start-Sleep 4
$exe="$root\dist\NotionTool.exe"
$procs=@(Get-CimInstance Win32_Process -Filter "Name='NotionTool.exe'" | Where-Object { $_.ExecutablePath -eq $exe })
L ("stopping exe pids "+(($procs | ForEach-Object { $_.ProcessId }) -join ','))
foreach($p in $procs){ Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
for($i=0;$i -lt 20;$i++){ if(-not (Get-NetTCPConnection -State Listen -LocalPort 8790 -ErrorAction SilentlyContinue)){ break }; Start-Sleep 1 }
$out = & powershell -NoProfile -ExecutionPolicy Bypass -File "$root\work\restart-notiontool.ps1" 2>&1 | Out-String
L ("restart: "+$out.Trim())
if($out -notmatch 'port=8790 ok'){
  L 'source instance did not own 8790 - starting the EXE again'
  Start-Process -FilePath $exe -ArgumentList '--data-dir',"`"$root\data`"" -WorkingDirectory "$root\dist"
  Start-Sleep 15
}
$c=Get-NetTCPConnection -State Listen -LocalPort 8790,53768 -ErrorAction SilentlyContinue | ForEach-Object { "$($_.LocalPort)->$($_.OwningProcess)" }
L ("listen: "+($c -join ' '))
