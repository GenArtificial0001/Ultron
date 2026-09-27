"""
Registro central de tools disponibles para el agente Vision. Ensambla un
`ToolRegistry` (app/tools/base.py) con todas las tools built-in, imitando
`packages/core/src/tool/builtins.ts` de OpenCode.
"""

from __future__ import annotations

from app.tools.base import ToolRegistry
from app.tools.bash import bash_tool
from app.tools.chart import render_chart_tool
from app.tools.dbquery import (
    bigquery_list_tables_tool,
    bigquery_query_tool,
    graph_directory_lookup_tool,
    graph_directory_manager_tool,
    graph_search_tool,
    oracle_query_tool,
    sap_query_tool,
    sharepoint_list_items_tool,
    sharepoint_list_lists_tool,
)
from app.tools.decisions import record_decision_tool
from app.tools.filesystem import edit_tool, glob_tool, grep_tool, read_tool, write_tool
from app.tools.question import question_tool
from app.tools.skill import skill_tool
from app.tools.todowrite import todowrite_tool
from app.tools.webfetch import webfetch_tool


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (
        read_tool,
        write_tool,
        edit_tool,
        glob_tool,
        grep_tool,
        bash_tool,
        webfetch_tool,
        todowrite_tool,
        question_tool,
        skill_tool,
        oracle_query_tool,
        bigquery_query_tool,
        bigquery_list_tables_tool,
        sharepoint_list_items_tool,
        sharepoint_list_lists_tool,
        sap_query_tool,
        graph_search_tool,
        graph_directory_lookup_tool,
        graph_directory_manager_tool,
        render_chart_tool,
        record_decision_tool,
    ):
        registry.register(tool)
    return registry


_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = build_registry()
    return _registry
