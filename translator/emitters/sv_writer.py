"""Emit a Snowflake ``CREATE OR REPLACE SEMANTIC VIEW`` DDL from a ``SemanticView`` IR.

This is the reverse of ``tmdl_writer.emit``: it serialises the IR back into the
Snowflake DDL grammar so that changes made in Fabric can be synced to Snowflake.
Clause order follows the spec: ``TABLES → RELATIONSHIPS → FACTS → DIMENSIONS →
METRICS`` (see ``sql_parser`` for the parse side).
"""

from __future__ import annotations

import re

from translator.ir import AccessModifier, LogicalTable, Metric, SemanticView


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _synonyms(items: list[str]) -> str:
    return "WITH SYNONYMS = (" + ", ".join(_sql_str(s) for s in items) + ")"


def _comment(text: str | None) -> str:
    if not text:
        return ""
    return "COMMENT = " + _sql_str(" ".join(text.splitlines()))


def _trailers(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def _qualify_view_name(view: SemanticView) -> str:
    if view.tables:
        base = view.tables[0].base_table
        return f"{base.database}.{base.schema_}.{view.name}"
    return view.name


def _tables_clause(view: SemanticView) -> str:
    rows: list[str] = []
    for t in view.tables:
        parts = [f"{t.name} AS {t.base_table.fqn()}"]
        if t.primary_key:
            parts.append(f"PRIMARY KEY ({', '.join(t.primary_key)})")
        for uc in t.unique_constraints:
            parts.append(f"UNIQUE ({', '.join(uc)})")
        trailer = _trailers(_synonyms(t.synonyms) if t.synonyms else "", _comment(t.description))
        row = " ".join(parts)
        if trailer:
            row += " " + trailer
        rows.append(row)
    return "  TABLES (\n" + ",\n".join(f"    {r}" for r in rows) + "\n  )"


def _relationships_clause(view: SemanticView) -> str:
    if not view.relationships:
        return ""
    rows: list[str] = []
    for r in view.relationships:
        left_cols = ", ".join(rc.left_column for rc in r.relationship_columns)
        right_cols = ", ".join(rc.right_column for rc in r.relationship_columns)
        rows.append(
            f"{r.name} AS {r.left_table}({left_cols}) REFERENCES {r.right_table}({right_cols})"
        )
    return "  RELATIONSHIPS (\n" + ",\n".join(f"    {r}" for r in rows) + "\n  )"


def _facts_clause(view: SemanticView) -> str:
    rows: list[str] = []
    for t in view.tables:
        for f in t.facts:
            prefix = "PRIVATE " if f.access_modifier == AccessModifier.PRIVATE else ""
            row = f"{prefix}{t.name}.{f.name} AS {f.expr}"
            trailer = _trailers(_synonyms(f.synonyms) if f.synonyms else "", _comment(f.description))
            if trailer:
                row += " " + trailer
            rows.append(row)
    if not rows:
        return ""
    return "  FACTS (\n" + ",\n".join(f"    {r}" for r in rows) + "\n  )"


def _dimensions_clause(view: SemanticView) -> str:
    rows: list[str] = []
    for t in view.tables:
        for d in list(t.dimensions) + list(t.time_dimensions):
            row = f"{t.name}.{d.name} AS {d.expr}"
            labels = f"LABELS = ({', '.join(l.upper() for l in d.labels)})" if getattr(d, "labels", None) else ""
            trailer = _trailers(labels, _synonyms(d.synonyms) if d.synonyms else "", _comment(d.description))
            if trailer:
                row += " " + trailer
            rows.append(row)
    if not rows:
        return ""
    return "  DIMENSIONS (\n" + ",\n".join(f"    {r}" for r in rows) + "\n  )"


def _metric_row(qualified: str, m: Metric, metric_owner: dict[str, str] | None = None) -> str:
    expr = _qualify_metric_refs(m.expr, metric_owner) if metric_owner else m.expr
    row = f"{qualified} AS {expr}"
    if m.non_additive_dimensions:
        nad = "; ".join(
            f"{n.table}.{n.dimension} {n.sort_direction.value.upper()[:4]}".rstrip()
            for n in m.non_additive_dimensions
        )
        row += f" NON ADDITIVE BY ({nad})"
    trailer = _trailers(_synonyms(m.synonyms) if m.synonyms else "", _comment(m.description))
    if trailer:
        row += " " + trailer
    return row


def _qualify_metric_refs(expr: str, metric_owner: dict[str, str]) -> str:
    """Prefix bare metric references with their owning table (``metric`` ->
    ``table.metric``). Snowflake requires table-qualified metric references
    inside metric expressions; the DAX->SQL reverse drops the qualifier.
    """
    def _repl(mm: re.Match[str]) -> str:
        tok = mm.group(0)
        owner = metric_owner.get(tok.lower())
        return f"{owner}.{tok}" if owner else tok

    # Identifiers not already qualified (not preceded by '.' or a word char).
    return re.sub(r"(?<![.\w])[A-Za-z_]\w*", _repl, expr)


def _metrics_clause(view: SemanticView) -> str:
    metric_owner = {
        m.name.lower(): t.name for t in view.tables for m in t.metrics
    }
    rows: list[str] = []
    for t in view.tables:
        for m in t.metrics:
            rows.append(_metric_row(f"{t.name}.{m.name}", m, metric_owner))
    for m in view.metrics:
        rows.append(_metric_row(m.name, m, metric_owner))
    if not rows:
        return ""
    return "  METRICS (\n" + ",\n".join(f"    {r}" for r in rows) + "\n  )"


def emit_semantic_view(view: SemanticView, *, or_replace: bool = True) -> str:
    """Serialise ``view`` into a Snowflake CREATE (OR REPLACE) SEMANTIC VIEW string."""
    head = "CREATE OR REPLACE" if or_replace else "CREATE"
    clauses = [
        f"{head} SEMANTIC VIEW {_qualify_view_name(view)}",
        _tables_clause(view),
        _relationships_clause(view),
        _facts_clause(view),
        _dimensions_clause(view),
        _metrics_clause(view),
    ]
    body = "\n\n".join(c for c in clauses if c)
    return body + "\n;\n"
