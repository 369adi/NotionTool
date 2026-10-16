$ErrorActionPreference = 'Continue'
$exe = "$env:USERPROFILE\.local\bin\codebase-memory-mcp.exe"
$cache = "$env:USERPROFILE\.cache\codebase-memory-mcp"
$log = 'C:\Users\liket\Downloads\NotionTool-harness\NotionTool\work\cbm-reset.log'
function L($m) { "[$(Get-Date -Format s)] $m" | Out-File $log -Append -Encoding utf8 }
L 'start'
$bak = "$cache\_backup-$(Get-Date -Format yyyyMMdd-HHmmss)"
New-Item -ItemType Directory $bak -Force | Out-Null
Get-ChildItem $cache -File -Filter *.db | ? { $_.Name -ne '_config.db' } | % { Copy-Item $_.FullName $bak }
L "backup -> $bak"
foreach ($p in '432Studio-src','AdiCode-src','Aurelune-convert-frequency-live','C-Users-liket-PlazCode-Shared-work-PlazCode-Notion','Refresher-src','SecretScript-cli') {
  $o = cmd /c """$exe"" cli delete_project --project $p 2>&1"
  L "delete $p : $(($o | Out-String).Trim() -replace '\s+',' ')"
}
L 'index NotionTool'
$o = cmd /c """$exe"" cli index_repository --repo-path C:/Users/liket/Downloads/NotionTool-harness/NotionTool --name NotionTool --mode moderate 2>&1"
L "index: $(($o | Select -Last 15 | Out-String).Trim())"
$o = cmd /c """$exe"" cli list_projects 2>nul"
L "list: $(($o | Out-String).Trim())"
L 'DONE'
