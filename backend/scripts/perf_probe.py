#!/usr/bin/env python
"""
Investigacion de rendimiento 2026-09 ("Vision tarda mucho en responder"):
script chico para correr A MANO cuando alguien reporte lentitud, y saber en
segundos si el cuello de botella es el MODELO (el proveedor de IA) o UN CONECTOR
puntual (Oracle/BigQuery/SharePoint/SAP/Graph) -- sin tener que reproducir
una conversacion completa ni leer perf.log de un turno real.

Mide contra los sistemas REALES configurados en backend/.env (no mockeados
-- a diferencia de la suite de tests). No requiere el backend levantado
(usa los conectores/cliente directo, igual que hace app/llm/loop.py).

Uso:
    cd vision-web/backend
    python scripts/perf_probe.py            # todo lo habilitado
    python scripts/perf_probe.py --only oracle,llm
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings


def _fmt_ms(ms: float) -> str:
    return f"{ms:,.0f} ms"


def probe_llm() -> tuple[bool, str, float]:
    """Tiempo de get_client() (auth, cache-miss = OAuth real) + tiempo hasta
    el PRIMER chunk de una respuesta streameada con un prompt trivial --
    aisla "tarda el modelo/el proveedor de IA" de "tarda Vision"."""
    from app.llm.client import get_client

    settings = get_settings()
    start = time.perf_counter()
    try:
        client = get_client()
        client_ms = (time.perf_counter() - start) * 1000

        stream_start = time.perf_counter()
        stream = client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": "Respondé solo: ok"}],
            stream=True,
            max_tokens=5,
        )
        first_chunk_ms = None
        for _chunk in stream:
            first_chunk_ms = (time.perf_counter() - stream_start) * 1000
            break
        total_ms = (time.perf_counter() - start) * 1000
        detail = (
            f"cliente/auth: {_fmt_ms(client_ms)} - primer chunk: "
            f"{_fmt_ms(first_chunk_ms) if first_chunk_ms is not None else 'sin respuesta'}"
        )
        return True, detail, total_ms
    except Exception as exc:  # noqa: BLE001 - probe de diagnostico, se reporta cualquier falla
        return False, str(exc), (time.perf_counter() - start) * 1000


def probe_oracle() -> tuple[bool, str, float]:
    from app.connectors.oracle_connector import OracleConnector

    start = time.perf_counter()
    ok = OracleConnector().test_connection()
    return ok, ("conectado" if ok else "fallo (ver logs)"), (time.perf_counter() - start) * 1000


def probe_bigquery() -> tuple[bool, str, float]:
    from app.connectors.bigquery_connector import BigQueryConnector

    start = time.perf_counter()
    ok = BigQueryConnector().test_connection()
    return ok, ("conectado" if ok else "fallo (ver logs)"), (time.perf_counter() - start) * 1000


def probe_sharepoint() -> tuple[bool, str, float]:
    from app.connectors.sharepoint_connector import SharePointConnector

    start = time.perf_counter()
    ok = SharePointConnector().test_connection()
    return ok, ("conectado" if ok else "fallo (ver logs)"), (time.perf_counter() - start) * 1000


def probe_sap() -> tuple[bool, str, float]:
    from app.connectors.sap_connector import SapConnector

    start = time.perf_counter()
    ok = SapConnector().test_connection()
    return ok, ("conectado" if ok else "fallo (ver logs)"), (time.perf_counter() - start) * 1000


def probe_graph() -> tuple[bool, str, float]:
    from app.connectors.graph_connector import GraphConnector

    start = time.perf_counter()
    ok = GraphConnector().test_connection()
    return ok, ("conectado" if ok else "fallo (ver logs)"), (time.perf_counter() - start) * 1000


# (nombre, funcion, flag de settings que lo habilita -- None = siempre se intenta)
_PROBES = [
    ("llm", probe_llm, None),
    ("oracle", probe_oracle, "enable_oracle"),
    ("bigquery", probe_bigquery, "enable_bigquery"),
    ("sharepoint", probe_sharepoint, "enable_sharepoint"),
    ("sap", probe_sap, "enable_sap"),
    ("graph", probe_graph, "enable_sharepoint"),  # comparte credenciales Azure con SharePoint
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        help="Lista separada por comas de sondas a correr (ej. oracle,llm). Por defecto, todas las habilitadas.",
    )
    args = parser.parse_args()
    only = {name.strip() for name in args.only.split(",")} if args.only else None

    settings = get_settings()
    print(f"Vision perf_probe -- modelo default: {settings.llm_model}\n")

    results = []
    for name, fn, flag in _PROBES:
        if only is not None and name not in only:
            continue
        if flag is not None and not getattr(settings, flag, True):
            print(f"[{name:10}] omitido ({flag}=false)")
            continue
        print(f"[{name:10}] midiendo...", end=" ", flush=True)
        try:
            ok, detail, elapsed_ms = fn()
        except Exception as exc:  # noqa: BLE001 - nunca debe tumbar el probe entero
            ok, detail, elapsed_ms = False, f"excepcion: {exc}", 0.0
        status = "OK  " if ok else "FAIL"
        print(f"{status} {_fmt_ms(elapsed_ms):>10}  -- {detail}")
        results.append((name, ok, elapsed_ms))

    if not results:
        print("Nada para medir (revisa --only o los flags enable_* en .env).")
        return

    print("\nResumen (de mas lento a mas rapido):")
    for name, ok, elapsed_ms in sorted(results, key=lambda r: -r[2]):
        # ASCII a proposito (no ✓/✗) -- la consola de Windows por defecto
        # (cp1252) no siempre soporta esos caracteres y el script rompe con
        # UnicodeEncodeError justo al final, despues de medir todo.
        marker = "OK  " if ok else "FAIL"
        print(f"  [{marker}] {name:10} {_fmt_ms(elapsed_ms):>10}")


if __name__ == "__main__":
    main()
