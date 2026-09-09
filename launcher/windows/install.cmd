@echo off
setlocal
set "DLE_LAUNCHER=%~dp0dolilocaledit-launcher.exe"
if not exist "%DLE_LAUNCHER%" (
  echo Doli Local Edit: dolilocaledit-launcher.exe est introuvable.
  pause
  exit /b 2
)
"%DLE_LAUNCHER%" install --source "%DLE_LAUNCHER%"
if errorlevel 1 (
  echo Doli Local Edit: l'installation a echoue.
  pause
  exit /b 1
)
exit /b 0
