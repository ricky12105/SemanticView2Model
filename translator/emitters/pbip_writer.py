"""Wrap the emitted `.SemanticModel` folder in a `.pbip` Power BI project.

PBIP layout:
    out/
        <name>.pbip
        <name>.SemanticModel/       (produced by tmdl_writer.emit)
        <name>.Report/              (thin report stub written here)
"""

from __future__ import annotations

import json
from pathlib import Path

from translator.emitters.mapping import MappingConfig


def write_pbip(model_dir: Path, config: MappingConfig) -> Path:
    out_root = model_dir.parent
    name = config.model_name

    # 1. .pbip launcher
    pbip = {
        "version": "1.0",
        "artifacts": [
            {"report": {"path": f"{name}.Report"}},
        ],
        "settings": {"enableAutoRecovery": True},
    }
    pbip_path = out_root / f"{name}.pbip"
    pbip_path.write_text(json.dumps(pbip, indent=2), encoding="utf-8")

    # 2. Thin .Report folder pointing at the semantic model
    report_dir = out_root / f"{name}.Report"
    defn = report_dir / "definition"
    defn.mkdir(parents=True, exist_ok=True)

    (report_dir / ".platform").write_text(
        json.dumps(
            {
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
                "metadata": {"type": "Report", "displayName": name},
                "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000002"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (report_dir / "definition.pbir").write_text(
        json.dumps(
            {
                "version": "1.0",
                "datasetReference": {
                    "byPath": {"path": f"../{name}.SemanticModel"},
                    "byConnection": None,
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (defn / "report.json").write_text(json.dumps({"resourcePackages": []}, indent=2), encoding="utf-8")

    return pbip_path
