# Stops EVERY NotionTool harness instance of this root and its Edge processes, then waits until the
# harness ports are free. Covers dist\NotionTool.exe (launcher + child) and python/pythonw main.py.
# Never touches other NotionTool installs (Mine-for-Eggs on 8873, the old desktop tool, MailRelay).
# -List only prints the running harness processes as Name:PID:ParentPID.
param([int]$TimeoutSec = 30, [switch]$List)
$root = 'C:\Users\liket\Downloads\NotionTool-harness\NotionTool'
$log = "$root\work\restart.log"
$ports = 8790, 53768
$exe = "$root\dist\NotionTool.exe".ToLower()
$profiles = "$root\data\browser-profiles\"
function L($m) { Add-Content $log ("[" + (Get-Date) + "] stop-harness: " + $m) }
function Harness {
    $owners = @(Get-NetTCPConnection -State Listen -LocalPort $ports -ErrorAction SilentlyContinue | ForEach-Object { $_.OwningProcess })
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and -not ($_.CommandLine -and $_.CommandLine.Contains('--gateway')) -and (
            ($_.Name -ieq 'NotionTool.exe' -and $_.ExecutablePath -and $_.ExecutablePath.ToLower() -eq $exe) -or
            ($_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match 'main\.py' -and (
                $_.CommandLine.ToLower().Contains($root.ToLower()) -or ($owners -contains $_.ProcessId)))
        )
    })
}
function Edges { @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profiles) }) }
if ($List) { Harness | ForEach-Object { "$($_.Name):$($_.ProcessId):$($_.ParentProcessId)" }; exit 0 }
$first = Harness
foreach ($p in $first) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
$edges = Edges
foreach ($e in $edges) { Stop-Process -Id $e.ProcessId -Force -ErrorAction SilentlyContinue }
L ("stopping [" + (($first | ForEach-Object { "$($_.Name):$($_.ProcessId)" }) -join ',') + "] and " + $edges.Count + " Edge processes")
$deadline = (Get-Date).AddSeconds($TimeoutSec)
do {
    Start-Sleep -Milliseconds 500
    $left = Harness
    foreach ($p in $left) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    foreach ($e in (Edges)) { Stop-Process -Id $e.ProcessId -Force -ErrorAction SilentlyContinue }
    $busy = @(Get-NetTCPConnection -State Listen -LocalPort $ports -ErrorAction SilentlyContinue)
} while (($left.Count -or $busy.Count) -and (Get-Date) -lt $deadline)
if ($left.Count -or $busy.Count) {
    L ("NOT clean, left=" + (($left | ForEach-Object { $_.ProcessId }) -join ',') + " ports=" + (($busy | ForEach-Object { "$($_.LocalPort):$($_.OwningProcess)" }) -join ','))
    exit 1
}
L 'clean: no harness process left, ports 8790/53768 free'
exit 0
