"""Tests for the reverse pipeline: TMDL → IR → Snowflake DDL, drift, and DAX→SQL."""

from __future__ import annotations

from pathlib import Path

import pytest

from translator.diff import diff_views
from translator.emitters import MappingConfig, emit_semantic_view, emit_tmdl
from translator.emitters.dax_to_sql import dax_measure_to_sql
from translator.parsers import parse_sql, parse_tmdl

ROOT = Path(__file__).resolve().parents[1]
SQL = ROOT / "semantic_view" / "insurance_actuarial.sql"
MAPPING = ROOT / "config" / "mapping.insurance_poc.yml"


@pytest.fixture(scope="module")
def config() -> MappingConfig:
    return MappingConfig.load(MAPPING)


@pytest.fixture(scope="module")
def source_view():
    return parse_sql(SQL)


@pytest.fixture(scope="module")
def roundtrip(tmp_path_factory, source_view, config):
    out = tmp_path_factory.mktemp("rt")
    model_dir = emit_tmdl(source_view, config, out)
    return parse_tmdl(model_dir, config, name=source_view.name)


def test_tmdl_roundtrip_preserves_structure(source_view, roundtrip) -> None:
    assert len(roundtrip.tables) == len(source_view.tables)
    assert {t.name for t in roundtrip.tables} == {t.name for t in source_view.tables}
    assert len(roundtrip.metrics) == len(source_view.metrics)


def test_tmdl_roundtrip_no_table_or_relationship_drift(source_view, roundtrip) -> None:
    report = diff_views(source_view, roundtrip)
    assert report.tables.empty(), report.tables
    assert report.relationships.empty(), report.relationships


def test_reverse_emits_semantic_view_ddl(roundtrip) -> None:
    ddl = emit_semantic_view(roundtrip)
    assert ddl.lstrip().startswith("CREATE OR REPLACE SEMANTIC VIEW")
    assert "TABLES (" in ddl
    assert "RELATIONSHIPS (" in ddl
    assert "FACTS (" in ddl
    assert "DIMENSIONS (" in ddl
    assert "METRICS (" in ddl
    assert ddl.rstrip().endswith(";")


def test_diff_detects_measure_change(source_view, roundtrip) -> None:
    edited = roundtrip.model_copy(deep=True)
    # Mutate a table-scoped measure expression to simulate a Fabric edit.
    target = next(t for t in edited.tables if t.metrics)
    original = target.metrics[0].expr
    target.metrics[0].expr = original + " * 2"
    report = diff_views(source_view, edited)
    assert report.has_drift()
    assert any(target.metrics[0].name.lower() in c.entity for c in report.measures.changed)


def test_resolve_reverse_roundtrips(config, source_view) -> None:
    for t in source_view.tables:
        fab = config.resolve(t.base_table.database, t.base_table.schema_, t.base_table.table)
        back = config.resolve_reverse(fab.schema, fab.table)
        assert back is not None
        assert back.database.lower() == t.base_table.database.lower()
        assert back.schema_.lower() == t.base_table.schema_.lower()


@pytest.mark.parametrize(
    "dax,expected",
    [
        ("COUNTROWS('fact_policy')", "COUNT(*)"),
        ("SUM('fact_claim'[PAID_LOSS])", "SUM(PAID_LOSS)"),
        ("DIVIDE([a], [b])", "a / NULLIF(b, 0)"),
        ("SUMX('t', 't'[X] + 't'[Y])", "SUM(X + Y)"),
        ('[COL] IN { "OPEN", "CLOSED" }', "COL IN ('OPEN','CLOSED')"),
    ],
)
def test_dax_to_sql_subset(dax, expected) -> None:
    assert dax_measure_to_sql(dax) == expected
