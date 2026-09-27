"""
Log de diagnostico de rendimiento por turno (investigacion 2026-09: "Vision
tarda mucho en responder", varios usuarios reportando lentitud a la vez).

Una linea JSON por turno en `backend/logs/perf.log` (mismo patron de archivo
que `audit.log`, ver `tools/base.py:_audit`) con el desglose real de tiempo:
armado del system prompt (solo si corrio en este turno), tiempo hasta el
primer texto del modelo (TTFT -- separa "es el modelo/el proveedor de IA" de "es
Vision"), y cada tool-call con su duracion real. Pensado para grepear un
`session_id` puntual cuando alguien reporte lentitud:

    grep '"session_id": "abc123"' backend/logs/perf.log | tail -5

NO reemplaza `tool_timings.json` (esa sigue siendo la fuente de la barra de
progreso adaptativa, con su propia ventana movil para estimar presupuestos) --
este log es de solo lectura para diagnostico humano, nunca se lee de vuelta
por el propio Vision.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


class TurnPerfRecorder:
    """Acumula los tiempos de UN turno de `run_turn()` y los vuelca a
    perf.log al cerrar (`finish()`, llamado siempre en el `finally` del
    turno -- un turno cortado por STOP o por error igual deja rastro)."""

    def __init__(self, session_id: str, model: str | None) -> None:
        self.session_id = session_id
        self.model = model or ""
        self._started_at = time.monotonic()
        self.system_prompt_build_ms: float | None = None
        self.ttft_ms: float | None = None
        self.tool_calls: list[dict[str, Any]] = []
        self._first_token_seen = False

    def mark_system_prompt_built(self, ms: float) -> None:
        self.system_prompt_build_ms = round(ms, 1)

    def mark_first_token(self) -> None:
        """Se llama en el primer `text-delta` real del turno -- ignora
        llamadas posteriores (solo interesa el PRIMER token)."""
        if self._first_token_seen:
            return
        self._first_token_seen = True
        self.ttft_ms = round((time.monotonic() - self._started_at) * 1000, 1)

    def record_tool_call(self, name: str, ms: float) -> None:
        self.tool_calls.append({"name": name, "ms": round(ms, 1)})

    def finish(self, status: str = "done") -> None:
        total_ms = round((time.monotonic() - self._started_at) * 1000, 1)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id,
            "model": self.model,
            # 'done': el turno termino (normal o via STOP -- ambos casos
            # salen del generador sin excepcion). 'error': se propago una
            # excepcion real (ver run_turn() en llm/loop.py).
            "status": status,
            "ttft_ms": self.ttft_ms,
            "system_prompt_build_ms": self.system_prompt_build_ms,
            "tool_calls": self.tool_calls,
            "total_ms": total_ms,
        }
        try:
            log_path = get_settings().perf_log_file
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            # Nunca debe romper el turno del usuario por un problema de
            # logging de diagnostico -- mismo criterio que tool_timings.py.
            logger.warning("No se pudo escribir perf.log", exc_info=True)
