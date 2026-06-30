"""Generate a Power BI `.SemanticModel` TMDL folder from a `SemanticView` IR.

POC output structure (matches what Power BI Desktop emits for PBIP projects):

    out/<model_name>.SemanticModel/
        .platform
        definition.pbism
        diagramLayout.json
        definition/
            database.tmdl
            model.tmdl
            expressions.tmdl
            cultures/en-US.tmdl
            tables/<table>.tmdl
            relationships.tmdl
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from translator.emitters.calc_materializer import build_plan, render_pyspark
from translator.emitters.dax_rewriter import rewrite_calc_column, rewrite_metric
from translator.emitters.mapping import FabricTable, MappingConfig
from translator.ir import LogicalTable, Metric, SemanticView


def _q(name: str) -> str:
    """Quote a DAX/TMDL identifier with single quotes if it contains non-word chars."""
    if name.replace("_", "").isalnum() and not name[0].isdigit():
        return f"'{name}'"
    return "'" + name.replace("'", "''") + "'"


def _indent(text: str, n: int = 1) -> str:
    pad = "\t" * n
    return "\n".join(pad + line if line else line for line in text.splitlines())


# ---------------------------------------------------------------------------
# Context passed to the rewriter
# ---------------------------------------------------------------------------

@dataclass
class _EmitContext:
    config: MappingConfig
    view: SemanticView
    fabric_tables: dict[str, FabricTable]      # logical table name -> FabricTable
    column_owner: dict[str, str]               # UPPER col name -> 'table' quoted name
    metric_owner: dict[str, str]               # "table.metric" -> "[metric]"
    table_columns: dict[str, set[str]]         # logical table name -> set of UPPER col names

    def resolve_dim(self, table_lower: str, dim_lower: str) -> str | None:
        """Resolve an SV dimension reference to its physical/calc-column name."""
        tbl = self.view.get_table(table_lower)
        if tbl is None:
            return None
        for d in list(tbl.dimensions) + list(tbl.time_dimensions):
            if d.name.lower() == dim_lower:
                if d.expr.replace("_", "").isalnum() and d.expr.isupper():
                    return d.expr
                return d.name  # calc col — physical name post-materialisation, DAX name otherwise
        return None


def _build_context(view: SemanticView, config: MappingConfig) -> _EmitContext:
    fabric_tables = {
        t.name: config.resolve(t.base_table.database, t.base_table.schema_, t.base_table.table)
        for t in view.tables
    }
    materialise = config.calc_column_strategy == "materialize"
    column_owner: dict[str, str] = {}
    table_columns: dict[str, set[str]] = {}
    for t in view.tables:
        cols: set[str] = set()
        for d in list(t.dimensions) + list(t.time_dimensions):
            if d.expr.replace("_", "").isalnum():
                column_owner.setdefault(d.expr.upper(), _q(t.name))
                cols.add(d.expr.upper())
            elif materialise and d.name.isupper():
                # Materialised calc col exposed as a physical Delta column —
                # only register when the name is already uppercase so that the
                # bare-column DAX rewriter resolves it to the same bracket.
                column_owner.setdefault(d.name, _q(t.name))
                cols.add(d.name)
        for f in t.facts:
            if f.expr.replace("_", "").isalnum():
                column_owner.setdefault(f.expr.upper(), _q(t.name))
                cols.add(f.expr.upper())
            elif materialise and f.name.isupper():
                column_owner.setdefault(f.name, _q(t.name))
                cols.add(f.name)
        table_columns[t.name] = cols

    metric_owner: dict[str, str] = {}
    for t in view.tables:
        for m in t.metrics:
            metric_owner[f"{t.name.lower()}.{m.name.lower()}"] = f"[{m.name}]"
    for m in view.metrics:
        metric_owner[m.name.lower()] = f"[{m.name}]"

    return _EmitContext(config, view, fabric_tables, column_owner, metric_owner, table_columns)


# ---------------------------------------------------------------------------
# File generators
# ---------------------------------------------------------------------------

def _platform_json(model_name: str) -> str:
    return json.dumps(
        {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
            "metadata": {"type": "SemanticModel", "displayName": model_name},
            "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"},
        },
        indent=2,
    )


def _pbism_json() -> str:
    return json.dumps(
        {
            "version": "4.0",
            "settings": {},
        },
        indent=2,
    )


def _diagram_layout() -> str:
    return json.dumps({"version": "1.1.0", "diagrams": []}, indent=2)


def _database_tmdl(config: MappingConfig) -> str:
    return f"database\n\tcompatibilityLevel: {config.compatibility_level}\n"


def _expressions_tmdl(config: MappingConfig) -> str:
    """Shared M expression used by every Direct Lake partition.

    * ``materialize`` strategy → use the SQL endpoint (DL/SQL). All calc
      columns are physical in Delta, so the model has none and the proven
      DL/SQL deploy path works.
    * ``dax`` strategy → use the OneLake connector (DL/OL), required for
      in-model calculated columns.
    """
    workspace = config.workspace_name or "<workspace>"
    if config.calc_column_strategy == "dax":
        onelake_url = (
            f"https://onelake.dfs.fabric.microsoft.com/{config.workspace_id}/{config.lakehouse_id}"
        )
        source_line = (
            f"\t\t\tSource = AzureStorage.DataLake(\"{onelake_url}\", [HierarchicalNavigation=true])"
        )
    else:
        source_line = (
            f"\t\t\tSource = Sql.Database(\"{config.sql_endpoint}\", \"{config.lakehouse_name}\")"
        )
    return (
        "expression DatabaseQuery =\n"
        "\t\tlet\n"
        f"{source_line}\n"
        "\t\tin\n"
        "\t\t\tSource\n"
        "\tlineageTag: " + "00000000-0000-0000-0000-000000000001" + "\n"
        "\tkind: m\n"
        f"\tannotation PBI_NavigationStepName = Navigation\n"
        f"\tannotation PBI_ResultType = Table\n"
        f"\tannotation Workspace = {workspace}\n"
    )


def _culture_tmdl(culture: str) -> str:
    return f"cultureInfo {culture}\n"


def _model_tmdl(config: MappingConfig, ctx: _EmitContext) -> str:
    lines = [
        f"model Model",
        f"\tculture: {config.culture}",
        "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
        f"\tsourceQueryCulture: {config.culture}",
        "\tdataAccessOptions",
        "\t\tlegacyRedirects",
        "\t\treturnErrorValuesAsNull",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Table TMDL
# ---------------------------------------------------------------------------

_DAX_TYPE_MAP = {
    "NUMBER": "int64",
    "NUMERIC": "decimal",
    "INT": "int64",
    "INTEGER": "int64",
    "BIGINT": "int64",
    "FLOAT": "double",
    "DOUBLE": "double",
    "DECIMAL": "decimal",
    "BOOLEAN": "boolean",
    "BOOL": "boolean",
    "DATE": "dateTime",
    "DATETIME": "dateTime",
    "TIMESTAMP": "dateTime",
    "VARCHAR": "string",
    "STRING": "string",
    "TEXT": "string",
    "CHAR": "string",
}


def _map_data_type(raw: str | None) -> str:
    if not raw:
        return "string"
    return _DAX_TYPE_MAP.get(raw.upper(), "string")


def _doc_lines(description: str | None, indent: str) -> list[str]:
    """Return TMDL ``/// description`` lines (one per source line) at the given indent."""
    if not description:
        return []
    return [f"{indent}/// {line}" for line in description.splitlines() or [description]]


def _column_block(name: str, source: str, data_type: str, description: str | None, hidden: bool) -> str:
    lines = _doc_lines(description, "\t")
    lines.append(f"\tcolumn {name}")
    lines.append(f"\t\tdataType: {data_type}")
    if hidden:
        lines.append("\t\tisHidden")
    lines.append(f"\t\tsourceColumn: {source}")
    lines.append(f"\t\tsummarizeBy: none")
    return "\n".join(lines) + "\n"


def _calc_column_block(name: str, dax_expr: str, data_type: str, description: str | None, hidden: bool) -> str:
    """Emit a TMDL DAX calculated column (Direct Lake on OneLake supports these)."""
    flat = " ".join(dax_expr.split())
    lines = _doc_lines(description, "\t")
    lines.append(f"\tcolumn '{name}' = {flat}")
    lines.append(f"\t\tdataType: {data_type}")
    if hidden:
        lines.append("\t\tisHidden")
    lines.append(f"\t\tsummarizeBy: none")
    return "\n".join(lines) + "\n"


def _measure_block(metric: Metric, dax_expr: str, hidden: bool, ctx: _EmitContext | None = None) -> str:
    # Wrap semi-additive metrics (Snowflake `NON ADDITIVE BY (<dim> DESC ...)`)
    # in the standard DAX LASTNONBLANK / FIRSTNONBLANK pattern so the value at
    # the latest (or earliest) snapshot in filter context is returned instead
    # of summing across snapshots.
    if metric.non_additive_dimensions:
        nad = metric.non_additive_dimensions[0]
        # Resolve `<table>.<dim_name>` to the physical column the TMDL table
        # actually exposes (the dim's `expr` when it's a bare column ref).
        phys_col = nad.dimension
        if ctx is not None:
            tbl = ctx.view.get_table(nad.table)
            if tbl is not None:
                for d in list(tbl.dimensions) + list(tbl.time_dimensions):
                    if d.name.lower() == nad.dimension.lower():
                        if d.expr.replace("_", "").isalnum() and d.expr.isupper():
                            phys_col = d.expr
                        else:
                            phys_col = d.name  # calc column name we emitted
                        break
        date_ref = f"'{nad.table}'[{phys_col}]"
        anchor = "LASTNONBLANK" if nad.sort_direction.value == "descending" else "FIRSTNONBLANK"
        dax_expr = f"CALCULATE({dax_expr}, {anchor}({date_ref}, 1))"
    # TMDL is fussy about multi-line property values; flatten the DAX
    # expression to a single line so the parser treats it as one value.
    flat_expr = " ".join(dax_expr.split())
    lines = _doc_lines(metric.description, "\t")
    lines.append(f"\tmeasure '{metric.name}' = {flat_expr}")
    if hidden:
        lines.append("\t\tisHidden")
    if metric.synonyms:
        syn = ", ".join(f'"{_escape(s)}"' for s in metric.synonyms)
        lines.append(f"\t\tannotation Synonyms = {syn}")
    if metric.non_additive_dimensions:
        nad_ann = "; ".join(
            f"{n.table}.{n.dimension} {n.sort_direction.value}"
            + (f" NULLS {n.null_order.value.upper()}" if n.null_order else "")
            for n in metric.non_additive_dimensions
        )
        lines.append(f"\t\tannotation NonAdditiveBy = \"{_escape(nad_ann)}\"")
    return "\n".join(lines) + "\n"


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\"", "\\\"")


def _table_tmdl(t: LogicalTable, ctx: _EmitContext) -> str:
    fab = ctx.fabric_tables[t.name]
    out: list[str] = []
    out.extend(_doc_lines(t.description, ""))
    out.append(f"table {t.name}")
    out.append("")

    # Columns: from dims + time_dims + facts (skip metric defs)
    seen: set[str] = set()
    local_cols_for_calc = ctx.table_columns.get(t.name, set())
    materialise = ctx.config.calc_column_strategy == "materialize"
    for d in list(t.dimensions) + list(t.time_dimensions):
        if d.expr.replace("_", "").isalnum() and d.expr.isupper():
            if d.expr in seen:
                continue
            seen.add(d.expr)
            # Use the source name as the TMDL column name so it matches
            # relationships (`fromColumn`/`toColumn`) and rewritten DAX refs.
            out.append(_column_block(d.expr, d.expr, _map_data_type(d.data_type), d.description, hidden=False))
        else:
            # Calculated dimension. Skip pure constants (e.g. `AS 1` helpers
            # used in Snowflake for COUNT DISTINCT patterns).
            if d.name in seen:
                continue
            seen.add(d.name)
            calc = rewrite_calc_column(d.expr, t.name, local_cols_for_calc)
            if calc.expression is None:
                continue
            dtype = _map_data_type(d.data_type) if d.data_type else calc.data_type
            if materialise:
                # Pre-computed in the lakehouse — bind as a plain physical column.
                out.append(_column_block(d.name, d.name, dtype, d.description, hidden=False))
            else:
                out.append(_calc_column_block(d.name, calc.expression, dtype, d.description, hidden=False))

    for f in t.facts:
        from translator.ir import AccessModifier  # local import to avoid cycle warnings
        hidden = f.access_modifier == AccessModifier.PRIVATE
        if f.expr.replace("_", "").isalnum() and f.expr.isupper():
            if f.expr in seen:
                continue
            seen.add(f.expr)
            # Facts are numeric by default (they're aggregated); fall back to
            # double when source type is unknown rather than string.
            fact_dtype = _map_data_type(f.data_type) if f.data_type else "double"
            out.append(_column_block(f.expr, f.expr, fact_dtype, f.description, hidden=hidden))
        else:
            # Calculated fact.
            if f.name in seen:
                continue
            seen.add(f.name)
            calc = rewrite_calc_column(f.expr, t.name, local_cols_for_calc)
            if calc.expression is None:
                continue
            dtype = _map_data_type(f.data_type) if f.data_type else calc.data_type
            if materialise:
                out.append(_column_block(f.name, f.name, dtype, f.description, hidden=hidden))
            else:
                out.append(_calc_column_block(f.name, calc.expression, dtype, f.description, hidden=hidden))

    # Auto-emit primary-key columns (needed for measures like COUNT(PK)
    # and for relationships that reference the PK on the other side).
    for pk in t.primary_key:
        if pk not in seen:
            seen.add(pk)
            out.append(_column_block(pk, pk, "string", None, hidden=True))

    # Auto-emit FK columns referenced by relationships but not declared as
    # dimensions/facts (otherwise TMDL fails to resolve fromColumn/toColumn).
    for r in ctx.view.relationships:
        for rc in r.relationship_columns:
            if r.left_table == t.name and rc.left_column not in seen:
                seen.add(rc.left_column)
                out.append(_column_block(rc.left_column, rc.left_column, "string", None, hidden=True))
            if r.right_table == t.name and rc.right_column not in seen:
                seen.add(rc.right_column)
                out.append(_column_block(rc.right_column, rc.right_column, "string", None, hidden=True))

    # Metrics → measures
    owning = _q(t.name)
    local_cols = ctx.table_columns.get(t.name, set())
    for m in t.metrics:
        from translator.ir import AccessModifier
        result = rewrite_metric(m.expr, owning, ctx.column_owner, ctx.metric_owner, local_cols, dim_resolver=ctx.resolve_dim)
        out.append(_measure_block(m, result.expression, hidden=m.access_modifier == AccessModifier.PRIVATE, ctx=ctx))

    # Partition (Direct Lake / Import)
    storage = fab.storage_mode
    if storage == "directlake":
        out.append(
            f"\tpartition '{t.name}-DirectLake' = entity\n"
            f"\t\tmode: directLake\n"
            f"\t\tsource\n"
            f"\t\t\texpressionSource: DatabaseQuery\n"
            f"\t\t\tschemaName: {fab.schema}\n"
            f"\t\t\tentityName: {fab.table}\n"
        )
    else:
        out.append(
            f"\tpartition '{t.name}' = m\n"
            f"\t\tmode: import\n"
            f"\t\tsource =\n"
            f"\t\t\tlet\n"
            f"\t\t\t\tSource = DatabaseQuery,\n"
            f"\t\t\t\tTable = Source{{[Schema=\"{fab.schema}\",Item=\"{fab.table}\"]}}[Data]\n"
            f"\t\t\tin\n"
            f"\t\t\t\tTable\n"
        )
    return "\n".join(out) + "\n"


def _holding_table_tmdl(t_name: str, measures: list[Metric], ctx: _EmitContext) -> str:
    """Disconnected table to hold view-level (derived) measures."""
    out = ["/// Holds view-level derived measures.", f"table {t_name}", ""]
    for m in measures:
        result = rewrite_metric(m.expr, None, ctx.column_owner, ctx.metric_owner, None, dim_resolver=ctx.resolve_dim)
        out.append(_measure_block(m, result.expression, hidden=False, ctx=ctx))
    # One placeholder column to make it a valid table
    out.append("\tcolumn '_' = BLANK()\n\t\tdataType: string\n\t\tisHidden\n")
    out.append(
        f"\tpartition '_{t_name}' = calculated\n"
        f"\t\tmode: import\n"
        f"\t\tsource = {{ BLANK() }}\n"
    )
    return "\n".join(out) + "\n"


def _relationships_tmdl(view: SemanticView) -> str:
    """Emit relationships, marking those that would create ambiguous paths as
    `isActive: false` so Power BI can load the model.

    Power BI requires the relationship graph to be a forest (no cycles / no
    duplicate paths between two tables). When the source semantic view points
    multiple facts at the same shared dimension (e.g. `dim_date` used as
    effective date, transaction date, loss date, reserve date), or contains
    fact-to-fact relationships in addition to direct dim links, the union of
    all relationships induces cycles. We greedily keep the first relationship
    that connects two previously-unconnected components and mark every
    subsequent edge between them inactive; consumers can re-activate them at
    query time via `USERELATIONSHIP`.
    """
    lines: list[str] = []
    # Union-find over logical table names.
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> bool:
        ra, rb = find(a), find(b)
        if ra == rb:
            return False
        parent[ra] = rb
        return True

    for r in view.relationships:
        for i, rc in enumerate(r.relationship_columns):
            rid = r.name if len(r.relationship_columns) == 1 else f"{r.name}_{i}"
            is_active = union(r.left_table, r.right_table)
            block = (
                f"relationship {rid}\n"
                f"\tfromColumn: {r.left_table}.{rc.left_column}\n"
                f"\ttoColumn: {r.right_table}.{rc.right_column}\n"
            )
            if not is_active:
                block += "\tisActive: false\n"
            lines.append(block)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def emit(view: SemanticView, config: MappingConfig, out_dir: str | Path) -> Path:
    """Write the `.SemanticModel` folder. Returns the folder Path."""
    ctx = _build_context(view, config)

    root = Path(out_dir) / f"{config.model_name}.SemanticModel"
    defn = root / "definition"
    (defn / "tables").mkdir(parents=True, exist_ok=True)
    (defn / "cultures").mkdir(parents=True, exist_ok=True)

    (root / ".platform").write_text(_platform_json(config.model_name), encoding="utf-8")
    (root / "definition.pbism").write_text(_pbism_json(), encoding="utf-8")
    (root / "diagramLayout.json").write_text(_diagram_layout(), encoding="utf-8")

    (defn / "database.tmdl").write_text(_database_tmdl(config), encoding="utf-8")
    (defn / "model.tmdl").write_text(_model_tmdl(config, ctx), encoding="utf-8")
    (defn / "expressions.tmdl").write_text(_expressions_tmdl(config), encoding="utf-8")
    (defn / "cultures" / f"{config.culture}.tmdl").write_text(_culture_tmdl(config.culture), encoding="utf-8")

    for t in view.tables:
        (defn / "tables" / f"{t.name}.tmdl").write_text(_table_tmdl(t, ctx), encoding="utf-8")

    if view.metrics:
        holding_name = "_Measures"
        (defn / "tables" / f"{holding_name}.tmdl").write_text(
            _holding_table_tmdl(holding_name, view.metrics, ctx), encoding="utf-8"
        )

    (defn / "relationships.tmdl").write_text(_relationships_tmdl(view), encoding="utf-8")

    # When using the materialise strategy, also emit the PySpark script that
    # adds the calc columns to the lakehouse Delta tables. Run it once in a
    # Fabric notebook attached to the target lakehouse before deploying.
    if config.calc_column_strategy == "materialize":
        plan = build_plan(view, config)
        if plan:
            script_path = Path(out_dir) / f"{config.model_name}.materialize.py"
            script_path.write_text(render_pyspark(plan, config), encoding="utf-8")

    return root
