"""Intermediate representation of a Snowflake Semantic View.

Mirrors the YAML spec (https://docs.snowflake.com/en/user-guide/views-semantic/semantic-view-yaml-spec)
and the CREATE SEMANTIC VIEW DDL grammar. Both parsers target this IR; the
emitter consumes it.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

class AccessModifier(str, Enum):
    PUBLIC = "public_access"
    PRIVATE = "private_access"


class SortDirection(str, Enum):
    ASC = "ascending"
    DESC = "descending"


class NullOrder(str, Enum):
    FIRST = "first"
    LAST = "last"


class TagRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str | None = None
    schema_: str | None = Field(default=None, alias="schema")
    tag: str


class Tag(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: TagRef
    value: str


class BaseTableRef(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    database: str
    schema_: str = Field(alias="schema")
    table: str

    def fqn(self) -> str:
        return f"{self.database}.{self.schema_}.{self.table}"


# ---------------------------------------------------------------------------
# Column-like objects (dimensions / facts / metrics)
# ---------------------------------------------------------------------------

class _BaseEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    expr: str
    data_type: str | None = None
    tags: list[Tag] = Field(default_factory=list)


class Dimension(_BaseEntity):
    unique: bool = False
    is_enum: bool = False
    sample_values: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)  # ["filter"] supported


class TimeDimension(_BaseEntity):
    unique: bool = False
    sample_values: list[str] = Field(default_factory=list)


class Fact(_BaseEntity):
    access_modifier: AccessModifier = AccessModifier.PUBLIC
    labels: list[str] = Field(default_factory=list)


class NonAdditiveDim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str
    dimension: str
    sort_direction: SortDirection = SortDirection.ASC
    null_order: NullOrder | None = None


class Metric(_BaseEntity):
    access_modifier: AccessModifier = AccessModifier.PUBLIC
    non_additive_dimensions: list[NonAdditiveDim] = Field(default_factory=list)
    using_relationships: list[str] = Field(default_factory=list)
    # data_type is not used by metrics; we still allow it on the parent but ignore
    data_type: str | None = None


class Filter(BaseModel):
    """Standalone filter expression (entity-level filters via labels=[filter] preferred)."""
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    expr: str


# ---------------------------------------------------------------------------
# Logical tables & relationships
# ---------------------------------------------------------------------------

class LogicalTable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    base_table: BaseTableRef
    primary_key: list[str] = Field(default_factory=list)
    unique_constraints: list[list[str]] = Field(default_factory=list)
    dimensions: list[Dimension] = Field(default_factory=list)
    time_dimensions: list[TimeDimension] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    tags: list[Tag] = Field(default_factory=list)


class RelationshipColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left_column: str
    right_column: str


class Relationship(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    left_table: str
    right_table: str
    relationship_columns: list[RelationshipColumn]


# ---------------------------------------------------------------------------
# View-level objects
# ---------------------------------------------------------------------------

class VerifiedQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    question: str
    sql: str
    verified_at: int | None = None
    verified_by: str | None = None
    use_as_onboarding_question: bool = False


class SemanticView(BaseModel):
    """Top-level IR object."""
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str | None = None
    tables: list[LogicalTable]
    relationships: list[Relationship] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)  # derived (view-level)
    verified_queries: list[VerifiedQuery] = Field(default_factory=list)
    tags: list[Tag] = Field(default_factory=list)
    custom_instructions: dict[str, Any] = Field(default_factory=dict)

    # ---- convenience lookups --------------------------------------------------
    def get_table(self, name: str) -> LogicalTable | None:
        for t in self.tables:
            if t.name.lower() == name.lower():
                return t
        return None

    def all_metric_names(self) -> set[str]:
        names: set[str] = {m.name for m in self.metrics}
        for t in self.tables:
            for m in t.metrics:
                names.add(f"{t.name}.{m.name}")
                names.add(m.name)
        return names
