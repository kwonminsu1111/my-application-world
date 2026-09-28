@echo off
cd /d "%~dp0_viewer"
python serve.py
if errorlevel 1 pause
