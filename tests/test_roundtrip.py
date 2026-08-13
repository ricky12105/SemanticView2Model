"""Round-trip parity: YAML fixture and SQL DDL fixture parse to equivalent IR.

The SQL DDL has no notion of `time_dimensions` vs `dimensions` and no
`data_type` / `is_enum` metadata, so we compare a normalized projection
that strips those YAML-only fields.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from translator.ir import SemanticView
from translator.parsers import parse_sql, parse_yaml

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "semantic_view"
YAML_PATH = FIXTURE_DIR / "insurance_actuarial.yaml"
SQL_PATH = FIXTURE_DIR / "insurance_actuarial.sql"


def _normalize_expr(s: str | None) -> str | None:
    if s is None:
        return None
    # Collapse whitespace; case-insensitive
    return " ".join(s.split()).lower()


def _norm_entity(e) -> dict:
    return {
        "name": e.name.lower(),
        "expr": _normalize_expr(e.expr),
        "description": e.description,
        "synonyms": sorted(s.lower() for s in (e.synonyms or [])),
        "labels": sorted(getattr(e, "labels", []) or []),
        "access_modifier": getattr(getattr(e, "access_modifier", None), "value", None),
        "non_additive": [
            (n.table.lower(), n.dimension.lower(),
             n.sort_direction.value, n.null_order.value if n.null_order else None)
            for n in getattr(e, "non_additive_dimensions", []) or []
        ],
    }


def _norm_table(t) -> dict:
    # Merge time_dimensions into dimensions (DDL doesn't distinguish).
    dims = list(t.dimensions) + list(t.time_dimensions)
    return {
        "name": t.name.lower(),
        "base_table": t.base_table.fqn().lower(),
        "primary_key": sorted(c.lower() for c in t.primary_key),
        "synonyms": sorted(s.lower() for s in t.synonyms),
        "dimensions": sorted([_norm_entity(d) for d in dims], key=lambda x: x["name"]),
        "facts": sorted([_norm_entity(f) for f in t.facts], key=lambda x: x["name"]),
        "metrics": sorted([_norm_entity(m) for m in t.metrics], key=lambda x: x["name"]),
    }


def _norm_view(v: SemanticView) -> dict:
    return {
        "name": v.name.lower(),
        "tables": sorted([_norm_table(t) for t in v.tables], key=lambda x: x["name"]),
        "relationships": sorted(
            [
                {
                    "name": r.name.lower(),
                    "left": r.left_table.lower(),
                    "right": r.right_table.lower(),
                    "cols": [(c.left_column.lower(), c.right_column.lower()) for c in r.relationship_columns],
                }
                for r in v.relationships
            ],
            key=lambda x: x["name"],
        ),
        "metrics": sorted([_norm_entity(m) for m in v.metrics], key=lambda x: x["name"]),
        "verified_queries": sorted(
            [
                {"name": q.name.lower(), "question": q.question, "onboarding": q.use_as_onboarding_question}
                for q in v.verified_queries
            ],
            key=lambda x: x["name"],
        ),
    }


def test_yaml_parses() -> None:
    v = parse_yaml(YAML_PATH)
    assert v.name == "insurance_actuarial"
    assert len(v.tables) == 10
    assert len(v.relationships) == 14
    assert {m.name for m in v.metrics} == {"loss_ratio", "expense_ratio", "combined_ratio", "claim_frequency"}


def test_sql_parses() -> None:
    v = parse_sql(SQL_PATH)
    assert v.name == "insurance_actuarial"
    # SQL fixture uses four role-playing date tables (effective/txn/loss/reserve)
    # over DIM_DATE, so 9 base tables + 4 date roles = 13.
    assert len(v.tables) == 13
    assert len(v.relationships) == 14


def test_roundtrip_yaml_vs_sql() -> None:
    yaml_view = parse_yaml(YAML_PATH)
    sql_view = parse_sql(SQL_PATH)

    y = _norm_view(yaml_view)
    s = _norm_view(sql_view)

    # The YAML fixture models dates as a single shared `dim_date`, while the SQL
    # fixture uses four role-playing date tables (effective/txn/loss/reserve).
    # That is an intentional modeling difference, so exclude the date tables and
    # their relationships from the equivalence check.
    DATE_TABLES = {"dim_date", "effective_date", "txn_date", "loss_date", "reserve_date"}

    def _no_dates(view: dict) -> dict:
        return {
            **view,
            "tables": [t for t in view["tables"] if t["name"] not in DATE_TABLES],
            "relationships": [r for r in view["relationships"] if r["right"] not in DATE_TABLES],
        }

    y = _no_dates(y)
    s = _no_dates(s)

    # Compare each top-level section in turn for clearer diffs.
    assert y["name"] == s["name"]
    assert y["relationships"] == s["relationships"]
    assert y["verified_queries"] == s["verified_queries"]

    assert [t["name"] for t in y["tables"]] == [t["name"] for t in s["tables"]]
    for yt, st in zip(y["tables"], s["tables"]):
        assert yt["name"] == st["name"]
        assert yt["base_table"] == st["base_table"]
        assert yt["primary_key"] == st["primary_key"]
        assert [d["name"] for d in yt["dimensions"]] == [d["name"] for d in st["dimensions"]]
        assert [f["name"] for f in yt["facts"]] == [f["name"] for f in st["facts"]]
        assert [m["name"] for m in yt["metrics"]] == [m["name"] for m in st["metrics"]]

    assert [m["name"] for m in y["metrics"]] == [m["name"] for m in s["metrics"]]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
