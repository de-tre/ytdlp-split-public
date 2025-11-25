@echo off
setlocal

rem Change into dir of this batch file
pushd "%~dp0"

echo ============================================
echo   ytdlp-split - URL Collector Starter
echo ============================================
echo.

rem 1) Determine python interpreter
set "PY_EXE="

rem a) If a .venv already exists, use its python
if exist ".venv\Scripts\python.exe" (
    set "PY_EXE=%CD%\.venv\Scripts\python.exe"
) else (
    rem b) Try conda env "ytdlp-split" first
    if exist "C:\Users\user_name\anaconda3\envs\ytdlp-split\python.exe" (
        echo [INFO] Verwende Conda-Python aus ytdlp-split-Env.
        set "PY_EXE=C:\Users\user_name\anaconda3\envs\ytdlp-split\python.exe"
    ) else (
        rem c) Fallback: system-wide "python" from PATH
        where python >nul 2>&1
        if errorlevel 1 (
            echo [ERROR] Python 3.10+ wasn't found.
            echo Please install python und make sure that 'python' is in the PATH.
            echo Download: https://www.python.org/downloads/windows/
            echo.
            pause
            popd
            exit /b 1
        )
        set "PY_EXE=python"
    )
)

echo [INFO] Use python: %PY_EXE%
echo.

rem 2) Create virtual env, in case it doesn't exist yet
if not exist ".venv\Scripts\python.exe" (
    echo [SETUP] Create virtual environment .venv ...
    "%PY_EXE%" -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Couldn't create virtual env.
        echo.
        pause
        popd
        exit /b 1
    )

    echo [SETUP] Activate .venv and install dependencies ...
    call ".venv\Scripts\activate.bat"
    if errorlevel 1 (
        echo [ERROR] Couldn't activate .venv.
        echo.
        pause
        popd
        exit /b 1
    )

    python -m pip install --upgrade pip
    if errorlevel 1 (
        echo [WARN] Couldn't update pip. Continue regardless ...
    )

    rem Install the project in editable mode (read pyproject.toml)
    pip install -e .
    if errorlevel 1 (
        echo [ERROR] Couldn't install project dependencies.
        echo.
        pause
        popd
        exit /b 1
    )
) else (
    rem 3) Activate existing env
    echo [INFO] Activate existing .venv ...
    call ".venv\Scripts\activate.bat"
    if errorlevel 1 (
        echo [ERROR] Couldn't activate .venv.
        echo.
        pause
        popd
        exit /b 1
    )
)

echo.
echo [INFO] Start URL collector ...
echo.

rem 4) Start URL collector (from the .venv)
python ytdlp_url_collector.py

echo.
echo [INFO] Shut down collector.
echo.
popd
pause