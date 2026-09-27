"""
Tool `render_chart` (spec dashboard-graficos): el LLM la llama para
pedirle a Vision que dibuje un grafico de dona o de barras sobre el
resultado de una tool de datos YA EJECUTADA en la sesion
(`oracle_query`/`bigquery_query`/`sharepoint_list_items`/`sap_query`).

Decision de diseno (REQ-001, spec): el LLM decide EXPLICITAMENTE el tipo
de grafico y las columnas a usar -- no hay heuristica automatica sobre el
CSV. Esto es mas preciso para lo que el usuario realmente pidio ("hazme
un grafico de dona con...") que adivinar a partir de la forma de los
datos.

REQ-002: a diferencia del resto de las tools (que devuelven texto que el
LLM lee), esta tool NO le muestra el dataset completo al LLM en su
resultado -- busca el `result` COMPLETO (sin el truncado a 4000
caracteres del SSE, ver app/llm/loop.py) directamente en SQLite (spec
persistencia-sqlite-sesiones) y arma los datos del grafico ahi mismo,
server-side. Esto resuelve una limitacion ya conocida y documentada en el
frontend (app.js: "el dashboard refleja una muestra, no el total exacto")
sin que el LLM tenga que re-transcribir un CSV grande como argumento.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

from app.persistence import service as persistence
from app.tools.base import Tool

logger = logging.getLogger(__name__)

# Mismo set de tools que ya alimentan el dashboard de KPIs/tabla existente
# (ver DASHBOARD_TOOLS en frontend/js/app.js) -- son las unicas que
# devuelven CSV, formato que esta tool sabe parsear.
CHARTABLE_TOOLS = [
    "oracle_query",
    "bigquery_query",
    "sharepoint_list_items",
    "sap_query",
]


def _parse_csv(text: str) -> tuple[list[str], list[list[str]]]:
    """Parser CSV real (stdlib `csv`, no un split naive por comas) --
    soporta comillas/comas dentro de celdas igual que pandas.to_csv() ya
    genera del lado de las tools de datos."""
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if r]
    if not rows:
        return [], []
    return rows[0], rows[1:]


def _render_chart_execute(args: dict, session: Any = None) -> str:
    if session is None or not session.session_id:
        return "Error: no hay sesion activa para graficar."

    chart_type = args.get("chart_type", "")
    if chart_type not in ("donut", "bar"):
        return "Error: 'chart_type' debe ser 'donut' o 'bar'."

    category_column = args.get("category_column", "")
    value_column = args.get("value_column", "")
    if not category_column or not value_column:
        return "Error: faltan 'category_column' y/o 'value_column'."

    title = args.get("title") or f"{value_column} por {category_column}"
    call_id = args.get("tool_call_id")

    # REQ-002: intenta el dataset COMPLETO desde SQLite primero -- solo
    # cae al resultado truncado (pasado por el propio LLM como fallback en
    # `csv_fallback`, ver parametros de la tool) si no hay persistencia
    # disponible o no se encontro la tool-call referenciada.
    tool_part = persistence.find_tool_result(
        session.session_id, call_id, CHARTABLE_TOOLS
    )
    csv_text = tool_part.get("result") if tool_part else None
    source = "completo (SQLite)"
    if not csv_text:
        csv_text = args.get("csv_fallback", "")
        source = "truncado (sin persistencia disponible)"
    if not csv_text:
        return (
            "Error: no se encontro ningun resultado de oracle_query/bigquery_query/"
            "sharepoint_list_items/sap_query para graficar en esta sesion."
        )

    headers, rows = _parse_csv(csv_text)
    if category_column not in headers:
        return f"Error: la columna '{category_column}' no existe en el resultado. Columnas disponibles: {', '.join(headers)}."
    if value_column not in headers:
        return f"Error: la columna '{value_column}' no existe en el resultado. Columnas disponibles: {', '.join(headers)}."

    cat_idx = headers.index(category_column)
    val_idx = headers.index(value_column)

    # Agrupa (suma) por categoria -- una fila por categoria, no una barra
    # por fila cruda (varias filas pueden compartir la misma categoria,
    # ej. varios "storage bin" con el mismo "storage type").
    grouped: dict[str, float] = {}
    for row in rows:
        if cat_idx >= len(row) or val_idx >= len(row):
            continue
        cat = row[cat_idx] or "(vacío)"
        try:
            val = float(row[val_idx])
        except (ValueError, TypeError):
            val = (
                1.0  # columna no numerica (ej. contar ocurrencias) -- cuenta 1 por fila
            )
        grouped[cat] = grouped.get(cat, 0.0) + val

    labels = list(grouped.keys())
    values = list(grouped.values())

    if session is not None:
        # REQ-001/003/004/005: evento fuera-de-banda (mismo mecanismo que
        # 'todo'/'question') -- el frontend dibuja con Chart.js al
        # recibirlo, sin que este resultado de texto tenga que describir
        # los datos (el LLM ya los eligio, no necesita releerlos).
        session.push_event(
            "chart-ready",
            {
                "chart_type": chart_type,
                "title": title,
                "labels": labels,
                "values": values,
                "category_column": category_column,
                "value_column": value_column,
            },
        )

    total_rows = len(rows)
    return (
        f"Grafico de {chart_type} '{title}' generado con {len(labels)} categorias "
        f"({total_rows} filas del dataset {source}). Ya se muestra en el dashboard."
    )


render_chart_tool = Tool(
    name="render_chart",
    description=(
        "Dibuja un grafico de dona o de barras en un modal, a partir del resultado de una "
        "consulta de datos YA EJECUTADA en esta sesion (oracle_query/bigquery_query/"
        "sharepoint_list_items/sap_query, o bash/otra tool si se pasa csv_fallback). Usar "
        "cuando el usuario pida un grafico/visualizacion, INCLUSO si pide datos 'de ejemplo' "
        "o ficticios para probar la funcionalidad -- en ese caso, generar los valores de "
        "ejemplo vos mismo (con bash+echo o directamente en csv_fallback) y llamar esta tool "
        "de inmediato, SIN cargar skills de brainstorming/diseno ni preguntar confirmacion "
        "de antemano: pedir un grafico de prueba es una accion tecnica directa, no una "
        "decision de negocio que requiera relevamiento previo."
    ),
    parameters={
        "type": "object",
        "properties": {
            "chart_type": {
                "type": "string",
                "enum": ["donut", "bar"],
                "description": "'donut' para comparar partes de un total (ej. vacias/llenas); 'bar' para comparar categorias en un eje (ej. storage type).",
            },
            "category_column": {
                "type": "string",
                "description": "Nombre exacto de la columna del CSV a usar como categoria (eje X en barras, porciones en dona).",
            },
            "value_column": {
                "type": "string",
                "description": "Nombre exacto de la columna del CSV a sumar como valor (eje Y en barras, tamaño de porcion en dona).",
            },
            "title": {
                "type": "string",
                "description": "Titulo del grafico. Si no se especifica, se genera uno a partir de las columnas.",
            },
            "tool_call_id": {
                "type": "string",
                "description": "ID de la tool-call especifica a graficar (visible en el historial de la conversacion). Si se omite, usa el resultado MAS RECIENTE de oracle_query/bigquery_query/sharepoint_list_items/sap_query en esta sesion.",
            },
            "csv_fallback": {
                "type": "string",
                "description": "CSV (o el texto del resultado tal cual se vio) a usar SOLO si no hay persistencia disponible para buscar el dataset completo -- puede venir truncado.",
            },
        },
        "required": ["chart_type", "category_column", "value_column"],
    },
    execute=_render_chart_execute,
    # Toca session.push_event() (asyncio.Queue) -- ver comentario de
    # Tool.thread_safe en app/tools/base.py.
    thread_safe=False,
)
