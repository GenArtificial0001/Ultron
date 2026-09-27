"""
ODataQueryBuilder: construye de forma incremental la URL y los parametros
de una consulta OData v2 (usada por `SapConnector.fetch_generic()`),
separando la LOGICA DE ARMADO de la query (entidad + parametros de
function import + $select/$filter/paginacion) de la logica de EJECUCION
HTTP + paginacion (que sigue viviendo en `SapConnector`).

Patron de diseño: **Builder** -- antes del refactor, `fetch_generic()`
armaba la URL/params concatenando strings inline en medio del bucle de
paginacion (ver Bloque de `sap_connector.py` `fetch_generic`, seccion
"key = f'{entity_name}(...)...'" + dict `params` reconstruido en cada
iteracion). Aca se separa en un objeto dedicado con metodos encadenables
("fluent interface"), que:
    1. Es testeable de forma aislada (sin mockear sesiones HTTP ni tokens
       CSRF) -- ver tests/test_odata_query_builder.py.
    2. Deja en claro, leyendo el codigo de armado, que piezas conforman
       una query OData v2 (entidad, parametros de function import,
       $select, $filter, paginacion) sin mezclarlas con reintentos/
       parseo de respuesta.
    3. Es reusable si a futuro se necesita armar la MISMA query para
       loggear/depurar sin ejecutarla (ej. mostrar la URL final en un
       mensaje de error).

Uso basico (equivalente a como ya lo usa fetch_generic() internamente):
    builder = (
        ODataQueryBuilder(entity_name="ZCDS_INB_TU_CONS_F_03")
        .with_params({"P_keydate_strt": "datetime'2026-07-24T00:00:00'"})
        .with_select(["tu_num_ext", "qty", "uom"])
        .with_filter("EWMWarehouse eq 'FI07'")
        .with_page_size(200)
    )
    url = builder.build_url(base_url)                 # entidad + parametros + sufijo
    query_params = builder.build_query_params(skip=0, sap_client="100")
"""
from __future__ import annotations


class ODataQueryBuilder:
    """Builder fluido de una consulta OData v2. Cada metodo `with_*`
    devuelve `self` para poder encadenar llamadas."""

    def __init__(self, entity_name: str, results_suffix: str = "/Results") -> None:
        self._entity_name = entity_name
        self._results_suffix = results_suffix
        self._entity_params: dict[str, str] = {}
        self._select_fields: list[str] | None = None
        self._odata_filter: str | None = None
        self._page_size: int = 200

    def with_params(self, entity_params: dict[str, str] | None) -> ODataQueryBuilder:
        """Parametros de function import (CDS View parametrizado), ej.
        {"P_keydate_strt": "datetime'2026-07-24T00:00:00'"}. Van DENTRO de
        los parentesis de la entidad: Entidad(param1=valor1,param2=valor2)."""
        self._entity_params = dict(entity_params) if entity_params else {}
        return self

    def with_select(self, select_fields: list[str] | None) -> ODataQueryBuilder:
        """Columnas a traer ($select). None = todas las disponibles."""
        self._select_fields = select_fields
        return self

    def with_filter(self, odata_filter: str | None) -> ODataQueryBuilder:
        """Filtro OData crudo ($filter), ej.
        "EWMWarehouse eq 'FI07' and (TuStatus eq 'C' or TuStatus eq 'R')"."""
        self._odata_filter = odata_filter
        return self

    def with_page_size(self, page_size: int) -> ODataQueryBuilder:
        """Filas por pagina OData ($top)."""
        self._page_size = page_size
        return self

    @property
    def page_size(self) -> int:
        return self._page_size

    def build_url(self, base_url: str) -> str:
        """Arma la URL completa: base + entidad + parametros de function
        import (si hay) + sufijo de resultados. Ej.:
            base_url + "ZENT(P_fecha=datetime'2026-07-24T00:00:00')/Results"
        """
        if self._entity_params:
            params_str = ",".join(f"{k}={v}" for k, v in self._entity_params.items())
            key = f"{self._entity_name}({params_str}){self._results_suffix}"
        else:
            key = f"{self._entity_name}{self._results_suffix}"
        return base_url + key

    def build_query_params(self, skip: int, sap_client: str) -> dict[str, str | int]:
        """Arma el dict de query params para UNA pagina especifica (el
        `skip` varia por pagina, todo lo demas es fijo para todo el
        builder). Incluye siempre $format/$skip/$top/$inlinecount, y
        opcionalmente $select/$filter si se configuraron."""
        params: dict[str, str | int] = {
            "sap-client": sap_client,
            "$format": "json",
            "$skip": skip,
            "$top": self._page_size,
            "$inlinecount": "allpages",
        }
        if self._select_fields:
            params["$select"] = ",".join(self._select_fields)
        if self._odata_filter:
            params["$filter"] = self._odata_filter
        return params
