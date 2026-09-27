"""
Framework de comandos "/" (spec comandos-slash): define los comandos
del sistema portados de OpenCode (`/init`, `/compact`, `/export`,
`/exit` -- undo/share/unshare se ELIMINARON por pedido explicito del
usuario 2026-09) y el CRUD de comandos propios del usuario (nombre +
prompt fijo).

REQ-001/006/007: init/export/rename ejecutan acciones reales; `/compact`
tambien es real desde la spec gestion-contexto-conversacion.

REQ-008/009: los comandos propios son simples (nombre + texto de prompt
que se manda al LLM, igual camino que una skill) -- se guardan como UN
ARCHIVO POR COMANDO en `<carpeta-de-proyecto-de-la-sesion>/commands/
<nombre>.md` (arquitectura carpeta-por-sesion, definitiva -- ver
docstring largo en app/persistence/service.py). Cambio respecto a la
version original de esta spec: antes era un unico `session/commands.json`
con TODOS los comandos de una carpeta global; ahora cada sesion tiene sus
propios comandos, un `.md` legible por archivo (mas facil de editar a
mano fuera de Vision si hiciera falta) en vez de un blob JSON compartido.
Sin carpeta de proyecto asignada a la sesion, no hay donde persistir (se
mantienen solo en memoria del lado del cliente)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# REQ-001/003/004/005/006/007: definicion de los comandos de
# OpenCode + `/rename` (agregado por pedido explicito, no existe en
# OpenCode). `kind` determina que tipo de modal arma el frontend:
#   - "confirm": modal simple de Aceptar/Cancelar (init, exit)
#   - "export": modal con boton de descarga (export)
#   - "rename": modal con input prellenado (rename)
SYSTEM_COMMANDS: list[dict[str, Any]] = [
    {
        "name": "init",
        "description": "Analiza el proyecto y crea/actualiza AGENTS.md",
        "kind": "confirm",
        "confirm_message": "Se va a analizar el proyecto y crear/actualizar AGENTS.md en la raíz.",
    },
    {
        "name": "export",
        "description": "Exporta esta conversación a Markdown",
        "kind": "export",
        "confirm_message": "Se exportará esta conversación a Markdown.",
    },
    {
        "name": "rename",
        "description": "Cambia el nombre de esta sesión",
        "kind": "rename",
    },
    {
        # spec gestion-contexto-conversacion REQ-002: accion real que
        # llama al endpoint de compactacion manual tras confirmar.
        "name": "compact",
        "description": "Resumir el contexto para ahorrar tokens",
        "kind": "confirm",
        "confirm_message": (
            "Se van a resumir los turnos más viejos de esta conversación para liberar "
            "espacio de contexto, preservando los mensajes más recientes intactos."
        ),
        "action": "compact",
    },
    # Pedido explicito del usuario (2026-09-14): "/qa-audit" (spec
    # bateria-qa-integracion) se SACA de esta lista -- un usuario comun de
    # Vision no tiene por que ver un comando de tooling de desarrollo
    # mezclado con sus comandos normales ("estamos mezclando las cosas").
    # NO se borra la funcionalidad: el endpoint sigue intacto y usable
    # (`POST /api/commands/qa-audit/{session_id}` en main.py) para quien
    # este desarrollando Vision -- solo deja de ofrecerse en el menu "/".
    {
        "name": "exit",
        "description": "Cierra esta conversación y empieza una nueva",
        "kind": "confirm",
        "confirm_message": "Se cerrará esta conversación y se iniciará una nueva vacía.",
        # REQ-007: distingue de "init"/"export" en el frontend -- al
        # confirmar, dispara el mismo flujo que el boton "+ Nueva sesión"
        # ya existente, en vez de arrancar un turno de chat.
        "action": "new_session",
    },
    {
        # spec modo-plan-loop REQ-010: `kind` nuevo -- ninguno de los
        # existentes calzaba (confirm/export/rename/unavailable), segun
        # la exploracion previa a la spec. El frontend arma un mensaje
        # dinamico con la cantidad de tareas pendientes antes de mostrar
        # el modal (necesita consultar session.todos, no es un texto fijo
        # como los demas "confirm").
        "name": "loop",
        "description": "Ejecuta automáticamente la lista de tareas pendiente",
        "kind": "loop",
    },
    {
        # Pedido del usuario (2026-09): limpiar TODA la conversacion de la
        # sesion (preguntas y respuestas, viejas y nuevas). `kind` propio
        # para que el frontend pida confirmacion (es destructivo) antes de
        # llamar POST /api/session/{id}/clear.
        "name": "clear",
        "description": "Borra toda la conversación de la sesión actual",
        "kind": "clear",
    },
    {
        # Arquitectura carpeta-por-sesion: comando NUEVO pedido
        # explicitamente por el usuario, separado de la seccion "Skills"
        # (siempre globales) del menu "/". `kind` propio: el frontend
        # consulta GET /api/local-skills/{session_id} y arma un modal de
        # solo-lectura listando las skills de `skills/` de la carpeta de
        # proyecto de esta sesion.
        "name": "local-skills",
        "description": "Lista las skills locales de la carpeta de esta sesión",
        "kind": "local-skills",
    },
]

# REQ-006: nombres reservados -- un comando propio no puede pisar uno de
# estos (regla de negocio de la spec).
RESERVED_NAMES = {c["name"] for c in SYSTEM_COMMANDS}


COMMANDS_SUBDIR = "commands"


def _commands_dir(project_path: str) -> Path:
    return Path(project_path) / COMMANDS_SUBDIR


def _command_file(project_path: str, name: str) -> Path:
    return _commands_dir(project_path) / f"{name}.md"


def list_custom_commands(project_path: str | None) -> list[dict[str, str]]:
    """REQ-008/009: lista los comandos propios guardados en `commands/`
    dentro de la carpeta de proyecto de la sesion -- un `.md` por comando,
    el nombre del archivo (sin extension) ES el nombre del comando y el
    contenido completo ES el prompt. Devuelve [] si no hay carpeta
    asignada o la subcarpeta no existe todavia -- nunca lanza (mismo
    criterio 'mejor esfuerzo' que el resto de la persistencia opcional)."""
    if not project_path:
        return []
    directory = _commands_dir(project_path)
    if not directory.exists():
        return []
    commands = []
    try:
        for md_file in sorted(directory.glob("*.md")):
            try:
                prompt = md_file.read_text(encoding="utf-8")
            except OSError:
                continue
            commands.append({"name": md_file.stem, "prompt": prompt})
    except OSError:
        logger.warning("No se pudo listar comandos en '%s'", directory, exc_info=True)
        return []
    return commands


def save_custom_command(
    project_path: str, name: str, prompt: str
) -> list[dict[str, str]]:
    """REQ-008/009: crea o actualiza (mismo nombre = edicion, sobrescribe
    el .md) un comando propio. Lanza ValueError si el nombre esta vacio,
    es un nombre reservado del sistema, o el prompt esta vacio -- el
    endpoint FastAPI traduce esto a un HTTPException 400."""
    name = name.strip().lower()
    prompt = prompt.strip()
    if not name:
        raise ValueError("El nombre del comando no puede estar vacío.")
    if name in RESERVED_NAMES:
        raise ValueError(
            f"'/{name}' es un comando del sistema y no se puede reemplazar."
        )
    if not prompt:
        raise ValueError("El prompt del comando no puede estar vacío.")

    path = _command_file(project_path, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prompt, encoding="utf-8")
    return list_custom_commands(project_path)


def delete_custom_command(project_path: str, name: str) -> list[dict[str, str]]:
    """REQ-009: borra el .md de un comando propio por nombre. No-op (sin
    error) si no existia -- borrar dos veces lo mismo no deberia romper la
    UI."""
    if project_path:
        path = _command_file(project_path, name.strip().lower())
        path.unlink(missing_ok=True)
    return list_custom_commands(project_path)


# REQ-003: prompt REAL de OpenCode (packages/opencode/src/command/template/
# initialize.txt), portado casi textual -- unicamente se tradujo al
# espanol y se removio `$ARGUMENTS`/`${path}` (placeholders de su sistema
# de templates que Vision no tiene) dejando el resto de la estructura y
# el nivel de detalle intactos, por decision explicita del usuario de
# reusar el diseno de OpenCode en vez de reinventarlo.
INIT_PROMPT = """Creá o actualizá `AGENTS.md` para este repositorio.

El objetivo es un archivo de instrucciones compacto que ayude a futuras sesiones de agentes a evitar errores y arrancar rápido. Cada línea debería responder: "¿Un agente probablemente se perdería esto sin ayuda?". Si no, no lo incluyas.

## Cómo investigar

Leé primero las fuentes de mayor valor:
- `README*`, manifiestos raíz, configuración del workspace, lockfiles
- configuración de build, test, lint, formatter, typecheck y codegen
- workflows de CI y configuración de pre-commit / task runner
- archivos de instrucciones existentes (`AGENTS.md`, `CLAUDE.md`, `.cursor/rules/`, `.cursorrules`, `.github/copilot-instructions.md`)

Si la arquitectura sigue sin quedar clara después de leer configuración y docs, inspeccioná una cantidad chica de archivos de código representativos para encontrar los entrypoints reales, los límites entre paquetes, y el flujo de ejecución. Preferí leer los archivos que explican cómo está armado el sistema por sobre archivos hoja al azar.

Preferí fuentes de verdad ejecutables por sobre prosa. Si la documentación contradice la configuración o los scripts, confiá en la fuente ejecutable y quedate solo con lo que puedas verificar.

## Qué extraer

Buscá los datos de mayor señal para un agente que trabaje en este repo:
- comandos exactos de desarrollo, especialmente los no obvios
- cómo correr un solo test, un solo paquete, o un paso de verificación puntual
- orden de comandos requerido cuando importa, como `lint -> typecheck -> test`
- límites de monorepo o multi-paquete, propietarios de directorios principales, y los entrypoints reales de app/librería
- particularidades del framework o toolchain: código generado, migraciones, codegen, artefactos de build, carga especial de env, servidores de desarrollo, flujo de deploy de infra
- convenciones de estilo o workflow específicas del repo que difieren de los defaults
- particularidades de testing: fixtures, prerequisitos de tests de integración, workflows de snapshots, servicios requeridos, suites lentas o inestables
- restricciones importantes de archivos de instrucciones existentes que valga la pena preservar

Buen contenido de `AGENTS.md` suele ser contexto ganado con esfuerzo que requirió leer varios archivos para inferir.

## Preguntas

Solo preguntale al usuario si el repo no puede responder algo importante. Usá la tool `question` para como mucho un lote corto de preguntas.

Buenas preguntas:
- convenciones de equipo no documentadas
- expectativas de branch / PR / release
- prerequisitos de setup o test conocidos pero no escritos

No preguntes sobre nada que el repo ya deje claro.

## Reglas de escritura

Incluí solo guía de alta señal específica del repo, como:
- comandos exactos y atajos que el agente adivinaría mal de otra forma
- notas de arquitectura que no son obvias a partir de los nombres de archivo
- convenciones que difieren de los defaults del lenguaje o framework
- requisitos de setup, particularidades del entorno, y trampas operacionales
- referencias a fuentes de instrucciones existentes que importan

Excluí:
- consejos genéricos de software
- tutoriales largos o árboles de archivos exhaustivos
- convenciones obvias del lenguaje
- afirmaciones especulativas o cualquier cosa que no puedas verificar
- contenido que estaría mejor en otro archivo

Ante la duda, omití.

Preferí secciones y bullets cortos. Si el repo es simple, mantené el archivo simple. Si el repo es grande, resumí los pocos datos estructurales que realmente cambian cómo debería trabajar un agente.

Si `AGENTS.md` ya existe, mejoralo en el lugar en vez de reescribirlo a ciegas. Preservá la guía útil ya verificada, eliminá relleno o afirmaciones desactualizadas, y reconciliá con el estado actual del código."""
