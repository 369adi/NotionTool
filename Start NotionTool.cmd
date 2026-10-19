@echo off
cd /d "%~dp0"
if exist "%~dp0NotionTool.exe" (
  start "" "%~dp0NotionTool.exe" --data-dir "%~dp0data"
) else if exist "%~dp0dist\NotionTool.exe" (
  start "" "%~dp0dist\NotionTool.exe" --data-dir "%~dp0data"
) else (
  start "" pythonw.exe main.py
)
