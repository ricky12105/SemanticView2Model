"""Render drift reports and change summaries as reviewable Markdown.

Produces the ``.md`` artifacts the team uses to confirm there is no unintended
drift between the Snowflake semantic view and the Fabric semantic model.
"""

from __future__ import annotations

from datetime import datetime, timezone

from translator.diff import CategoryDrift, DriftReport


def _fence(expr: str) -> str:
    expr = expr.strip() or "(empty)"
    return f"`{expr}`" if "`" not in expr else expr


def _category_section(cat: CategoryDrift, left_label: str, right_label: str) -> list[str]:
    lines = [f"### {cat.name}", ""]
    if cat.empty():
        lines += ["_No changes._", ""]
        return lines
    if cat.added:
        lines.append(f"**Added (in {right_label}, missing in {left_label}):**")
        lines += [f"- `{a}`" for a in cat.added] + [""]
    if cat.removed:
        lines.append(f"**Removed (in {left_label}, missing in {right_label}):**")
        lines += [f"- `{r}`" for r in cat.removed] + [""]
    if cat.changed:
        lines.append("**Changed:**")
        lines.append("")
        lines.append(f"| Entity | Field | {left_label} | {right_label} |")
        lines.append("| --- | --- | --- | --- |")
        for ch in cat.changed:
            lines.append(f"| `{ch.entity}` | {ch.field} | {_fence(ch.left)} | {_fence(ch.right)} |")
        lines.append("")
    return lines


def render_drift_markdown(report: DriftReport) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    status = "⚠️ Drift detected" if report.has_drift() else "✅ In sync — no drift detected"
    lines = [
        "# Semantic Drift Report",
        "",
        f"- **Generated:** {ts}",
        f"- **{report.left_label} view:** `{report.left_name}`",
        f"- **{report.right_label} model:** `{report.right_name}`",
        f"- **Status:** {status}",
        "",
        "## Summary",
        "",
        "| Category | Added | Removed | Changed |",
        "| --- | ---: | ---: | ---: |",
    ]
    for cat in report.categories:
        lines.append(f"| {cat.name} | {len(cat.added)} | {len(cat.removed)} | {len(cat.changed)} |")
    lines.append("")

    lines.append("## Details")
    lines.append("")
    for cat in report.categories:
        lines += _category_section(cat, report.left_label, report.right_label)

    if report.review_flags:
        lines += ["## Review flags", ""]
        lines += ["The following expressions could not be translated automatically and need manual review:", ""]
        lines += [f"- {f}" for f in report.review_flags]
        lines.append("")

    return "\n".join(lines)


def render_change_markdown(
    title: str,
    source_desc: str,
    target_desc: str,
    report: DriftReport,
    *,
    extra_notes: list[str] | None = None,
) -> str:
    """Render a change summary for a translate / reverse / sync operation.

    Reuses the drift diff to describe exactly what the operation added, removed,
    or changed relative to the source.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# {title}",
        "",
        f"- **Generated:** {ts}",
        f"- **Source:** {source_desc}",
        f"- **Target:** {target_desc}",
        "",
    ]
    if not report.has_drift():
        lines += ["No structural changes: source and target are equivalent.", ""]
    else:
        lines += ["## Changes", ""]
        for cat in report.categories:
            lines += _category_section(cat, report.left_label, report.right_label)

    if extra_notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in extra_notes] + [""]
    if report.review_flags:
        lines += ["## Review flags", ""] + [f"- {f}" for f in report.review_flags] + [""]
    return "\n".join(lines)
