@echo off
setlocal
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 "%~dp0bootstrap.py" %*
goto done
:use_python
python "%~dp0bootstrap.py" %*
:done
set "LAUNCH_STATUS=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %LAUNCH_STATUS%
