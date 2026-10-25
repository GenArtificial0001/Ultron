"""spec informe-tecnico-hablado: la frase hablada de cada accion (app/tech_report.py)."""

from __future__ import annotations

import json

import pytest

from app.tech_report import HIDDEN, describe_call, first_comment, redact


def call(name, **args):
    return describe_call(name, json.dumps(args))


# ---------------------------------------------------------------------------
# REQ-002: una frase por accion, armada con lo que ejecuta
# ---------------------------------------------------------------------------
def test_writing_a_file_says_what_its_comment_says():
    content = "# Lee el Excel de stock y agrupa por pieza\nimport pandas as pd\n"
    assert call("write", path="C:/trabajo/reporte.py", content=content) == (
        "Escribo reporte.py: lee el Excel de stock y agrupa por pieza"
    )


def test_editing_uses_the_comment_of_the_new_code_and_drops_the_req_prefix():
    new = "    // REQ-004: valida el stock mínimo antes de generar la orden\n    if (x) {}"
    assert call("edit", path="src/orden.js", old_string="a", new_string=new) == (
        "Edito orden.js: valida el stock mínimo antes de generar la orden"
    )


def test_without_a_useful_comment_it_only_names_the_file():
    content = "#!/usr/bin/env python\n# -*- coding: utf-8 -*-\n# ----------\nprint(1)\n"
    assert call("write", path="a/b/script.py", content=content) == "Escribo script.py"


def test_a_command_is_read_by_its_comment_or_by_its_first_line():
    assert call("bash", command="# Cuento las filas del CSV exportado\nwc -l datos.csv") == (
        "Ejecuto un comando: cuento las filas del CSV exportado"
    )
    assert call("bash", command="python reporte.py --mes 9\necho listo") == "Ejecuto: python reporte.py --mes 9"


def test_queries_name_the_system_and_the_tables():
    sql = "SELECT p.id FROM inventario.STOCK s JOIN PIEZAS p ON p.id = s.pieza"
    assert call("oracle_query", query=sql) == "Consulto Oracle: tablas STOCK, PIEZAS"
    assert call("bigquery_query", query="select * from `proj.ds.ventas`") == "Consulto BigQuery: tabla ventas"
    assert call("bigquery_query", query="SELECT 1") == "Consulto BigQuery"


def test_other_actions():
    assert call("webfetch", url="https://docs.python.org/3/library/re.html") == "Abro la página docs.python.org"
    assert call("render_chart", chart_type="bar", category_column="a", value_column="b", title="Stock por planta") == (
        "Armo un gráfico: Stock por planta"
    )
    assert call("sap_query", service_name="API_X", entity_name="A_Product") == "Consulto SAP: A_Product"
    assert call("sharepoint_list_items", list_name="Pedidos") == "Consulto SharePoint: lista Pedidos"


def test_long_phrases_are_cut():
    command = "python " + " ".join(f"arg{i}" for i in range(40))
    phrase = call("bash", command=command)
    assert len(phrase.split()) <= 21 and phrase.endswith("…")


# ---------------------------------------------------------------------------
# REQ-003: solo las acciones
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["read", "glob", "grep", "skill", "question", "todowrite", "record_decision"])
def test_non_actions_are_silent(name):
    assert describe_call(name, json.dumps({"path": "x.py", "pattern": "*.py", "name": "algo"})) is None


def test_broken_arguments_do_not_break_the_turn():
    assert describe_call("bash", "{no es json") == "Ejecuto un comando"
    assert describe_call("write", None) == "Escribo un archivo"


# ---------------------------------------------------------------------------
# REQ-007: nunca se dice un secreto
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("set API_KEY=sk_live_abcdef1234567890", "sk_live_abcdef1234567890"),
        ("curl -H 'Authorization: Bearer eyJhbGciOi.eyJzdWIi.c2lnbmF0dXJl' url", "eyJhbGciOi"),
        ("export GROQ=gsk_ABCdef1234567890xyz", "gsk_ABCdef1234567890xyz"),
        ("psql postgresql://admin:SuperSecreta1@db.local/app", "SuperSecreta1"),
        ("sqlplus scott/tiger123@orcl", "tiger123"),
        ('password = "hunter2"', "hunter2"),
        ("mysql --password Clave2026 -u root", "Clave2026"),
        ("clave: 9f8e7d6c5b4a39281726354453627180a", "9f8e7d6c5b4a39281726354453627180a"),
    ],
)
def test_secrets_are_hidden(text, secret):
    hidden = redact(text)
    assert secret not in hidden
    assert HIDDEN in hidden


def test_redaction_reaches_the_spoken_phrase():
    phrase = call("bash", command="set ELEVENLABS_API_KEY=sk_1234567890abcdef && python app.py")
    assert "sk_1234567890abcdef" not in phrase and HIDDEN in phrase


def test_normal_text_is_not_hidden():
    text = "Lee el token de acceso y crea la carpeta con mkdir -p reportes"
    assert redact(text) == text


def test_first_comment_ignores_shebang_and_decorations():
    assert first_comment("#!/bin/bash\n# =====\n# Copia los backups al servidor\ncp a b") == (
        "copia los backups al servidor"
    )


# ---------------------------------------------------------------------------
# REQ-008: recorrido del codigo en vivo (fragmentos como los manda el modelo)
# ---------------------------------------------------------------------------
from app.tech_report import LiveNarrator, task_transitions  # noqa: E402

DASHBOARD = (
    "<!DOCTYPE html>\n<html>\n<head>\n<title>Panel de Salud</title>\n"
    "<style>\n#sidebar { float: right; }\n/* Colores de las tarjetas de KPIs */\n</style>\n</head>\n<body>\n"
    "<!-- Sidebar a la derecha con el menú de secciones -->\n"
    '<aside id="sidebar" class="sidebar sidebar-right">\n</aside>\n'
    "<!-- Tres tarjetas de KPIs: pacientes, consultas y diagnósticos -->\n"
    '<div class="card kpi">Pacientes</div>\n<div class="card kpi">Consultas</div>\n'
    '<canvas id="consultasPorMes"></canvas>\n'
    "<script>\n// Pide los datos de pacientes a la API interna\n"
    "fetch('https://api.hospital.test/pacientes?token=abc123abc123abc123abc123abc123abc123')\n"
    "</script>\n</body>\n</html>\n"
)


def feed_in_chunks(narrator, raw, size=7):
    spoken = []
    for i in range(0, len(raw), size):
        spoken.extend(narrator.feed(raw[i : i + size]))
    return spoken


def test_the_code_is_narrated_live_in_order_while_it_is_written():
    raw = json.dumps({"path": "C:/demo/demo.html", "content": DASHBOARD})
    narrator = LiveNarrator("write")
    spoken = feed_in_chunks(narrator, raw)
    assert spoken[:4] == [
        "Escribo demo.html",
        "Título: Panel de Salud",
        "colores de las tarjetas de KPIs",
        "sidebar a la derecha con el menú de secciones",
    ]
    assert "Armo el sidebar del lado derecho" in spoken
    assert "tres tarjetas de KPIs: pacientes, consultas y diagnósticos" in spoken
    assert spoken.count("Agrego tarjetas de indicadores") == 1  # sin repetir
    assert "Agrego un gráfico: consultas por mes" in spoken
    assert "pide los datos de pacientes a la API interna" in spoken
    assert "Pido datos a api.hospital.test" in spoken
    assert not any("sidebar {" in s for s in spoken)  # un selector CSS no es un comentario
    assert narrator.narrated


def test_it_speaks_before_the_file_is_finished():
    raw = json.dumps({"path": "demo.html", "content": DASHBOARD})
    narrator = LiveNarrator("write")
    half = raw[: len(raw) // 2]
    spoken = narrator.feed(half)
    assert "Título: Panel de Salud" in spoken
    assert not narrator.closed


def test_escapes_cut_between_fragments_are_decoded():
    raw = json.dumps({"path": "a.py", "content": "# Conecta con la base \u00f3ptima\nimport oracledb\ncon = oracledb.connect(user='u')\n"}, ensure_ascii=True)
    narrator = LiveNarrator("write")
    spoken = feed_in_chunks(narrator, raw, size=3)
    assert "conecta con la base óptima" in spoken
    assert "Conecto con la base de datos Oracle" in spoken


def test_bash_is_narrated_by_its_comments_and_secrets_are_hidden():
    command = "# Exporto el stock del día\nset API_KEY=sk_live_1234567890abcdef\npython exportar.py"
    narrator = LiveNarrator("bash")
    spoken = feed_in_chunks(narrator, json.dumps({"command": command}), size=5)
    assert spoken == ["Ejecuto un comando: exporto el stock del día"]
    assert not any("sk_live" in s for s in spoken)


def test_non_code_tools_are_not_narrated_live():
    narrator = LiveNarrator("read")
    assert not narrator.active and narrator.feed('{"path": "x.py"}') == []


# ---------------------------------------------------------------------------
# REQ-010: avance del plan de tareas
# ---------------------------------------------------------------------------
def plan(main_status, *subs):
    return [
        {
            "content": "Armar dashboard demo de salud",
            "status": main_status,
            "subtasks": [{"content": c, "status": s} for c, s in subs],
        }
    ]


def test_task_progress_is_announced_when_a_step_starts_and_ends():
    before = plan("in_progress", ("Diseñar estructura", "in_progress"), ("Generar datos ficticios", "pending"),
                  ("Crear el HTML", "pending"), ("Avisar al usuario", "pending"))
    after = plan("in_progress", ("Diseñar estructura", "completed"), ("Generar datos ficticios", "in_progress"),
                 ("Crear el HTML", "pending"), ("Avisar al usuario", "pending"))
    assert task_transitions(before, after) == ["Listo: Diseñar estructura", "Paso 2 de 4: Generar datos ficticios"]


def test_a_new_plan_announces_what_starts():
    after = plan("in_progress", ("Diseñar estructura", "in_progress"), ("Generar datos", "pending"))
    assert task_transitions([], after) == ["Empiezo: Armar dashboard demo de salud", "Paso 1 de 2: Diseñar estructura"]


def test_nothing_is_said_if_nothing_changed():
    same = plan("in_progress", ("Diseñar estructura", "in_progress"))
    assert task_transitions(same, same) == []
