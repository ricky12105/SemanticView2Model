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
    from translator.parsers import parse_sql, parse_tmdl, parse_yaml

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

    if not args.no_report:
        from translator.diff import diff_views
        from translator.reports import render_change_markdown

        roundtrip = parse_tmdl(model_dir, config, name=view.name)
        report = diff_views(view, roundtrip, left_label="Snowflake", right_label="Fabric")
        md = render_change_markdown(
            "Translate Change Report (Snowflake → Fabric)",
            f"Snowflake semantic view `{view.name}` ({src})",
            f"Fabric semantic model `{config.model_name}` ({model_dir.name})",
            report,
        )
        report_path = out_dir / f"{config.model_name}.changes.md"
        report_path.write_text(md, encoding="utf-8")
        print(f"Wrote change report: {report_path}")
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


def _load_source_view(path: Path):
    from translator.parsers import parse_sql, parse_yaml

    return (parse_sql if path.suffix.lower() == ".sql" else parse_yaml)(path)


def _preserve_untranslatable_measures(source, fabric) -> list[str]:
    """Replace Fabric measures that cannot be faithfully reverse-translated to
    valid Snowflake metric SQL with the original Snowflake expression (matched
    by name). Covers `/* review: */` placeholders (unsupported DAX) and window
    functions (whose reverse uses a physical column Snowflake can't resolve in a
    metric expression). Returns the names of measures that were preserved.
    """
    import re as _re

    def _untranslatable(expr: str) -> bool:
        return "/* review:" in expr or bool(_re.search(r"\bOVER\s*\(", expr, _re.IGNORECASE))

    src: dict[str, str] = {}
    for t in source.tables:
        for m in t.metrics:
            src[m.name.lower()] = m.expr
    for m in source.metrics:
        src[m.name.lower()] = m.expr

    preserved: list[str] = []
    for t in fabric.tables:
        for m in t.metrics:
            if _untranslatable(m.expr) and m.name.lower() in src:
                m.expr = src[m.name.lower()]
                preserved.append(m.name)
    for m in fabric.metrics:
        if _untranslatable(m.expr) and m.name.lower() in src:
            m.expr = src[m.name.lower()]
            preserved.append(m.name)
    return preserved


def _cmd_pull(args: argparse.Namespace) -> int:
    from translator.deploy import get_semantic_model

    workspace = args.workspace_id or os.environ.get("FABRIC_WORKSPACE_ID")
    if not workspace:
        print("ERROR: --workspace-id or FABRIC_WORKSPACE_ID required", file=sys.stderr)
        return 2
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    root = get_semantic_model(workspace, out_dir, display_name=args.name, item_id=args.item_id)
    print(f"Pulled semantic model to: {root}")
    return 0


def _cmd_reverse(args: argparse.Namespace) -> int:
    from translator.emitters import MappingConfig, emit_semantic_view
    from translator.parsers import parse_tmdl

    config = MappingConfig.load(args.mapping)
    view = parse_tmdl(Path(args.model_dir), config, name=args.name)
    ddl = emit_semantic_view(view)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(ddl, encoding="utf-8")
    print(f"Wrote Snowflake semantic view DDL: {out_path}")

    if args.report:
        from translator.diff import diff_views
        from translator.reports import render_change_markdown

        source = _load_source_view(Path(args.source)) if args.source else view
        report = diff_views(source, view, left_label="Snowflake", right_label="Fabric")
        md = render_change_markdown(
            "Reverse Change Report (Fabric → Snowflake)",
            f"Fabric semantic model ({Path(args.model_dir).name})",
            f"Snowflake semantic view DDL ({out_path.name})",
            report,
        )
        Path(args.report).write_text(md, encoding="utf-8")
        print(f"Wrote change report: {args.report}")
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    from translator.diff import diff_views
    from translator.emitters import MappingConfig
    from translator.parsers import parse_tmdl
    from translator.reports import render_drift_markdown

    config = MappingConfig.load(args.mapping)
    source = _load_source_view(Path(args.snowflake))
    fabric = parse_tmdl(Path(args.fabric), config, name=source.name)
    report = diff_views(source, fabric, left_label="Snowflake", right_label="Fabric")

    md = render_drift_markdown(report)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"Wrote drift report: {out_path}")
    print("Drift detected." if report.has_drift() else "No drift detected.")
    return 1 if (report.has_drift() and args.fail_on_drift) else 0


def _cmd_sync(args: argparse.Namespace) -> int:
    from translator.deploy import execute_ddl
    from translator.diff import diff_views
    from translator.emitters import MappingConfig, emit_semantic_view
    from translator.parsers import parse_tmdl
    from translator.reports import render_change_markdown

    config = MappingConfig.load(args.mapping)
    source = _load_source_view(Path(args.snowflake))
    fabric = parse_tmdl(Path(args.fabric), config, name=source.name)

    report = diff_views(source, fabric, left_label="Snowflake", right_label="Fabric")

    # Measures whose DAX could not be reverse-translated round-trip to a
    # `/* review: ... */` placeholder (invalid SQL). Preserve the original
    # Snowflake expression for those so the emitted DDL stays valid/executable.
    preserved = _preserve_untranslatable_measures(source, fabric)

    ddl = emit_semantic_view(fabric)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(ddl, encoding="utf-8")
    print(f"Wrote Snowflake DDL: {out_path}")

    result = execute_ddl(ddl, dry_run=not args.execute)
    notes = [f"Executed against Snowflake: {result.executed}"] + result.messages
    if preserved:
        notes.append(
            f"Preserved {len(preserved)} untranslatable measure expression(s) from source: "
            + ", ".join(preserved)
        )
    if args.report:
        md = render_change_markdown(
            "Sync Change Report (Fabric → Snowflake)",
            f"Current Snowflake view `{source.name}` ({Path(args.snowflake).name})",
            f"Fabric semantic model ({Path(args.fabric).name})",
            report,
            extra_notes=notes,
        )
        Path(args.report).write_text(md, encoding="utf-8")
        print(f"Wrote change report: {args.report}")
    for msg in notes:
        print(msg)
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
    p_tx.add_argument("--no-report", action="store_true", help="Skip writing the <model>.changes.md review report")
    p_tx.set_defaults(func=_cmd_translate)

    p_dep = sub.add_parser("deploy", help="Deploy a .SemanticModel folder to a Fabric workspace")
    p_dep.add_argument("model_dir")
    p_dep.add_argument("--workspace-id")
    p_dep.add_argument("--name", required=True, help="Display name in the Fabric workspace")
    p_dep.set_defaults(func=_cmd_deploy)

    p_pull = sub.add_parser("pull", help="Pull a Fabric SemanticModel definition (TMDL) to a local folder")
    p_pull.add_argument("--workspace-id")
    p_pull.add_argument("--name", help="Display name of the SemanticModel in the workspace")
    p_pull.add_argument("--item-id", help="SemanticModel item id (alternative to --name)")
    p_pull.add_argument("--out", default="out_pulled")
    p_pull.set_defaults(func=_cmd_pull)

    p_rev = sub.add_parser("reverse", help="Reconstruct Snowflake SEMANTIC VIEW DDL from a Fabric .SemanticModel")
    p_rev.add_argument("model_dir")
    p_rev.add_argument("--mapping", default="config/mapping.yml")
    p_rev.add_argument("--out", default="out/reverse.sql")
    p_rev.add_argument("--name", help="Semantic view name (defaults to model folder name)")
    p_rev.add_argument("--source", help="Optional source SV (.sql/.yaml) to diff against for the report")
    p_rev.add_argument("--report", help="Write a change .md to this path")
    p_rev.set_defaults(func=_cmd_reverse)

    p_diff = sub.add_parser("diff", help="Report drift between a Snowflake SV and a Fabric SemanticModel")
    p_diff.add_argument("--snowflake", required=True, help="Source semantic view (.sql/.yaml)")
    p_diff.add_argument("--fabric", required=True, help="Fabric .SemanticModel folder")
    p_diff.add_argument("--mapping", default="config/mapping.yml")
    p_diff.add_argument("--out", default="drift.md")
    p_diff.add_argument("--fail-on-drift", action="store_true", help="Exit non-zero when drift is found")
    p_diff.set_defaults(func=_cmd_diff)

    p_sync = sub.add_parser("sync", help="Sync Fabric SemanticModel changes back to Snowflake as DDL")
    p_sync.add_argument("--snowflake", required=True, help="Current source semantic view (.sql/.yaml)")
    p_sync.add_argument("--fabric", required=True, help="Edited Fabric .SemanticModel folder")
    p_sync.add_argument("--mapping", default="config/mapping.yml")
    p_sync.add_argument("--out", default="out/sync.sql")
    p_sync.add_argument("--report", help="Write a change .md to this path")
    p_sync.add_argument("--execute", action="store_true", help="Execute the DDL against Snowflake (default: dry run)")
    p_sync.set_defaults(func=_cmd_sync)

    p_rt = sub.add_parser("roundtrip", help="Compare YAML and SQL parses of the same view")
    p_rt.add_argument("--yaml", required=True)
    p_rt.add_argument("--sql", required=True)
    p_rt.set_defaults(func=_cmd_roundtrip)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
