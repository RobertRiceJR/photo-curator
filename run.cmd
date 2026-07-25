@echo off
REM photo-curator wrapper. Resolves a real Python 3.12+ interpreter, because bare
REM `python` on this machine is Anaconda 3.10. Same reason the research repo has one.
setlocal

REM `if not defined` inside the loop is load-bearing: without it every matching line
REM overwrote PC_PY, so the LAST entry won and 3.12 beat 3.13 -- the reverse of the
REM listed preference. Dormant while only one is installed, which is why it survived.
set "PC_PY="
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
) do (
  if not defined PC_PY if exist %%P set "PC_PY=%%~P"
)

if not defined PC_PY (
  py -3.13 -c "import sys" >nul 2>&1 && set "PC_PY=py -3.13"
)
if not defined PC_PY (
  py -3.12 -c "import sys" >nul 2>&1 && set "PC_PY=py -3.12"
)
if not defined PC_PY (
  echo [error] no Python 3.12+ found. Install one, or edit run.cmd.
  exit /b 1
)

%PC_PY% "%~dp0src\cli.py" %*

REM Propagate the exit code. A bare `endlocal` is itself a successful command, so it resets
REM ERRORLEVEL to 0 and every code the CLI returns -- 2 for a contract violation, 1 for a
REM missing index -- silently became "success" to any caller. The `&` idiom expands %RC% at
REM parse time, before endlocal discards the variable.
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
