# Kept for old references: delegates to the single restart path work\restart-harness.ps1,
# which stops ALL harness instances (EXE and source) before starting exactly one.
param([ValidateSet('exe', 'source')][string]$Mode = 'exe', [switch]$Rebuild)
& 'C:\Users\liket\Downloads\NotionTool-harness\NotionTool\work\restart-harness.ps1' -Mode $Mode -Rebuild:$Rebuild
exit $LASTEXITCODE
