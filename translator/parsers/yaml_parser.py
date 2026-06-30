"""Parse a Snowflake Semantic View YAML spec into the IR."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from translator.ir import (
    AccessModifier,
    BaseTableRef,
    Dimension,
    Fact,
    Filter,
    LogicalTable,
    Metric,
    NonAdditiveDim,
    NullOrder,
    Relationship,
    RelationshipColumn,
    SemanticView,
    SortDirection,
    Tag,
    TagRef,
    TimeDimension,
    VerifiedQuery,
)


def parse_yaml(source: str | Path) -> SemanticView:
    """Parse a YAML file path or YAML string into a `SemanticView` IR."""
    if isinstance(source, Path) or (isinstance(source, str) and "\n" not in source and Path(source).exists()):
        text = Path(source).read_text(encoding="utf-8")
    else:
        text = source
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("Top-level YAML must be a mapping")
    return _build_view(data)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _build_tag(raw: dict) -> Tag:
    name = raw["name"]
    return Tag(
        name=TagRef(database=name.get("database"), schema=name.get("schema"), tag=name["tag"]),
        value=str(raw["value"]),
    )


def _tags(raw_list: list[dict] | None) -> list[Tag]:
    return [_build_tag(t) for t in (raw_list or [])]


def _base_table(raw: dict) -> BaseTableRef:
    return BaseTableRef(database=raw["database"], schema=raw["schema"], table=raw["table"])


def _common_kwargs(raw: dict) -> dict[str, Any]:
    return {
        "name": raw["name"],
        "description": raw.get("description"),
        "synonyms": list(raw.get("synonyms") or []),
        "expr": str(raw["expr"]),
        "data_type": raw.get("data_type"),
        "tags": _tags(raw.get("tags")),
    }


def _build_dimension(raw: dict) -> Dimension:
    return Dimension(
        **_common_kwargs(raw),
        unique=bool(raw.get("unique", False)),
        is_enum=bool(raw.get("is_enum", False)),
        sample_values=list(raw.get("sample_values") or []),
        labels=[str(x).lower() for x in (raw.get("labels") or [])],
    )


def _build_time_dimension(raw: dict) -> TimeDimension:
    return TimeDimension(
        **_common_kwargs(raw),
        unique=bool(raw.get("unique", False)),
        sample_values=list(raw.get("sample_values") or []),
    )


def _build_fact(raw: dict) -> Fact:
    return Fact(
        **_common_kwargs(raw),
        access_modifier=AccessModifier(raw.get("access_modifier") or AccessModifier.PUBLIC.value),
        labels=[str(x).lower() for x in (raw.get("labels") or [])],
    )


def _build_non_additive(raw: dict) -> NonAdditiveDim:
    return NonAdditiveDim(
        table=raw["table"],
        dimension=raw["dimension"],
        sort_direction=SortDirection(raw.get("sort_direction") or SortDirection.ASC.value),
        null_order=NullOrder(raw["null_order"]) if raw.get("null_order") else None,
    )


def _build_metric(raw: dict) -> Metric:
    return Metric(
        **_common_kwargs(raw),
        access_modifier=AccessModifier(raw.get("access_modifier") or AccessModifier.PUBLIC.value),
        non_additive_dimensions=[_build_non_additive(d) for d in (raw.get("non_additive_dimensions") or [])],
        using_relationships=list(raw.get("using_relationships") or []),
    )


def _build_filter(raw: dict) -> Filter:
    return Filter(
        name=raw["name"],
        description=raw.get("description"),
        synonyms=list(raw.get("synonyms") or []),
        expr=str(raw["expr"]),
    )


def _build_table(raw: dict) -> LogicalTable:
    return LogicalTable(
        name=raw["name"],
        description=raw.get("description"),
        synonyms=list(raw.get("synonyms") or []),
        base_table=_base_table(raw["base_table"]),
        primary_key=list(raw.get("primary_key") or []),
        unique_constraints=[list(u) for u in (raw.get("unique_constraints") or [])],
        dimensions=[_build_dimension(d) for d in (raw.get("dimensions") or [])],
        time_dimensions=[_build_time_dimension(d) for d in (raw.get("time_dimensions") or [])],
        facts=[_build_fact(f) for f in (raw.get("facts") or [])],
        metrics=[_build_metric(m) for m in (raw.get("metrics") or [])],
        filters=[_build_filter(f) for f in (raw.get("filters") or [])],
        tags=_tags(raw.get("tags")),
    )


def _build_relationship(raw: dict) -> Relationship:
    return Relationship(
        name=raw["name"],
        left_table=raw["left_table"],
        right_table=raw["right_table"],
        relationship_columns=[
            RelationshipColumn(left_column=c["left_column"], right_column=c["right_column"])
            for c in raw["relationship_columns"]
        ],
    )


def _build_verified(raw: dict) -> VerifiedQuery:
    return VerifiedQuery(
        name=raw["name"],
        question=raw["question"],
        sql=raw["sql"],
        verified_at=raw.get("verified_at"),
        verified_by=raw.get("verified_by"),
        use_as_onboarding_question=bool(raw.get("use_as_onboarding_question", False)),
    )


def _build_view(data: dict) -> SemanticView:
    return SemanticView(
        name=data["name"],
        description=data.get("description"),
        tables=[_build_table(t) for t in data["tables"]],
        relationships=[_build_relationship(r) for r in (data.get("relationships") or [])],
        metrics=[_build_metric(m) for m in (data.get("metrics") or [])],
        verified_queries=[_build_verified(v) for v in (data.get("verified_queries") or [])],
        tags=_tags(data.get("tags")),
        custom_instructions=dict(data.get("custom_instructions") or {}),
    )
