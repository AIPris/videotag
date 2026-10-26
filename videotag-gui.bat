@echo off
rem Opens the videotag dashboard in your browser (http://127.0.0.1:8765/).
rem Close this window or press Ctrl+C to stop it. Runs started from the dashboard keep going without it.
rem Extra options are passed on, for example: videotag-gui.bat --port 9000 --no-browser
setlocal
set "PY=python"
where python >nul 2>nul || set "PY=py -3"
%PY% -I -m videotag gui %*
if errorlevel 1 (
  echo.
  echo videotag gui stopped with an error. Is videotag installed?  pip install --no-user -e ".[describe]"
  pause
)
