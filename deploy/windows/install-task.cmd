@echo off
REM Registers the tracker to start at logon and keep running.
REM Removes itself with:  schtasks /delete /tn "EbayDealTracker" /f

set "LAUNCHER=%~dp0run-tracker.cmd"

schtasks /create ^
  /tn "EbayDealTracker" ^
  /tr "\"%LAUNCHER%\"" ^
  /sc onlogon ^
  /rl limited ^
  /f

echo.
echo Registered. Start it now with:
echo   schtasks /run /tn "EbayDealTracker"
echo.
echo Check on it with:
echo   schtasks /query /tn "EbayDealTracker"
echo.
echo Remove it with:
echo   schtasks /delete /tn "EbayDealTracker" /f
