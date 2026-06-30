"""Emit a .SemanticModel folder from the YAML fixture and sanity-check it."""

from __future__ import annotations

from pathlib import Path

from translator.emitters import MappingConfig, emit_tmdl, write_pbip
from translator.parsers import parse_yaml

ROOT = Path(__file__).resolve().parents[1]
YAML = ROOT / "semantic_view" / "insurance_actuarial.yaml"
MAPPING = ROOT / "config" / "mapping.yml"


def test_emit_full_pipeline(tmp_path: Path) -> None:
    view = parse_yaml(YAML)
    config = MappingConfig.load(MAPPING)
    model_dir = emit_tmdl(view, config, tmp_path)

    assert model_dir.is_dir()
    defn = model_dir / "definition"

    # Core files exist
    for required in [
        ".platform",
        "definition.pbism",
        "diagramLayout.json",
    ]:
        assert (model_dir / required).is_file(), f"missing {required}"
    for required in [
        "database.tmdl",
        "model.tmdl",
        "expressions.tmdl",
        "relationships.tmdl",
    ]:
        assert (defn / required).is_file(), f"missing definition/{required}"

    # One TMDL per logical table + derived measures table
    tables_dir = defn / "tables"
    table_files = {p.stem for p in tables_dir.glob("*.tmdl")}
    expected = {t.name for t in view.tables} | {"_Measures"}
    assert expected.issubset(table_files), f"missing tables: {expected - table_files}"

    # Spot-check Direct Lake partition for fact_policy
    fp = (tables_dir / "fact_policy.tmdl").read_text(encoding="utf-8")
    assert "mode: directLake" in fp
    assert "entityName: fact_policy" in fp
    assert "measure 'policy_count'" in fp
    assert "COUNTROWS('fact_policy')" in fp

    # Premium table: t12m → DATESINPERIOD
    pt = (tables_dir / "fact_premium_txn.tmdl").read_text(encoding="utf-8")
    assert "DATESINPERIOD" in pt, "trailing-12-month metric not rewritten"
    assert "measure 't12m_earned_premium'" in pt

    # Claim table: average_claim_size uses DIVIDE
    ct = (tables_dir / "fact_claim.tmdl").read_text(encoding="utf-8")
    assert "DIVIDE(" in ct

    # Derived measures table includes loss_ratio with DIVIDE
    dm = (tables_dir / "_Measures.tmdl").read_text(encoding="utf-8")
    assert "measure 'loss_ratio'" in dm
    assert "DIVIDE(" in dm
    assert "[total_incurred_loss]" in dm
    assert "[total_earned_premium]" in dm

    # Private fact is hidden
    cl_text = ct
    assert "incurred_loss" in cl_text
    # Relationships
    rels = (defn / "relationships.tmdl").read_text(encoding="utf-8")
    assert "fromColumn: fact_policy.POLICYHOLDER_KEY" in rels
    assert "toColumn: dim_policyholder.POLICYHOLDER_KEY" in rels

    # PBIP wrapper
    pbip_path = write_pbip(model_dir, config)
    assert pbip_path.is_file() and pbip_path.suffix == ".pbip"
    assert (model_dir.parent / f"{config.model_name}.Report").is_dir()
