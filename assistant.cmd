@echo off
REM Lance l'assistant avec le Python du venv, sans avoir a activer l'environnement.
REM Usage depuis la racine du projet :  assistant auth  /  assistant serve  /  assistant sync
REM Fonctionne dans cmd.exe comme dans PowerShell (.\assistant ...).

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Environnement Python absent. A faire une fois :
  echo    python -m venv .venv
  echo    .venv\Scripts\python.exe -m pip install -e .
  exit /b 1
)

"%~dp0.venv\Scripts\python.exe" -m assistant.cli %*
