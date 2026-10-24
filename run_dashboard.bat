@echo off
rem Opens the cabinet clustering dashboard in your browser.
cd /d "%~dp0"
rem Uses Anaconda's Python if it is in the usual place, otherwise the python on PATH.
set "PY=%USERPROFILE%\anaconda3\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m streamlit run dashboard\app.py
pause
