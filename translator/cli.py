"""`sv2m` command-line interface.

Subcommands:
    parse      Parse a YAML or SQL semantic view and print IR as JSON.
    translate  Parse a semantic view and emit a `.SemanticModel` / `.pbip`.
    deploy     Push an emitted `.SemanticModel` to a Fabric workspace.
    roundtrip  Parse YAML and SQL fixtures and report structural diff.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _cmd_parse(args: argparse.Namespace) -> int:
    from translator.parsers import parse_sql, parse_yaml

    src = Path(args.input)
    parser = parse_sql if src.suffix.lower() == ".sql" else parse_yaml
    view = parser(src)
    sys.stdout.write(view.model_dump_json(indent=2))
    return 0


def _cmd_translate(args: argparse.Namespace) -> int:
    from translator.emitters import MappingConfig, emit_tmdl, write_pbip
    from translator.parsers import parse_sql, parse_yaml

    src = Path(args.input)
    parser = parse_sql if src.suffix.lower() == ".sql" else parse_yaml
    view = parser(src)

    config = MappingConfig.load(args.mapping)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    model_dir = emit_tmdl(view, config, out_dir)
    print(f"Wrote semantic model: {model_dir}")

    if args.pbip:
        pbip = write_pbip(model_dir, config)
        print(f"Wrote PBIP: {pbip}")
    return 0


def _cmd_deploy(args: argparse.Namespace) -> int:
    from translator.deploy import deploy_semantic_model

    workspace = args.workspace_id or os.environ.get("FABRIC_WORKSPACE_ID")
    if not workspace:
        print("ERROR: --workspace-id or FABRIC_WORKSPACE_ID required", file=sys.stderr)
        return 2

    model_dir = Path(args.model_dir)
    if not model_dir.is_dir():
        print(f"ERROR: not a directory: {model_dir}", file=sys.stderr)
        return 2

    result = deploy_semantic_model(model_dir, workspace, args.name)
    print(json.dumps(result, indent=2, default=str))
    return 0


def _cmd_roundtrip(args: argparse.Namespace) -> int:
    from translator.parsers import parse_sql, parse_yaml

    y = parse_yaml(args.yaml)
    s = parse_sql(args.sql)

    def _summary(v):
        return {
            "name": v.name,
            "tables": len(v.tables),
            "relationships": len(v.relationships),
            "table_metrics": sum(len(t.metrics) for t in v.tables),
            "derived_metrics": len(v.metrics),
            "verified_queries": len(v.verified_queries),
        }

    print(json.dumps({"yaml": _summary(y), "sql": _summary(s)}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sv2m", description="Snowflake Semantic View → Fabric Semantic Model")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_parse = sub.add_parser("parse", help="Parse a YAML or SQL semantic view")
    p_parse.add_argument("input")
    p_parse.set_defaults(func=_cmd_parse)

    p_tx = sub.add_parser("translate", help="Emit a Fabric .SemanticModel / .pbip from a semantic view")
    p_tx.add_argument("input")
    p_tx.add_argument("--mapping", default="config/mapping.yml")
    p_tx.add_argument("--out", default="out")
    p_tx.add_argument("--pbip", action="store_true", help="Also write a .pbip wrapper + thin .Report")
    p_tx.set_defaults(func=_cmd_translate)

    p_dep = sub.add_parser("deploy", help="Deploy a .SemanticModel folder to a Fabric workspace")
    p_dep.add_argument("model_dir")
    p_dep.add_argument("--workspace-id")
    p_dep.add_argument("--name", required=True, help="Display name in the Fabric workspace")
    p_dep.set_defaults(func=_cmd_deploy)

    p_rt = sub.add_parser("roundtrip", help="Compare YAML and SQL parses of the same view")
    p_rt.add_argument("--yaml", required=True)
    p_rt.add_argument("--sql", required=True)
    p_rt.set_defaults(func=_cmd_roundtrip)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
