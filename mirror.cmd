@echo off
rem Mirror and control the projector with scrcpy (no audio).   Usage: mirror.cmd [projector-ip]
rem The shim sends scrcpy's helper to /sdcard, because this firmware wipes /data/local/tmp.
rem adb and scrcpy are taken from PATH, or from platform-tools and scrcpy-win64-* folders
rem next to this one; set REAL_ADB / SCRCPY to point elsewhere.
setlocal
set IP=%~1
if "%IP%"=="" set IP=192.168.1.20
if not defined REAL_ADB if exist "%~dp0..\platform-tools\adb.exe" set "REAL_ADB=%~dp0..\platform-tools\adb.exe"
if not defined REAL_ADB set REAL_ADB=adb
if not defined SCRCPY for /d %%d in ("%~dp0..\scrcpy-win64-*") do set "SCRCPY=%%d\scrcpy.exe"
if not defined SCRCPY set SCRCPY=scrcpy
set "ADB=%~dp0adb-sdcard-shim\adb.exe"
"%REAL_ADB%" connect %IP%:5555
start "" "%SCRCPY%" --serial=%IP%:5555 --no-audio --window-title=Projector --max-size=1280
