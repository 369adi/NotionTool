# Rebuilds dist\NotionTool.exe and restarts the harness via the single restart path
# work\restart-harness.ps1 (stops ALL harness instances first, verifies the new owner of 8790).
$root='C:\Users\liket\Downloads\NotionTool-harness\NotionTool'
Add-Content "$root\work\rebuild.log" ("["+(Get-Date)+"] delegating to restart-harness.ps1 -Mode exe -Rebuild")
& "$root\work\restart-harness.ps1" -Mode exe -Rebuild
Add-Content "$root\work\rebuild.log" ("["+(Get-Date)+"] restart-harness exit=$LASTEXITCODE")
exit $LASTEXITCODE
