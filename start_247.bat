@echo off
REM ==========================================================================
REM  EzyGamers GameStreamer - 24/7 launcher
REM
REM  Plays SumLink "Level 100" marathons forever, streams to YouTube and saves
REM  overlay + clean recordings. If the program ever exits (crash, update,
REM  anything), it is restarted after 30 seconds.
REM
REM  Usage:   start_247.bat            YouTube + recordings
REM           start_247.bat record     recordings only (no YouTube)
REM  Stop:    close this window, or press Ctrl+C twice.
REM ==========================================================================
cd /d "%~dp0"

set FLAGS=--mode marathon --headless --record --live
if /i "%1"=="record" set FLAGS=--mode marathon --headless --record

:loop
echo [%date% %time%] Starting streamer: %FLAGS%
".venv\Scripts\python.exe" run_bot.py %FLAGS%
echo [%date% %time%] Streamer exited with code %errorlevel% - restarting in 30 seconds (close window to stop)
echo [%date% %time%] Streamer exited with code %errorlevel% >> logs\launcher.log
timeout /t 30 /nobreak >nul
goto loop
