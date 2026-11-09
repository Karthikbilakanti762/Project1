@echo off
REM Windows launcher. Same as `python run.py ...`. Extra words are passed on:
REM   run.bat    run.bat check    run.bat login    run.bat replay 2026-11-06
REM On macOS / Linux use ./run.sh (or python3 run.py) instead.
cd /d "%~dp0"
set PY=python
where python >nul 2>nul || set PY=py
%PY% run.py %*
pause
