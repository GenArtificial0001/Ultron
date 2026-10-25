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

REM REQ-109 REVERTIDO (spec configuracion-limpieza-credenciales-servidor,
REM 2026-09-20): se probo cambiar a --host 0.0.0.0 para permitir conectarse
REM desde el celular en la misma LAN, pero requeria ademas crear una regla
REM de firewall de Windows para el puerto entrante (Windows Firewall
REM bloquea por defecto conexiones entrantes nuevas sin regla explicita en
REM el perfil "Domain" de una notebook corporativa) -- eso exige privilegios
REM de administrador/elevacion UAC. El usuario decidio explicitamente NO
REM automatizar la apertura de un puerto de entrada en una notebook
REM corporativa de la empresa por riesgo real de chocar con politicas de Cyber
REM Security (mismo criterio ya aplicado antes con la idea de usar un
REM proveedor de IA personal). Se revierte a 127.0.0.1 (loopback, solo esta
REM misma maquina) -- ver tambien la eliminacion de la seccion de deteccion
REM de IPs en la pestaña Servidor (REQ-107/108 removidos del todo).
REM
REM REQ-008 (spec fix-detener-y-progreso-visible): --reload-dir app hace que
REM uvicorn vigile SOLO el codigo de Vision (backend\app), no todo backend\.
REM Bug real (2026-10-11): el agente de Vision corrio "pip install" con la
REM tool bash, pip escribio .py dentro de backend\.venv y el --reload
REM reinicio Vision a mitad del turno (el filtro ".*" de uvicorn mira solo
REM el nombre del archivo, no la carpeta .venv): el proceso viejo siguio con
REM el turno sin atender conexiones y Detener quedo colgado. Con esto, ni
REM backend\.venv ni backend\workspace (scripts del agente) reinician nada;
REM tocar un .py de backend\app sigue recargando como siempre. Es relativo a
REM backend\ (el cd /d de arriba); lo verifica tests\test_dev_launcher_reload.py.
REM Hallazgo del 2026-10-17 (spec office-outlook-teams-mcp): con un chat abierto (la conexion de streaming /api/chat/stream/{id} nunca termina sola) el
REM reinicio automatico de --reload se CUELGA: uvicorn espera para siempre a que esa conexion cierre y el servidor viejo sigue sirviendo el codigo viejo. Medido con una
REM app minima: sin limite no se reinicia; con 3 s se reinicia en ~4 s. Por eso --timeout-graceful-shutdown 5 (el chat se reengancha solo al volver). Lo vigila
REM tests	est_dev_launcher_reload.py.
".venv\Scripts\python.exe" -m uvicorn app.main:app --reload --reload-dir app --timeout-graceful-shutdown 5 --host 127.0.0.1 --port 8030

pause
