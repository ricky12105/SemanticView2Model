"""Resolve Snowflake `<db>.<schema>.<table>` references to Fabric Lakehouse tables."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class FabricTable:
    schema: str
    table: str
    storage_mode: str

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"


@dataclass
class MappingConfig:
    workspace_id: str
    workspace_name: str
    lakehouse_id: str
    lakehouse_name: str
    sql_endpoint: str
    default_storage_mode: str
    namespace_map: list[dict]
    table_overrides: list[dict]
    model_name: str
    culture: str
    compatibility_level: int
    # How to realize calculated columns: "materialize" pushes them to the
    # Lakehouse Delta tables (DL/SQL safe); "dax" emits them as in-model DAX
    # calculated columns (requires Direct Lake on OneLake).
    calc_column_strategy: str = "materialize"
    # When True, preserve the source (Snowflake DDL) casing for physical table
    # names instead of forcing lower-case. Needed for Snowflake-mirrored
    # databases in OneLake, where tables land in UPPER_CASE and Direct Lake
    # requires an exact-case match on `entityName`.
    preserve_table_case: bool = False

    @classmethod
    def load(cls, path: str | Path) -> "MappingConfig":
        data: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        fab = data.get("fabric") or {}
        model = data.get("model") or {}
        return cls(
            workspace_id=fab.get("workspace_id", ""),
            workspace_name=fab.get("workspace_name", ""),
            lakehouse_id=fab.get("lakehouse_id", ""),
            lakehouse_name=fab.get("lakehouse_name", ""),
            sql_endpoint=fab.get("sql_endpoint", ""),
            default_storage_mode=str(data.get("default_storage_mode", "directlake")).lower(),
            namespace_map=list(data.get("namespace_map") or []),
            table_overrides=list(data.get("table_overrides") or []),
            model_name=model.get("name", "SemanticModel"),
            culture=model.get("culture", "en-US"),
            compatibility_level=int(model.get("compatibility_level", 1604)),
            calc_column_strategy=str(data.get("calc_column_strategy", "materialize")).lower(),
            preserve_table_case=bool(data.get("preserve_table_case", False)),
        )

    def _cased(self, table: str) -> str:
        return table if self.preserve_table_case else table.lower()

    def resolve(self, database: str, schema: str, table: str) -> FabricTable:
        db_l, sc_l, tb_l = database.lower(), schema.lower(), table.lower()

        # Per-table overrides win first.
        for o in self.table_overrides:
            src = o.get("snowflake") or {}
            if (
                str(src.get("database", "")).lower() == db_l
                and str(src.get("schema", "")).lower() == sc_l
                and str(src.get("table", "")).lower() == tb_l
            ):
                fab_table = o.get("fabric_table")
                fab_table = fab_table if fab_table is not None else self._cased(table)
                # Look up fabric schema via namespace_map below as well
                fab_schema = self._lookup_namespace(db_l, sc_l)
                return FabricTable(
                    schema=fab_schema,
                    table=fab_table,
                    storage_mode=str(o.get("storage_mode", self.default_storage_mode)).lower(),
                )

        fab_schema = self._lookup_namespace(db_l, sc_l)
        return FabricTable(schema=fab_schema, table=self._cased(table), storage_mode=self.default_storage_mode)

    def _lookup_namespace(self, database: str, schema: str) -> str:
        for ns in self.namespace_map:
            sn = ns.get("snowflake") or {}
            if (
                str(sn.get("database", "")).lower() == database
                and str(sn.get("schema", "")).lower() == schema
            ):
                fb = ns.get("fabric") or {}
                return str(fb.get("schema", "dbo"))
        return "dbo"

    def resolve_reverse(self, fabric_schema: str, fabric_table: str):
        """Map a Fabric Lakehouse ``schema.table`` back to a Snowflake ``BaseTableRef``.

        Inverse of :meth:`resolve`. Uses ``namespace_map`` to recover the
        Snowflake database/schema and ``table_overrides`` (when a
        ``fabric_table`` rename was configured) to recover the source table
        name. Returns ``None`` when no namespace mapping matches.
        """
        from translator.ir import BaseTableRef

        fsl = fabric_schema.lower()
        ftl = fabric_table.lower()

        database = schema = None
        for ns in self.namespace_map:
            fb = ns.get("fabric") or {}
            if str(fb.get("schema", "")).lower() == fsl:
                sn = ns.get("snowflake") or {}
                database = sn.get("database")
                schema = sn.get("schema")
                break
        if database is None or schema is None:
            return None

        # Recover the source table name via any override that renamed it.
        table = fabric_table
        for o in self.table_overrides:
            src = o.get("snowflake") or {}
            override_fab = o.get("fabric_table")
            if override_fab is not None and str(override_fab).lower() == ftl:
                table = src.get("table", fabric_table)
                break
            if (
                str(src.get("database", "")).lower() == str(database).lower()
                and str(src.get("schema", "")).lower() == str(schema).lower()
                and str(src.get("table", "")).lower() == ftl
            ):
                table = src.get("table", fabric_table)
                break

        return BaseTableRef(database=database, schema=schema, table=table)
