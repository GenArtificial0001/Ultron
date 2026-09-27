@echo off
REM Levanta Vision en modo desarrollo (--reload) en http://127.0.0.1:8030/.
REM Se puede dejar corriendo: cualquier cambio en backend/frontend recarga
REM solo. Cerrar esta ventana apaga el servidor.

REM VISION_DEV_MODE=1: le dice a app/config.py que use backend\.env (tu
REM configuracion de desarrollo) en vez de %LOCALAPPDATA%\Vision-personal\.env,
REM que es la que usa Vision.exe (ver config.py).
set "VISION_DEV_MODE=1"

cd /d "%~dp0backend"

REM Proxy de la empresa (spec vision-generico REQ-004): si backend\.env trae
REM HTTP_PROXY/HTTPS_PROXY, pip los usa para instalar dependencias. Lo ya
REM definido en Windows manda.
for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b /i "HTTP_PROXY= HTTPS_PROXY= NO_PROXY=" .env 2^>nul`) do (
    if not "%%B"=="" if not defined %%A set "%%A=%%B"
)

if not exist ".venv\Scripts\python.exe" (
    echo No se encontro el entorno virtual en backend\.venv
    echo Creandolo ahora...
    python -m venv .venv
)

REM Verifica en CADA arranque que las dependencias de requirements.txt esten
REM instaladas, sin pagar un pip install completo cuando nada cambio: se
REM compara un hash SHA256 de requirements.txt contra ".venv\.vision-deps-ok".
set "REQ_HASH="
for /f "skip=1 delims=" %%H in ('certutil -hashfile requirements.txt SHA256 2^>nul') do (
    if not defined REQ_HASH set "REQ_HASH=%%H"
)
set "REQ_HASH=%REQ_HASH: =%"

set "OLD_HASH="
if exist ".venv\.vision-deps-ok" set /p OLD_HASH=<".venv\.vision-deps-ok"

if /i "%REQ_HASH%"=="%OLD_HASH%" (
    echo Dependencias al dia -- salteando pip install.
) else (
    echo requirements.txt cambio -- instalando/actualizando dependencias...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        REM "pip install -r" es todo o nada: si el indice de pip de la empresa
        REM no tiene algun paquete (ej. win11toast), falla la instalacion
        REM completa. Se reintenta sumando el PyPI publico.
        echo El pip install normal fallo -- reintentando con PyPI publico como fallback...
        ".venv\Scripts\python.exe" -m pip install -r requirements.txt --extra-index-url https://pypi.org/simple
    )
    if errorlevel 1 (
        echo ADVERTENCIA: no se pudieron instalar/actualizar todas las dependencias
        echo ^(revisa tu conexion, la VPN o el proxy de tu empresa^). Vision va a
        echo arrancar igual con lo que ya este instalado.
    ) else (
        > ".venv\.vision-deps-ok" echo %REQ_HASH%
    )
)

echo.
echo Levantando Vision en http://127.0.0.1:8030/ ...
echo Cerrar esta ventana apaga el servidor.
echo.

REM Abre el navegador cuando el servidor responde (script standalone, solo
REM stdlib), sin bloquear el arranque de uvicorn de abajo.
start /B "" ".venv\Scripts\python.exe" scripts\wait_and_open_browser.py

REM Solo esta PC (127.0.0.1): exponer el puerto a la red exige una regla de
REM firewall con permisos de administrador.
".venv\Scripts\python.exe" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8030

pause
