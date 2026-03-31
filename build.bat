@echo off
setlocal

REM Assure un venv ou Python 3 installé
if exist ".venv\Scripts\python.exe" (
    set PYTHON=.venv\Scripts\python.exe
) else (
    set PYTHON=python
)

%PYTHON% -m pip install --upgrade pip
%PYTHON% -m pip install pyinstaller

pyinstaller --noconfirm --onefile --name GalaxieBot bot.py

endlocal
