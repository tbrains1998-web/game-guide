@echo off
rem Game Guide - hold F10 in game to ask by voice, F11 for a screenshot.
rem Uses the .venv in this folder when there is one (see README), else the Python launcher (pyw),
rem else pythonw.exe from PATH.
if exist "%~dp0.venv\Scripts\pythonw.exe" (start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0guide.py" & exit /b 0)
where pyw >nul 2>nul && (start "" pyw -3 "%~dp0guide.py" & exit /b 0)
where pythonw >nul 2>nul && (start "" pythonw "%~dp0guide.py" & exit /b 0)
echo Python 3 was not found. Install Python 3.12 from python.org (tick "Add python.exe to PATH"),
echo then run these two lines in this folder (see README):
echo   py -3.12 -m venv .venv
echo   .venv\Scripts\python -m pip install -r requirements.txt
pause
exit /b 1
