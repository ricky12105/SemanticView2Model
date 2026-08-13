"""Rewrite Snowflake metric/dimension SQL expressions into DAX.

POC scope (the patterns exercised by `insurance_actuarial.yaml`):
  * COUNT(*)                                 → COUNTROWS('<table>')
  * SUM(<col_expr>)                          → SUM('<table>'[<col>]) or SUMX for derived expressions
  * NULLIF(a, 0)                             → DIVIDE wrapping (handled by ratio rewrite)
  * a / NULLIF(b, 0)                         → DIVIDE(a, b)
  * SUM(SUM(x)) OVER (ORDER BY <date> ROWS BETWEEN 11 PRECEDING AND CURRENT ROW)
                                             → CALCULATE([base], DATESINPERIOD(<date>, MAX(<date>), -12, MONTH))
  * <tbl>.<metric>                           → [<metric>]   (cross-metric reference)
  * Bare column refs become '<table>'[<col>] using the metric's owning table.

Anything not matched falls back to the original text wrapped in a DAX comment so
the model still loads — review-flagged in the report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class DaxRewriteResult:
    expression: str
    needs_review: bool
    notes: list[str]


_RE_COUNT_STAR = re.compile(r"\bCOUNT\s*\(\s*\*\s*\)", re.IGNORECASE)
_RE_SUM = re.compile(r"\bSUM\s*\(", re.IGNORECASE)
_RE_DIV0 = re.compile(r"\bDIV0\s*\(\s*(?P<num>.+?)\s*,\s*(?P<den>.+?)\s*\)", re.IGNORECASE | re.DOTALL)
_RE_NULLIF_DIV = re.compile(
    r"(?P<num>.+?)\s*/\s*NULLIF\s*\(\s*(?P<den>.+?)\s*,\s*0\s*\)",
    re.IGNORECASE | re.DOTALL,
)
_RE_OVER = re.compile(
    r"SUM\s*\(\s*SUM\s*\(\s*(?P<col>[A-Z_][A-Z0-9_]*)\s*\)\s*\)\s*OVER\s*\(\s*ORDER\s+BY\s+"
    r"(?P<dtbl>\w+)\.(?P<dcol>\w+)\s+ROWS\s+BETWEEN\s+(?P<n>\d+)\s+PRECEDING\s+AND\s+CURRENT\s+ROW\s*\)",
    re.IGNORECASE | re.DOTALL,
)
_RE_BARE_COL = re.compile(r"\b([A-Z_][A-Z0-9_]*)\b")
_RE_QUAL_METRIC = re.compile(r"\b(?P<tbl>[a-z_][a-z0-9_]*)\.(?P<met>[a-z_][a-z0-9_]*)\b")

_SQL_KEYWORDS = {
    "AND", "OR", "NOT", "IN", "NULLIF", "CASE", "WHEN", "THEN", "ELSE", "END",
    "TRUE", "FALSE", "NULL", "OVER", "ORDER", "BY", "ROWS", "BETWEEN", "PRECEDING",
    "CURRENT", "ROW", "ASC", "DESC", "NULLS", "FIRST", "LAST", "AS", "ON",
}


def rewrite_metric(
    expr: str,
    owning_table_dax: str | None,
    column_owner: dict[str, str],
    metric_owner: dict[str, str],
    local_columns: set[str] | None = None,
    dim_resolver: "callable | None" = None,
) -> DaxRewriteResult:
    """Translate a single metric expression.

    Args:
        expr: Source SQL expression.
        owning_table_dax: DAX-quoted name of the table the metric lives on
            (e.g. ``"'fact_premium_txn'"``). None for derived/view-level metrics.
        column_owner: Map from UPPERCASE column name → DAX-quoted owning table.
        metric_owner: Map from lowercase ``"table.metric"`` → ``"[metric]"`` DAX ref.
        local_columns: Optional set of UPPERCASE columns belonging to the owning
            table. When provided, columns in this set resolve to the owning table
            even if the global ``column_owner`` first-assigned them elsewhere.
        dim_resolver: Optional ``(table_lower, dim_lower) -> physical_col`` callable
            used to translate SV-level dimension names (e.g. ``txn_date.txn_date``)
            referenced inside window-function clauses into the physical TMDL
            column name (e.g. ``DATE_VAL``).
    """
    notes: list[str] = []
    text = expr.strip()

    def _resolve(col: str) -> str | None:
        if local_columns and owning_table_dax and col in local_columns:
            return owning_table_dax
        return column_owner.get(col, owning_table_dax)

    # 1. Window function: trailing N+1 month SUM(SUM(x)) OVER (...)
    m = _RE_OVER.search(text)
    if m:
        col = m.group("col").upper()
        dim_tbl = m.group("dtbl")
        dim_col = m.group("dcol")
        if dim_resolver is not None:
            resolved = dim_resolver(dim_tbl.lower(), dim_col.lower())
            if resolved:
                dim_col = resolved
        n = int(m.group("n")) + 1  # PRECEDING + current row
        base_table = _resolve(col) or "'UNKNOWN'"
        date_ref = f"'{dim_tbl}'[{dim_col}]"
        return DaxRewriteResult(
            expression=(
                f"CALCULATE(SUM({base_table}[{col}]),\n"
                f"    DATESINPERIOD({date_ref}, LASTDATE({date_ref}), -{n}, MONTH))"
            ),
            needs_review=False,
            notes=["window→DATESINPERIOD"],
        )

    # 2. DIV0(a, b) → DIVIDE(a, b) (Snowflake safe division, paren-balanced)
    text = _rewrite_div0_calls(text, owning_table_dax, column_owner, metric_owner, notes, local_columns)
    if "DIVIDE(" in text and "DIV0" not in text.upper():
        pass  # continue with generic rewriting below

    # 3. a / NULLIF(b, 0) → DIVIDE(a, b)  (apply once, then continue)
    def _div_repl(mm: re.Match[str]) -> str:
        n = _rewrite_inner(mm.group("num"), owning_table_dax, column_owner, metric_owner, notes, local_columns)
        d = _rewrite_inner(mm.group("den"), owning_table_dax, column_owner, metric_owner, notes, local_columns)
        return f"DIVIDE({n}, {d})"

    if _RE_NULLIF_DIV.search(text):
        text = _RE_NULLIF_DIV.sub(_div_repl, text)
        return DaxRewriteResult(expression=text, needs_review=False, notes=notes or ["nullif→DIVIDE"])

    # 4. Generic rewrite (COUNT(*), SUM(...), cross-metric refs, column refs)
    rewritten = _rewrite_inner(text, owning_table_dax, column_owner, metric_owner, notes, local_columns)
    return DaxRewriteResult(expression=rewritten, needs_review=False, notes=notes)


def _rewrite_inner(
    text: str,
    owning_table_dax: str | None,
    column_owner: dict[str, str],
    metric_owner: dict[str, str],
    notes: list[str],
    local_columns: set[str] | None = None,
) -> str:
    # COUNT(*) -> COUNTROWS('<owning_table>')
    if owning_table_dax:
        text = _RE_COUNT_STAR.sub(f"COUNTROWS({owning_table_dax})", text)

    # SUM( <expr> ) — if <expr> is a simple column, use SUM(table[col]); otherwise SUMX(table, ...)
    text = _rewrite_sum_calls(text, owning_table_dax, column_owner, notes, local_columns)

    # Cross-metric refs: <tbl>.<metric>  →  [metric]
    def _met_repl(mm: re.Match[str]) -> str:
        key = f"{mm.group('tbl').lower()}.{mm.group('met').lower()}"
        if key in metric_owner:
            return metric_owner[key]
        return mm.group(0)

    text = _RE_QUAL_METRIC.sub(_met_repl, text)

    # Bare column refs → 'owning_table'[col]
    def _col_repl(mm: re.Match[str]) -> str:
        token = mm.group(1)
        if token.upper() in _SQL_KEYWORDS:
            return token
        if not token.isupper():
            return token  # likely a function name or alias
        # Skip function calls: identifier immediately followed by `(`
        after = mm.end()
        s = mm.string
        k = after
        while k < len(s) and s[k] == " ":
            k += 1
        if k < len(s) and s[k] == "(":
            return token
        # Skip identifiers already inside a bracketed column ref `[...]`
        # by checking if there's an unmatched `[` before this position
        # within the current line / segment.
        before = s[:mm.start()]
        # walk backwards to find nearest `[` or `]` to determine bracket state
        depth = 0
        for ch in reversed(before):
            if ch == "]":
                depth += 1
            elif ch == "[":
                if depth == 0:
                    return token  # already inside [...]
                depth -= 1
        owner = column_owner.get(token, owning_table_dax)
        if local_columns and owning_table_dax and token in local_columns:
            owner = owning_table_dax
        if not owner:
            return token
        return f"{owner}[{token}]"

    text = _RE_BARE_COL.sub(_col_repl, text)
    return text


def _rewrite_sum_calls(
    text: str,
    owning_table_dax: str | None,
    column_owner: dict[str, str],
    notes: list[str],
    local_columns: set[str] | None = None,
) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        m = _RE_SUM.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        # find matching paren
        depth = 1
        j = m.end()
        while j < n and depth > 0:
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        inner = text[m.end():j].strip()
        close = j + 1
        # If `inner` is a single bare uppercase identifier, simple SUM
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*", inner):
            if local_columns and owning_table_dax and inner in local_columns:
                owner = owning_table_dax
            else:
                owner = column_owner.get(inner, owning_table_dax) or "'UNKNOWN'"
            out.append(f"SUM({owner}[{inner}])")
        else:
            # Derived: use SUMX over the owning table, rewriting inner column refs.
            inner_rew = _rewrite_inner(inner, owning_table_dax, column_owner, {}, notes, local_columns)
            table_ref = owning_table_dax or "'UNKNOWN'"
            out.append(f"SUMX({table_ref}, {inner_rew})")
            notes.append("SUM(expr)→SUMX")
        i = close
    return "".join(out)


_RE_DIV0_HEAD = re.compile(r"\bDIV0\s*\(", re.IGNORECASE)


def _split_args_top_level(text: str) -> list[str]:
    """Split on commas at paren depth 0, respecting single-quoted strings."""
    args: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "'":
            buf.append(c)
            j = i + 1
            while j < n:
                buf.append(text[j])
                if text[j] == "'":
                    j += 1
                    break
                j += 1
            i = j
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == "," and depth == 0:
            args.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        args.append(tail)
    return args


def _rewrite_div0_calls(
    text: str,
    owning_table_dax: str | None,
    column_owner: dict[str, str],
    metric_owner: dict[str, str],
    notes: list[str],
    local_columns: set[str] | None = None,
) -> str:
    """Rewrite DIV0(a, b) → DIVIDE(a, b) using paren-balanced matching."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        m = _RE_DIV0_HEAD.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        depth = 1
        j = m.end()
        while j < n and depth > 0:
            if text[j] == "'":
                k = j + 1
                while k < n and text[k] != "'":
                    k += 1
                j = k + 1
                continue
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            out.append(text[m.start():])
            break
        inner = text[m.end():j]
        args = _split_args_top_level(inner)
        if len(args) == 2:
            num = _rewrite_inner(args[0], owning_table_dax, column_owner, metric_owner, notes, local_columns)
            den = _rewrite_inner(args[1], owning_table_dax, column_owner, metric_owner, notes, local_columns)
            out.append(f"DIVIDE({num}, {den})")
            notes.append("DIV0→DIVIDE")
        else:
            out.append(text[m.start():j + 1])
        i = j + 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Calculated-column rewriter (Direct Lake on OneLake supports calc columns)
# ---------------------------------------------------------------------------

# Snowflake DATEDIFF(<part>, <start>, <end>)  →  DAX DATEDIFF(<start>, <end>, <interval>)
# (Args can themselves be function calls, so we locate by name and balance parens
# instead of using a fixed-shape regex.)
_RE_DATEDIFF_HEAD = re.compile(r"\bDATEDIFF\s*\(", re.IGNORECASE)
_RE_CURRENT_DATE = re.compile(r"\bCURRENT_DATE\s*\(\s*\)", re.IGNORECASE)
# bare `col IN ('a','b',...)`  →  `[col] IN { "a", "b", ... }`
_RE_IN_LIST = re.compile(
    r"\b(?P<col>[A-Z_][A-Z0-9_]*)\s+IN\s*\(\s*(?P<vals>(?:'[^']*'\s*,\s*)*'[^']*')\s*\)",
    re.IGNORECASE,
)
_RE_STR_LIT = re.compile(r"'([^']*)'")


@dataclass
class CalcColumnResult:
    expression: str | None      # None means "skip — emit nothing"
    data_type: str              # TMDL data type
    notes: list[str]


def rewrite_calc_column(
    expr: str,
    owning_table: str,
    local_columns: set[str] | None = None,
) -> CalcColumnResult:
    """Translate a Snowflake calculated-dimension/-fact expression to a DAX
    calculated-column expression.

    Returns ``CalcColumnResult(expression=None, ...)`` for trivial constants
    (e.g. ``AS 1`` helper columns) so callers can skip emission.
    """
    notes: list[str] = []
    text = expr.strip()

    # Drop trivial constants like `AS 1` (Snowflake helpers for COUNT DISTINCT
    # patterns that have no DAX equivalent — measures handle that directly).
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return CalcColumnResult(expression=None, data_type="int64", notes=["constant→skip"])

    # 1. CURRENT_DATE() → TODAY()  (must run before DATEDIFF arg-splitting)
    text = _RE_CURRENT_DATE.sub("TODAY()", text)

    # 2. DATEDIFF(part, a, b) → DATEDIFF(a, b, PART) with arg-aware paren scan
    text = _rewrite_datediff(text)

    # 3. col IN ('a','b')  →  [col] IN { "a", "b" }   (before generic bracketing)
    def _in(mm: re.Match[str]) -> str:
        col = mm.group("col")
        vals = ", ".join(f'"{v}"' for v in _RE_STR_LIT.findall(mm.group("vals")))
        return f"[{col}] IN {{ {vals} }}"

    text = _RE_IN_LIST.sub(_in, text)

    # 4. Single-quoted string literals → double-quoted (DAX uses ")
    text = _RE_STR_LIT.sub(lambda mm: f'"{mm.group(1)}"', text)

    # 5. Bracket bare uppercase identifiers (quote- and bracket-aware).
    text = _bracket_idents(text)

    # Heuristic data-type guess
    upper = text.upper()
    if "DATEDIFF(" in upper or re.search(r"\b(YEAR|MONTH|DAY)\(", upper):
        dtype = "int64"
    elif any(op in text for op in (" = ", " <> ", " != ", " IN {", " > ", " < ", " >= ", " <= ")):
        dtype = "boolean"
    elif any(op in text for op in (" + ", " - ", " * ", " / ")):
        dtype = "double"
    else:
        dtype = "string"

    return CalcColumnResult(expression=text, data_type=dtype, notes=notes)


def _rewrite_datediff(text: str) -> str:
    """Rewrite every Snowflake-style DATEDIFF(part, a, b) into DAX
    DATEDIFF(a, b, PART). Args may contain nested function calls.
    """
    part_map = {
        "YEAR": "YEAR", "YEARS": "YEAR", "YY": "YEAR", "YYYY": "YEAR",
        "QUARTER": "QUARTER", "QUARTERS": "QUARTER", "QQ": "QUARTER",
        "MONTH": "MONTH", "MONTHS": "MONTH", "MM": "MONTH",
        "DAY": "DAY", "DAYS": "DAY", "DD": "DAY",
    }
    out: list[str] = []
    i = 0
    while i < len(text):
        m = _RE_DATEDIFF_HEAD.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        # Find matching close paren from m.end()-1 (the open paren position).
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
        # Split top-level commas.
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
            dax_part = part_map.get(part, "DAY")
            a = _bracket_idents(args[1])
            b = _bracket_idents(args[2])
            out.append(f"DATEDIFF({a}, {b}, {dax_part})")
        else:
            # Unexpected arity — leave as-is for review.
            out.append(text[m.start():j + 1])
        i = j + 1
    return "".join(out)


def _bracket_idents(text: str) -> str:
    """Wrap bare uppercase identifiers in `[...]`. Skips identifiers inside
    `"..."` string literals, already inside `[...]`, or immediately followed
    by `(` (function calls).
    """
    # Find bracketed and quoted spans up front so we can skip them.
    skip: list[tuple[int, int]] = []
    in_q = False
    q_start = 0
    depth = 0
    b_start = 0
    for idx, ch in enumerate(text):
        if in_q:
            if ch == '"':
                skip.append((q_start, idx + 1))
                in_q = False
            continue
        if ch == '"':
            in_q = True
            q_start = idx
        elif ch == "[":
            if depth == 0:
                b_start = idx
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                skip.append((b_start, idx + 1))

    def in_skip(pos: int) -> bool:
        for a, b in skip:
            if a <= pos < b:
                return True
        return False

    def _repl(mm: re.Match[str]) -> str:
        token = mm.group(1)
        if token.upper() in _SQL_KEYWORDS or token.upper() in {
            "TODAY", "DATEDIFF", "YEAR", "QUARTER", "MONTH", "DAY", "BLANK",
        }:
            return token
        if in_skip(mm.start()):
            return token
        # function call?
        s = mm.string
        k = mm.end()
        while k < len(s) and s[k] == " ":
            k += 1
        if k < len(s) and s[k] == "(":
            return token
        return f"[{token}]"

    return _RE_BARE_COL.sub(_repl, text)
