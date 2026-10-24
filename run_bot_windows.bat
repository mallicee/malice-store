@echo off
setlocal
cd /d "%~dp0"
py -3.13 bot\malice_bot.py
if errorlevel 1 (
  echo.
  echo MALICE bot stopped with an error. Review the message above.
)
pause
