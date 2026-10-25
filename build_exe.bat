@echo off
REM Compila Ultron.exe (lanzador liviano, ver launch_vision.py) con PyInstaller.
REM
REM IMPORTANTE -- por que este .bat usa un venv de build SEPARADO:
REM backend\.venv (el que usa Vision para correr) pesa mas de 1 GB porque
REM tiene pandas/numpy/scipy/sklearn/oracledb/matplotlib/etc -- TODAS las
REM dependencias reales de la app, mas pytest/ruff de desarrollo. Si
REM PyInstaller analizara ese venv, Ultron.exe terminaria pesando cientos
REM de MB (igual o peor que el Vision.zip actual de ~377 MB), porque
REM `launch_vision.py` importa por texto (subprocess), pero un analisis
REM estatico agresivo o un import accidental de algo pesado igual
REM arrastraria peso.
REM
REM `launch_vision.py` NO necesita nada de eso: es puro stdlib (hashlib,
REM subprocess, threading, urllib, webbrowser). Por eso este script arma
REM un venv de build DESCARTABLE (backend\.venv-build) con SOLO pyinstaller
REM adentro, compila desde ahi, y el resultado es un Ultron.exe de pocos MB.
REM El .venv-build no se commitea (ver .gitignore) y se puede borrar en
REM cualquier momento -- se re-crea solo la proxima vez que compiles.
REM
REM Uso: doble click, o `build_exe.bat` desde una consola en la raiz de
REM vision-web (al lado de backend/ y frontend/).

setlocal

cd /d "%~dp0"

if not exist "launch_vision.py" (
    echo ERROR: no se encontro launch_vision.py en %cd%
    echo Corre este .bat desde la raiz de vision-web.
    exit /b 1
)

set "BUILD_VENV=backend\.venv-build"

if not exist "%BUILD_VENV%\Scripts\python.exe" (
    echo Creando entorno de build aislado en %BUILD_VENV% ...
    python -m venv "%BUILD_VENV%"
    if errorlevel 1 (
        echo ERROR: no se pudo crear el entorno de build. Verifica que Python
        echo este instalado y disponible como "python" en el PATH.
        exit /b 1
    )
)

echo Instalando/actualizando PyInstaller en el entorno de build...
"%BUILD_VENV%\Scripts\python.exe" -m pip install --upgrade pip pyinstaller >nul
if errorlevel 1 (
    echo ERROR: no se pudo instalar pyinstaller en %BUILD_VENV%.
    echo Revisa tu conexion, la VPN o el proxy de tu empresa.
    exit /b 1
)

echo.
echo Limpiando artefactos de compilaciones anteriores...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "Ultron.spec" del /q "Ultron.spec"

REM Arma build\embed\ -- copia LIMPIA del codigo fuente minimo (backend\app,
REM requirements.txt, frontend\
REM completo -- sin __pycache__/tests/.venv/.env/keys/logs/data/workspace/
REM browser). Ver build_stage.py para el detalle exacto de que se copia y
REM que se excluye. Esto es lo que hace que Ultron.exe sea REALMENTE
REM portable (movible a cualquier carpeta): el codigo va EMBEBIDO adentro
REM del .exe, no al lado.
echo.
echo Preparando codigo fuente a embeber (build\embed\)...
"%BUILD_VENV%\Scripts\python.exe" build_stage.py
if errorlevel 1 (
    echo ERROR: no se pudo preparar el codigo a embeber.
    exit /b 1
)

REM spec exe-metadatos-version: bloque de version del .exe (empresa, producto, descripcion, version),
REM generado desde backend\app\version.py. Si falla, el build CORTA: no se publica un .exe sin metadatos
REM por un error callado.
echo.
echo Generando metadatos de version del .exe...
"%BUILD_VENV%\Scripts\python.exe" build_version_info.py "build\version_info.txt"
if errorlevel 1 (
    echo ERROR: no se pudieron generar los metadatos de version del .exe.
    exit /b 1
)

echo.
echo Compilando Ultron.exe (onefile, sin consola de PyInstaller extra)...
"%BUILD_VENV%\Scripts\python.exe" -m PyInstaller ^
    --onefile ^
    --console ^
    --name Ultron ^
    --icon "%~dp0frontend\assets\favicon.ico" ^
    --version-file "%~dp0build\version_info.txt" ^
    --add-data "%~dp0build\embed;embed" ^
    --distpath "." ^
    --workpath "build" ^
    --specpath "build" ^
    launch_vision.py

if errorlevel 1 (
    echo.
    echo ERROR: la compilacion fallo. Revisa el log de arriba.
    exit /b 1
)

echo.
echo Limpiando carpeta de trabajo intermedia...
if exist "build" rmdir /s /q "build"

echo.
echo ==========================================================
echo  Ultron.exe compilado con exito en la raiz del proyecto.
echo ==========================================================
for %%F in ("Ultron.exe") do echo  Tamano: %%~zF bytes
echo.
echo  Ultron.exe es REALMENTE portable: lleva el codigo fuente embebido
echo  adentro (backend\app, backend\scripts, requirements.txt, frontend\
echo  completo). Se puede mover a CUALQUIER carpeta -- USB, OneDrive,
echo  escritorio, otra PC -- y funciona solo, sin llevar backend\/frontend\
echo  al lado. La primera vez que se ejecute en una maquina/carpeta nueva:
echo  extrae ese codigo a %%LOCALAPPDATA%%\Ultron\app, crea ahi el entorno
echo  virtual e instala las dependencias de requirements.txt (esto SI
echo  requiere internet -- tarda varios minutos la primera vez).
echo  Arranques siguientes son casi instantaneos.
echo.

endlocal
