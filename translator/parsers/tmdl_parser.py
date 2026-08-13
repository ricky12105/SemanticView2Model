"""Parse a Power BI ``.SemanticModel`` TMDL folder back into a ``SemanticView`` IR.

This is the reverse of ``translator/emitters/tmdl_writer.py``. It lets us take a
Fabric semantic model that a modeler has edited and reconstruct the equivalent
Snowflake semantic-view IR, so changes can be synced back to Snowflake and drift
between the two platforms can be detected.

Reconstruction rules (inverse of the emitter):
  * ``tables/<name>.tmdl``            → one ``LogicalTable`` (name = ``table`` id).
  * ``/// text`` above an entity      → its ``description``.
  * ``column X`` w/ ``sourceColumn:`` → ``Fact`` when numeric, else ``Dimension``
    (``expr`` = the ``sourceColumn`` physical name).
  * ``column X = <DAX>`` (calc col)   → ``Dimension`` (or ``Fact`` when hidden);
    ``expr`` = DAX rewritten to Snowflake SQL (see ``dax_to_sql``).
  * ``measure X = <DAX>``             → ``Metric`` (``expr`` = DAX → SQL).
  * ``annotation Synonyms = ...``     → ``synonyms``.
  * ``isHidden``                      → ``PRIVATE`` access / hidden key column.
  * ``partition ... entityName/schemaName`` → base table (reverse-mapped to
    Snowflake ``db.schema.table`` via the mapping config).
  * ``relationships.tmdl``            → ``Relationship`` list.
  * ``_Measures`` table               → view-level ``SemanticView.metrics``.

Some Snowflake concepts have no TMDL representation (e.g. ``PRIMARY KEY`` is only
implied by relationship targets). Those are reconstructed best-effort and flagged
as known-lossy in the round-trip tests.
"""

from __future__ import annotations

import re
from pathlib import Path

from translator.emitters.dax_to_sql import dax_measure_to_sql
from translator.emitters.mapping import MappingConfig
from translator.ir import (
    AccessModifier,
    BaseTableRef,
    Dimension,
    Fact,
    LogicalTable,
    Metric,
    Relationship,
    RelationshipColumn,
    SemanticView,
)

_NUMERIC_TYPES = {"int64", "double", "decimal"}
_HOLDING_TABLE = "_Measures"


def _unquote(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] == "'" and name[-1] == "'":
        return name[1:-1].replace("''", "'")
    return name


def _dedent_level(line: str) -> int:
    n = 0
    for ch in line:
        if ch == "\t":
            n += 1
        else:
            break
    return n


class _RawEntity:
    """A column/measure block collected line-by-line before typing."""

    def __init__(self, kind: str, name: str, inline_expr: str | None, doc: list[str]):
        self.kind = kind                # "column" | "measure"
        self.name = name
        self.inline_expr = inline_expr  # RHS of `= ...` for calc cols / measures
        self.doc = doc
        self.data_type: str | None = None
        self.hidden = False
        self.source_column: str | None = None
        self.synonyms: list[str] = []


def _parse_annotation_synonyms(value: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', value)]


def _parse_table_file(text: str, config: MappingConfig | None) -> tuple[LogicalTable | None, list[Metric]]:
    """Parse one ``tables/*.tmdl`` file.

    Returns ``(logical_table, view_level_metrics)``. For the ``_Measures``
    holding table ``logical_table`` is ``None`` and its measures are returned as
    view-level metrics instead.
    """
    lines = text.splitlines()
    table_name: str | None = None
    table_desc: list[str] = []
    table_synonyms: list[str] = []
    pending_doc: list[str] = []
    entities: list[_RawEntity] = []
    current: _RawEntity | None = None
    schema_name: str | None = None
    entity_name: str | None = None

    def _flush() -> None:
        nonlocal current
        if current is not None:
            entities.append(current)
            current = None

    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        level = _dedent_level(raw)

        if not stripped:
            i += 1
            continue

        if stripped.startswith("///"):
            pending_doc.append(stripped[3:].strip())
            i += 1
            continue

        if level == 0 and stripped.startswith("table "):
            table_name = _unquote(stripped[len("table "):])
            table_desc = pending_doc
            pending_doc = []
            i += 1
            continue

        if level == 1 and stripped.startswith("annotation Synonyms") and current is None:
            table_synonyms = _parse_annotation_synonyms(stripped.split("=", 1)[1])
            i += 1
            continue

        if level == 1 and (stripped.startswith("column ") or stripped.startswith("measure ")):
            _flush()
            kind = "column" if stripped.startswith("column ") else "measure"
            body = stripped[len(kind) + 1:]
            if "=" in body:
                name_part, expr_part = body.split("=", 1)
                current = _RawEntity(kind, _unquote(name_part.strip()), expr_part.strip(), pending_doc)
            else:
                current = _RawEntity(kind, _unquote(body.strip()), None, pending_doc)
            pending_doc = []
            i += 1
            continue

        if level == 1 and stripped.startswith("partition"):
            _flush()
            pending_doc = []
            j = i + 1
            while j < len(lines) and _dedent_level(lines[j]) >= 2:
                s = lines[j].strip()
                if s.startswith("schemaName:"):
                    schema_name = s.split(":", 1)[1].strip()
                elif s.startswith("entityName:"):
                    entity_name = s.split(":", 1)[1].strip()
                elif s.startswith("Item="):
                    pass
                j += 1
            # Also scan m-partition Item=/Schema= form.
            block = "\n".join(lines[i:j])
            msch = re.search(r'Schema="([^"]+)"', block)
            mitm = re.search(r'Item="([^"]+)"', block)
            if msch:
                schema_name = msch.group(1)
            if mitm:
                entity_name = mitm.group(1)
            i = j
            continue

        if level >= 2 and current is not None:
            if stripped.startswith("dataType:"):
                current.data_type = stripped.split(":", 1)[1].strip()
            elif stripped == "isHidden":
                current.hidden = True
            elif stripped.startswith("sourceColumn:"):
                current.source_column = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("annotation Synonyms"):
                current.synonyms = _parse_annotation_synonyms(stripped.split("=", 1)[1])
            i += 1
            continue

        i += 1

    _flush()

    if table_name is None:
        return None, []

    if table_name == _HOLDING_TABLE:
        metrics = [
            Metric(
                name=e.name,
                description="\n".join(e.doc) or None,
                synonyms=e.synonyms,
                expr=dax_measure_to_sql(e.inline_expr or "", table_name),
            )
            for e in entities
            if e.kind == "measure"
        ]
        return None, metrics

    dimensions: list[Dimension] = []
    facts: list[Fact] = []
    metrics: list[Metric] = []
    key_columns: list[str] = []

    for e in entities:
        desc = "\n".join(e.doc) or None
        if e.kind == "measure":
            metrics.append(
                Metric(
                    name=e.name,
                    description=desc,
                    synonyms=e.synonyms,
                    expr=dax_measure_to_sql(e.inline_expr or "", table_name),
                    access_modifier=AccessModifier.PRIVATE if e.hidden else AccessModifier.PUBLIC,
                )
            )
            continue

        # Column
        is_calc = e.inline_expr is not None
        dtype = (e.data_type or "").lower()
        if is_calc:
            sql_expr = dax_measure_to_sql(e.inline_expr or "", table_name)
            # Hidden calc columns are PRIVATE facts (e.g. helper `incurred_loss`);
            # visible calc columns are dimensions regardless of numeric type.
            if e.hidden:
                facts.append(
                    Fact(
                        name=e.name,
                        description=desc,
                        synonyms=e.synonyms,
                        expr=sql_expr,
                        access_modifier=AccessModifier.PRIVATE,
                    )
                )
            else:
                dimensions.append(
                    Dimension(name=e.name, description=desc, synonyms=e.synonyms, expr=sql_expr)
                )
            continue

        source = e.source_column or e.name
        if e.hidden and dtype not in _NUMERIC_TYPES:
            # Auto-emitted PK/FK key column — reconstruct keys, not a fact/dim.
            key_columns.append(source)
            continue
        if dtype in _NUMERIC_TYPES:
            facts.append(
                Fact(
                    name=e.name,
                    description=desc,
                    synonyms=e.synonyms,
                    expr=source,
                    access_modifier=AccessModifier.PRIVATE if e.hidden else AccessModifier.PUBLIC,
                    data_type=e.data_type,
                )
            )
        else:
            dimensions.append(
                Dimension(name=e.name, description=desc, synonyms=e.synonyms, expr=source, data_type=e.data_type)
            )

    base = _reverse_base_table(schema_name, entity_name, config)
    table = LogicalTable(
        name=table_name,
        description="\n".join(table_desc) or None,
        synonyms=table_synonyms,
        base_table=base,
        dimensions=dimensions,
        facts=facts,
        metrics=metrics,
    )
    # Attach reconstructed key columns for the relationship/PK pass.
    table.__dict__["_key_columns"] = key_columns
    return table, []


def _reverse_base_table(schema_name: str | None, entity_name: str | None, config: MappingConfig | None) -> BaseTableRef:
    if config is not None and schema_name and entity_name:
        ref = config.resolve_reverse(schema_name, entity_name)
        if ref is not None:
            return ref
    return BaseTableRef(
        database="UNKNOWN",
        schema=schema_name or "UNKNOWN",
        table=entity_name or (schema_name or "UNKNOWN"),
    )


def _parse_relationships(text: str) -> list[Relationship]:
    rels: list[Relationship] = []
    name: str | None = None
    from_col: tuple[str, str] | None = None
    to_col: tuple[str, str] | None = None

    def _flush() -> None:
        nonlocal name, from_col, to_col
        if name and from_col and to_col:
            rels.append(
                Relationship(
                    name=name,
                    left_table=from_col[0],
                    right_table=to_col[0],
                    relationship_columns=[
                        RelationshipColumn(left_column=from_col[1], right_column=to_col[1])
                    ],
                )
            )
        name, from_col, to_col = None, None, None

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("relationship "):
            _flush()
            name = _unquote(stripped[len("relationship "):])
        elif stripped.startswith("fromColumn:"):
            val = stripped.split(":", 1)[1].strip()
            if "." in val:
                t, c = val.split(".", 1)
                from_col = (t.strip(), c.strip())
        elif stripped.startswith("toColumn:"):
            val = stripped.split(":", 1)[1].strip()
            if "." in val:
                t, c = val.split(".", 1)
                to_col = (t.strip(), c.strip())
    _flush()
    return rels


def _locate_definition(model_dir: Path) -> Path:
    if (model_dir / "definition").is_dir():
        return model_dir / "definition"
    if (model_dir / "tables").is_dir():
        return model_dir
    # Search one level down for a *.SemanticModel folder.
    for child in model_dir.glob("*.SemanticModel"):
        if (child / "definition").is_dir():
            return child / "definition"
    raise FileNotFoundError(f"No TMDL definition folder found under {model_dir}")


def parse_tmdl(model_dir: str | Path, config: MappingConfig | None = None, *, name: str | None = None) -> SemanticView:
    """Reconstruct a ``SemanticView`` IR from a ``.SemanticModel`` TMDL folder.

    Args:
        model_dir: Path to the ``.SemanticModel`` folder, its ``definition``
            subfolder, or a parent directory containing one.
        config: Optional mapping config used to reverse-resolve Fabric schema/
            table back to Snowflake ``db.schema.table``.
        name: Optional semantic-view name; defaults to the model folder name.
    """
    defn = _locate_definition(Path(model_dir))

    # Support both the PBIP layout (definition/tables/*.tmdl) and the flat
    # Tabular-Editor / modeling-MCP export (all *.tmdl in definition/).
    # Non-table files (model/database/expressions/relationships/culture) parse
    # to None and are skipped.
    candidates = list((defn / "tables").glob("*.tmdl")) + list(defn.glob("*.tmdl"))
    seen_paths: set[str] = set()

    tables: list[LogicalTable] = []
    view_metrics: list[Metric] = []
    for tf in sorted(candidates, key=lambda p: p.name):
        key = str(tf.resolve())
        if key in seen_paths:
            continue
        seen_paths.add(key)
        table, holding_metrics = _parse_table_file(tf.read_text(encoding="utf-8"), config)
        if table is not None:
            tables.append(table)
        view_metrics.extend(holding_metrics)

    relationships: list[Relationship] = []
    rel_file = defn / "relationships.tmdl"
    if rel_file.is_file():
        relationships = _parse_relationships(rel_file.read_text(encoding="utf-8"))

    _reconstruct_primary_keys(tables, relationships)

    view_name = name or _derive_view_name(Path(model_dir))
    return SemanticView(
        name=view_name,
        tables=tables,
        relationships=relationships,
        metrics=view_metrics,
    )


def _reconstruct_primary_keys(tables: list[LogicalTable], relationships: list[Relationship]) -> None:
    """Infer each table's primary key from relationship target (``toColumn``) columns."""
    by_name = {t.name: t for t in tables}
    for r in relationships:
        target = by_name.get(r.right_table)
        if target is None:
            continue
        for rc in r.relationship_columns:
            if rc.right_column not in target.primary_key:
                target.primary_key.append(rc.right_column)


def _derive_view_name(model_dir: Path) -> str:
    for part in [model_dir.name, *[p.name for p in model_dir.glob("*.SemanticModel")]]:
        if part.endswith(".SemanticModel"):
            return part[: -len(".SemanticModel")]
    return model_dir.name.replace(".SemanticModel", "")
