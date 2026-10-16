$p=@("$env:APPDATA\NotionTool","$env:USERPROFILE\Refresher")
foreach($x in $p){ cmd /c rd /s /q "$x" }
$p | % { "$_ " + (Test-Path $_) } | Set-Content "$env:TEMP\oldnt-remove.txt"
