# ESTADO ACTUAL (ULTRON)

Última actualización: 2026-10-25 23:30 (Claude Code, modo de desarrollo automático autorizado por el usuario: «repara y arregla
todo... sin mi decisión hasta llegar al objetivo planificado... sin romper nada de lo que ya funciona»).

**Si una sesión se corta, retomar desde acá:** leé este archivo, después `git status` y `git log --oneline -5` en
`C:\Proyectos\Vision-personal` (rama `ultron-2.0`). Lo que está sin commitear es trabajo a medias: verificalo con la batería
antes de seguir.

## Objetivo planificado
1. **ULTRON 2.0** (spec `portar-desde-origen`, IN_DEVELOPMENT): las mejoras de la versión de origen 0.36.2 sobre Personal 1.1.0,
   el renombre visible a ULTRON, la migración de carpeta, la palabra de voz y el README con «Conexión a LLMs».
2. **Batería completa en verde** (sin e2e + e2e, cada e2e en su propio proceso) y guardián
   `tests/test_vision_generico.py` en verde.
3. **Commit 2.0.0 en `ultron-2.0`**, llevar `dev` a ese commit (fast-forward) y subir `ultron-2.0` y `dev` a `origin`
   (GenArtificial0001/Ultron, alias SSH `github-ultron`). `main` solo con pedido explícito.
4. **Spec `ia-local`** (APPROVED): detección de Ollama y LM Studio, REQ-001 a REQ-011, con tests y mutaciones, README al día. Versión 2.1.0.
5. **REQ-011 de portar-desde-origen:** renombrar la carpeta a `C:\Proyectos\Ultron` al final, cuando no quede nada corriendo, y
   actualizar la tarea programada `status-vision`.

## Dónde está cada cosa
- Repo: `C:\Proyectos\Vision-personal`, rama `ultron-2.0`. Remotos: `origin` = Ultron (SSH), `vision` = el repo viejo.
  Identidad local: GenArtificial0001.
- Punto de control commiteado: `9dc570c` (merge + limpieza + tests adaptados). Todo lo posterior está sin commitear: renombre,
  migración, README, CHANGELOG 2.0.0 y specs.
- Bitácora: `specs/portar-desde-origen/progress.md`.
- Salidas de las baterías: `%TEMP%\claude\<carpeta de esta sesión>\scratchpad\u2_*.txt`.
  Leer solo las líneas `^(FAILED|ERROR) ` y el resumen final.
- Scripts de apoyo (en el mismo scratchpad, `scripts/`):
  - `resolver.py`: conflictos de merge;
  - `genericos.py`: barrido de textos de la empresa de origen;
  - `renombrar_ultron.py`: renombre visible; nunca toca bytes ni docstrings;
  - `prender_interruptor.py`: fixture de Office/M365 en los tests;
  - `duplicados.py`: funciones duplicadas por el merge.

## Hecho (2026-10-25)
- **Merge y limpieza:** resueltos los 31 conflictos; sin restos de la empresa de origen (el guardián también revisa `specs/` y
  `.agent/`); configuración de IA genérica; Office y M365 apagados de fábrica; conector de M365 configurable; proxy sin predeterminado.
- **Arreglo del LSP:** espera el `initialize` (con test de un `pylsp` lento).
- **Renombre visible a ULTRON:** la entropía DPAPI no se toca; `Ultron.exe`, `Iniciar-Ultron.bat`.
- **Carpeta y migración:** `DATA_FOLDER_NAME = "Ultron"` y migración desde `Vision-personal` (`app/data_migration.py`, 8 tests).
- **Palabra de voz:** «Ultron» (y «Jarvis»).
- **Documentos:** README nuevo (con la tabla de 69 tools y «Conexión a LLMs»), CHANGELOG 2.0.0 y `version.py` en 2.0.0.
- **Batería sin e2e** antes de los últimos arreglos: 4.744 ok y 9 fallos, todos corregidos y verificados por archivo.

## Avance posterior (2026-10-25 noche)
- **Commit `17c6e45` (2.0.0) en `ultron-2.0`**, SIN subir todavía:
  - batería sin e2e: 4.757 ok y 0 fallos;
  - e2e: los 51 archivos en verde; `stop_unreachable` pasa 3/3, pero se cuelga al cerrar Chromium (QA-028, infraestructura).
- **Después del commit (sin commitear):**
  - arreglo de rendimiento: `switches.availability_filter()`, para que `definitions()` lea los interruptores una vez por paso;
  - 5 tests nuevos en `test_switches_wiring`;
  - CHANGELOG actualizado;
  - `app/llm/local_ai.py`, el módulo de `ia-local`, escrito y en el índice de git pero SIN conectar todavía a `providers.py`.
- **Corriendo:** cobertura con orden aleatorio (`u2_cov2.txt`). Si sale verde: commit, `git branch -f dev ultron-2.0`
  (fast-forward) y push de las dos ramas.

## 2.1.0 (`ia-local`) terminada (2026-10-25 noche)
- **Commits:** 2.0.0 = `cc36a56`. 2.1.0 = el commit siguiente en `ultron-2.0`, con `dev` llevado a ese commit (fast-forward);
  las dos ramas en `origin`. `main` sigue en 1.0.0.
- **Batería en verde:**
  - sin e2e: 4.784 ok y 0 fallos;
  - los 51 e2e en verde (`session_visibility` tuvo un fallo intermitente con la PC cargada y repetido dio 21/21);
  - guardián, qa_battery y `test_marca_ultron`.
- **Prueba con el Ollama real:** detección, chat, herramienta y ventana de 16.384 (ver `specs/ia-local/progress.md`).
- **Pedido del usuario:** la marca dice ULTRON y el logo de la «V» está vacío.
- **Falta:** REQ-011 de `portar-desde-origen` (renombrar la carpeta a `C:\Proyectos\Ultron` y actualizar la tarea programada
  `status-vision`); después, `portar-desde-origen` en DONE.
  - Intentado el 2026-10-25: Windows no dejó renombrar porque OpenCode (del usuario) tiene la carpeta abierta. No se cierra
    nada del usuario: se renombra cuando él cierre OpenCode.
  - Ya revisado que el `.venv` sigue andando si se mueve (se usa `python -m`) y que no hay worktrees extra.

## En curso (histórico)
- **e2e completos:** salida en `u2_e2e.txt`. Ya corregidos en los tests:
  - `capabilities_in_lists` y `m365`: los interruptores se prenden;
  - `brain_autosend`: «ultrón» llama y «visión» ya no;
  - `confluence`: el nombre de la conexión de prueba tenía un espacio.
- **Pendiente de investigar:** `test_e2e_error_messages.py::test_revalidating_a_model_error_never_claims_it_was_solved`
  (timeout de 15 s al revalidar).

## Reglas que no se negocian
- Nada de la empresa de origen en el repo, ni en `specs/` ni en `.agent/`: el guardián lo vigila.
- No tocar la carpeta de la versión de origen (solo lectura).
- No tocar `frontend/` mientras corren los e2e (los sirven en vivo).
- No romper lo que funciona: cada arreglo con su test; al final, la batería completa.
- Las barras invertidas en heredocs de bash se transforman: para reemplazos con `\`, usar el editor de archivos o un script en archivo.
