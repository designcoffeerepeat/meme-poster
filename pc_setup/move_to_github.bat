@echo off
rem One-time: moves the daily meme posting from this PC to GitHub (runs with the PC off).
cd /d "%~dp0"
where py >nul 2>nul && (py -3 "%~dp0move_to_github.py") || (python "%~dp0move_to_github.py")
