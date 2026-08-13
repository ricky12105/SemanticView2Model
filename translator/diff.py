"""Structural diff between two ``SemanticView`` IRs for drift detection.

Compares a "left" and "right" view (typically the Snowflake semantic view and
the reverse-parsed Fabric semantic model) and reports what was added, removed,
or changed across tables, columns, relationships, and measures. The result
feeds ``translator.reports.markdown`` to produce a reviewable drift report so
teams can confirm Snowflake and Fabric stay in sync.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from translator.ir import Dimension, Fact, LogicalTable, Metric, SemanticView


def _norm(s: str | None) -> str:
    return " ".join((s or "").split()).lower()


def _norm_expr(s: str | None) -> str:
    """Normalise an expression for equality: collapse whitespace, lowercase,
    and drop ``<table>.`` qualifier prefixes so a cross-metric reference such as
    ``fact_claim.claim_count`` compares equal to the bare ``claim_count``.
    """
    text = _norm(s)
    return re.sub(r"\b[a-z_]\w*\.", "", text)


def _norm_syn(items: list[str] | None) -> list[str]:
    return sorted(_norm(x) for x in (items or []))


@dataclass
class Change:
    """A single field-level difference for an entity present on both sides."""

    entity: str
    field: str
    left: str
    right: str


@dataclass
class CategoryDrift:
    name: str
    added: list[str] = field(default_factory=list)      # present on right, missing on left
    removed: list[str] = field(default_factory=list)     # present on left, missing on right
    changed: list[Change] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


@dataclass
class DriftReport:
    left_name: str
    right_name: str
    left_label: str
    right_label: str
    tables: CategoryDrift
    columns: CategoryDrift
    measures: CategoryDrift
    relationships: CategoryDrift
    review_flags: list[str] = field(default_factory=list)

    @property
    def categories(self) -> list[CategoryDrift]:
        return [self.tables, self.columns, self.measures, self.relationships]

    def has_drift(self) -> bool:
        return any(not c.empty() for c in self.categories)


def _columns(t: LogicalTable) -> dict[str, Dimension | Fact]:
    cols: dict[str, Dimension | Fact] = {}
    for c in list(t.dimensions) + list(t.time_dimensions) + list(t.facts):
        cols[c.name.lower()] = c
    return cols


def _col_kind(c: Dimension | Fact) -> str:
    return "fact" if isinstance(c, Fact) else "dimension"


def _physical_col(view: SemanticView, table_name: str, col: str) -> str:
    """Resolve a logical column name to its physical source column.

    The emitter renames a relationship's target column to the declared logical
    dimension name (e.g. role-playing ``DATE_KEY`` → ``effective_date_key``),
    so we map back to the physical column on both sides before comparing.
    """
    t = view.get_table(table_name)
    if t is None:
        return col.lower()
    for c in list(t.dimensions) + list(t.time_dimensions) + list(t.facts):
        if c.name.lower() == col.lower():
            expr = (c.expr or "").strip()
            if re.fullmatch(r"[A-Za-z_]\w*", expr):
                return expr.lower()
            break
    return col.lower()


def _rel_keys(view: SemanticView) -> dict[str, str]:
    keys: dict[str, str] = {}
    for r in view.relationships:
        for rc in r.relationship_columns:
            lc = _physical_col(view, r.left_table, rc.left_column)
            rcol = _physical_col(view, r.right_table, rc.right_column)
            k = f"{r.left_table.lower()}.{lc} -> {r.right_table.lower()}.{rcol}"
            keys[k] = r.name
    return keys


def _metrics(view: SemanticView) -> dict[str, Metric]:
    out: dict[str, Metric] = {}
    for t in view.tables:
        for m in t.metrics:
            out[f"{t.name.lower()}.{m.name.lower()}"] = m
    for m in view.metrics:
        out[m.name.lower()] = m
    return out


def diff_views(
    left: SemanticView,
    right: SemanticView,
    *,
    left_label: str = "Snowflake",
    right_label: str = "Fabric",
) -> DriftReport:
    """Compute drift where ``left`` is the reference and ``right`` the compared view."""
    tables = CategoryDrift("Tables")
    columns = CategoryDrift("Columns")
    measures = CategoryDrift("Measures")
    relationships = CategoryDrift("Relationships")
    review_flags: list[str] = []

    lt = {t.name.lower(): t for t in left.tables}
    rt = {t.name.lower(): t for t in right.tables}

    tables.added = sorted(rt.keys() - lt.keys())
    tables.removed = sorted(lt.keys() - rt.keys())

    for name in sorted(lt.keys() & rt.keys()):
        a, b = lt[name], rt[name]
        if _norm(a.base_table.fqn()) != _norm(b.base_table.fqn()):
            tables.changed.append(Change(name, "base_table", a.base_table.fqn(), b.base_table.fqn()))
        if _norm(a.description) != _norm(b.description):
            tables.changed.append(Change(name, "description", a.description or "", b.description or ""))
        if _norm_syn(a.synonyms) != _norm_syn(b.synonyms):
            tables.changed.append(Change(name, "synonyms", ", ".join(a.synonyms), ", ".join(b.synonyms)))

        # Columns within this table.
        ca, cb = _columns(a), _columns(b)
        columns.added += [f"{name}.{c}" for c in sorted(cb.keys() - ca.keys())]
        columns.removed += [f"{name}.{c}" for c in sorted(ca.keys() - cb.keys())]
        for col in sorted(ca.keys() & cb.keys()):
            x, y = ca[col], cb[col]
            ent = f"{name}.{col}"
            if _norm_expr(x.expr) != _norm_expr(y.expr):
                columns.changed.append(Change(ent, "expr", x.expr, y.expr))
            if _col_kind(x) != _col_kind(y):
                columns.changed.append(Change(ent, "kind", _col_kind(x), _col_kind(y)))
            if _norm(x.description) != _norm(y.description):
                columns.changed.append(Change(ent, "description", x.description or "", y.description or ""))
            if _norm_syn(x.synonyms) != _norm_syn(y.synonyms):
                columns.changed.append(Change(ent, "synonyms", ", ".join(x.synonyms), ", ".join(y.synonyms)))

    # Measures (table-scoped + view-level).
    ma, mb = _metrics(left), _metrics(right)
    measures.added = sorted(mb.keys() - ma.keys())
    measures.removed = sorted(ma.keys() - mb.keys())
    for key in sorted(ma.keys() & mb.keys()):
        x, y = ma[key], mb[key]
        if _norm_expr(x.expr) != _norm_expr(y.expr):
            measures.changed.append(Change(key, "expr", x.expr, y.expr))
        if _norm(x.description) != _norm(y.description):
            measures.changed.append(Change(key, "description", x.description or "", y.description or ""))

    # Relationships (identity = the from/to column path).
    ra, rb = _rel_keys(left), _rel_keys(right)
    relationships.added = sorted(rb.keys() - ra.keys())
    relationships.removed = sorted(ra.keys() - rb.keys())

    for view, side in ((left, left_label), (right, right_label)):
        for m in _metrics(view).values():
            if "/* review:" in m.expr:
                review_flags.append(f"{side} measure `{m.name}`: {m.expr}")
        for t in view.tables:
            for c in list(t.dimensions) + list(t.facts):
                if "/* review:" in c.expr:
                    review_flags.append(f"{side} column `{t.name}.{c.name}`: {c.expr}")

    return DriftReport(
        left_name=left.name,
        right_name=right.name,
        left_label=left_label,
        right_label=right_label,
        tables=tables,
        columns=columns,
        measures=measures,
        relationships=relationships,
        review_flags=review_flags,
    )
