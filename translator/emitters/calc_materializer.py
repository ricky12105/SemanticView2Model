"""Generate a PySpark script that materializes Semantic View calculated columns
as physical Delta columns in the Fabric Lakehouse.

The DAX-in-model approach requires Direct Lake on OneLake, which the Fabric
DeployToFabric path does not consistently accept. The portable alternative
(per Microsoft's "Workarounds" guidance for calc cols on Direct Lake) is to
push the calculation upstream: add the column to the Delta table and backfill
it once, then expose it to the model as a plain physical column.

This module produces a single, idempotent PySpark script that:
  * For each calc column in the IR, ALTERs the target Delta table to add the
    column if missing.
  * UPDATEs every row with the rewritten Snowflake → Spark SQL expression.

Run the generated script in a Fabric notebook attached to the lakehouse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from translator.emitters.dax_rewriter import rewrite_calc_column
from translator.emitters.mapping import FabricTable, MappingConfig
from translator.ir import LogicalTable, SemanticView


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

# DAX/TMDL data type -> Spark SQL DDL type
_SPARK_TYPE = {
    "int64": "BIGINT",
    "double": "DOUBLE",
    "decimal": "DECIMAL(38,6)",
    "boolean": "BOOLEAN",
    "dateTime": "TIMESTAMP",
    "string": "STRING",
}


@dataclass(frozen=True)
class CalcMaterializationStep:
    schema: str
    table: str            # physical Delta table name
    column: str           # new column name (matches SV calc-col name)
    spark_type: str
    spark_expr: str
    description: str | None
    source_expr: str      # original Snowflake expression (for the comment)


def _iter_calc_entities(t: LogicalTable) -> Iterable:
    """Yield every dimension/fact that needs to be realised as a calc column."""
    for d in list(t.dimensions) + list(t.time_dimensions):
        if not _is_bare_column(d.expr):
            yield d
    for f in t.facts:
        if not _is_bare_column(f.expr):
            yield f


def _is_bare_column(expr: str) -> bool:
    return expr.replace("_", "").isalnum() and expr.isupper()


def build_plan(view: SemanticView, config: MappingConfig) -> list[CalcMaterializationStep]:
    plan: list[CalcMaterializationStep] = []
    for t in view.tables:
        fab = config.resolve(t.base_table.database, t.base_table.schema_, t.base_table.table)
        for ent in _iter_calc_entities(t):
            calc = rewrite_calc_column(ent.expr, t.name, set())
            if calc.expression is None:  # trivial constant — nothing to materialise
                continue
            spark_expr = _snowflake_to_spark(ent.expr)
            spark_type = _SPARK_TYPE.get(calc.data_type, "STRING")
            plan.append(
                CalcMaterializationStep(
                    schema=fab.schema,
                    table=fab.table,
                    column=ent.name,
                    spark_type=spark_type,
                    spark_expr=spark_expr,
                    description=ent.description,
                    source_expr=ent.expr.strip(),
                )
            )
    return plan


# ---------------------------------------------------------------------------
# Snowflake expression -> Spark SQL expression
# ---------------------------------------------------------------------------

_RE_CURRENT_DATE = re.compile(r"\bCURRENT_DATE\s*\(\s*\)", re.IGNORECASE)
_RE_DATEDIFF_HEAD = re.compile(r"\bDATEDIFF\s*\(", re.IGNORECASE)


def _snowflake_to_spark(expr: str) -> str:
    """Translate the subset of Snowflake SQL used in calc-column expressions
    into Spark SQL. Idempotent; passes most ANSI SQL through unchanged.
    """
    text = expr.strip()
    text = _RE_CURRENT_DATE.sub("current_date()", text)
    text = _rewrite_sf_datediff(text)
    return text


def _rewrite_sf_datediff(text: str) -> str:
    """Snowflake: DATEDIFF(<part>, <start>, <end>)
    Spark:    datediff(<end>, <start>)        for day
              months_between(<end>, <start>) cast to int for month
              floor(months_between(<end>, <start>) / 12) for year
              floor(months_between(<end>, <start>) / 3) for quarter
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        m = _RE_DATEDIFF_HEAD.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        depth = 1
        j = m.end()
        while j < len(text) and depth > 0:
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        inner = text[m.end():j]
        args, depth, last = [], 0, 0
        for k, ch in enumerate(inner):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                args.append(inner[last:k].strip())
                last = k + 1
        args.append(inner[last:].strip())
        if len(args) == 3:
            part = args[0].upper().strip("'\"")
            a, b = _rewrite_sf_datediff(args[1]), _rewrite_sf_datediff(args[2])
            if part in {"YEAR", "YEARS", "YY", "YYYY"}:
                out.append(f"CAST(FLOOR(months_between({b}, {a}) / 12) AS BIGINT)")
            elif part in {"QUARTER", "QUARTERS", "QQ"}:
                out.append(f"CAST(FLOOR(months_between({b}, {a}) / 3) AS BIGINT)")
            elif part in {"MONTH", "MONTHS", "MM"}:
                out.append(f"CAST(FLOOR(months_between({b}, {a})) AS BIGINT)")
            else:
                out.append(f"datediff({b}, {a})")
        else:
            out.append(text[m.start():j + 1])
        i = j + 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Script emission
# ---------------------------------------------------------------------------

def render_pyspark(plan: list[CalcMaterializationStep], config: MappingConfig) -> str:
    """Render an idempotent PySpark script. Run in a Fabric notebook attached
    to the target lakehouse. Each step is skipped if the column already exists.
    """
    header = [
        '"""Auto-generated by sv2m. Materialise Semantic View calculated columns',
        f'as physical Delta columns in lakehouse `{config.lakehouse_name}`.',
        "",
        "Run this notebook against the lakehouse before deploying the semantic",
        "model. Re-running is safe: existing columns are skipped, values are",
        "always recomputed so the column reflects the latest source rows.",
        '"""',
        "",
        "from pyspark.sql import SparkSession",
        "",
        "spark = SparkSession.builder.getOrCreate()",
        "",
        "STEPS = [",
    ]
    body: list[str] = []
    for s in plan:
        body.append("    {")
        body.append(f"        \"schema\": {_py(s.schema)},")
        body.append(f"        \"table\": {_py(s.table)},")
        body.append(f"        \"column\": {_py(s.column)},")
        body.append(f"        \"spark_type\": {_py(s.spark_type)},")
        body.append(f"        \"expr\": {_py(s.spark_expr)},")
        body.append(f"        \"description\": {_py(s.description or '')},")
        body.append(f"        \"source_expr\": {_py(s.source_expr)},")
        body.append("    },")
    footer = [
        "]",
        "",
        "for step in STEPS:",
        "    fq = f\"{step['schema']}.{step['table']}\"",
        "    existing = {f.name.lower() for f in spark.table(fq).schema.fields}",
        "    if step['column'].lower() not in existing:",
        "        spark.sql(",
        "            f\"ALTER TABLE {fq} ADD COLUMNS ({step['column']} {step['spark_type']})\"",
        "        )",
        "        print(f\"  + added {fq}.{step['column']} {step['spark_type']}\")",
        "    else:",
        "        print(f\"  = column exists {fq}.{step['column']}\")",
        "    spark.sql(",
        "        f\"UPDATE {fq} SET {step['column']} = {step['expr']}\"",
        "    )",
        "    print(f\"    backfilled from: {step['source_expr']}\")",
        "",
        "print(f\"\\nDone. {len(STEPS)} calc column(s) materialised.\")",
        "",
    ]
    return "\n".join(header + body + footer)


def _py(value: str) -> str:
    """Python-literal repr that is JSON-friendly."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
