"""Rewrite DAX measure / calculated-column expressions back into Snowflake SQL.

This is the reverse of ``translator/emitters/dax_rewriter.py``. It mirrors the
same subset that the forward path supports, so a round-trip
(SQL → DAX → SQL) preserves the recognised patterns:

  * COUNTROWS('t')                        → COUNT(*)
  * SUM('t'[COL])                         → SUM(COL)
  * SUMX('t', <expr>)                     → SUM(<expr>)
  * DIVIDE(a, b)                          → a / NULLIF(b, 0)
  * 't'[COL] / [measure] references       → COL / <measure name>
  * CALCULATE(SUM('t'[C]), DATESINPERIOD(...))  → SUM(SUM(C)) OVER (... ROWS ...)
  * [measure]                             → measure name (cross-metric ref)
  * IN { "a", "b" }                       → IN ('a','b')  (calc columns)
  * DATEDIFF(a, b, PART)                  → DATEDIFF(PART, a, b)

Anything not recognised is returned as-is with a leading ``/* review: ... */``
marker so the drift report can flag it for a human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class SqlRewriteResult:
    expression: str
    needs_review: bool
    notes: list[str]


_RE_COUNTROWS = re.compile(r"\bCOUNTROWS\s*\(\s*'?([A-Za-z_][A-Za-z0-9_]*)'?\s*\)", re.IGNORECASE)
_RE_COL_REF = re.compile(r"'?([A-Za-z_][A-Za-z0-9_]*)'?\s*\[\s*([A-Za-z_][A-Za-z0-9_]*)\s*\]")
_RE_MEASURE_REF = re.compile(r"(?<![A-Za-z0-9_])\[\s*([A-Za-z_][A-Za-z0-9_]*)\s*\]")
_RE_DIVIDE_HEAD = re.compile(r"\bDIVIDE\s*\(", re.IGNORECASE)
_RE_SUMX_HEAD = re.compile(r"\bSUMX\s*\(", re.IGNORECASE)
_RE_SUM_HEAD = re.compile(r"(?<![A-Za-z0-9_])SUM\s*\(", re.IGNORECASE)
_RE_CALCULATE_DATESINPERIOD = re.compile(
    r"CALCULATE\s*\(\s*SUM\s*\(\s*'?(?P<tbl>[A-Za-z_]\w*)'?\s*\[\s*(?P<col>\w+)\s*\]\s*\)\s*,\s*"
    r"DATESINPERIOD\s*\(\s*'?(?P<dtbl>[A-Za-z_]\w*)'?\s*\[\s*(?P<dcol>\w+)\s*\]\s*,\s*"
    r"(?:LASTDATE|MAX)\s*\([^)]*\)\s*,\s*-?(?P<n>\d+)\s*,\s*MONTH\s*\)\s*\)",
    re.IGNORECASE | re.DOTALL,
)
_RE_IN_SET = re.compile(r"\[\s*(?P<col>\w+)\s*\]\s+IN\s*\{\s*(?P<vals>[^}]*)\}", re.IGNORECASE)
_RE_DBLQ_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')
_RE_DATEDIFF_HEAD = re.compile(r"\bDATEDIFF\s*\(", re.IGNORECASE)
_RE_TODAY = re.compile(r"\bTODAY\s*\(\s*\)", re.IGNORECASE)


def _find_matching(text: str, open_idx: int) -> int:
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        if c == "'":
            i += 1
            while i < n and text[i] != "'":
                i += 1
        elif c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_args(inner: str) -> list[str]:
    args: list[str] = []
    depth = 0
    buf: list[str] = []
    i = 0
    n = len(inner)
    while i < n:
        c = inner[i]
        if c in "'\"":
            q = c
            buf.append(c)
            i += 1
            while i < n and inner[i] != q:
                buf.append(inner[i])
                i += 1
            if i < n:
                buf.append(inner[i])
        elif c == "(":
            depth += 1
            buf.append(c)
        elif c == ")":
            depth -= 1
            buf.append(c)
        elif c == "," and depth == 0:
            args.append("".join(buf).strip())
            buf = []
        else:
            buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        args.append(tail)
    return args


def _has_top_level_binop(text: str) -> bool:
    """True if a +, -, *, or / appears outside parens/brackets/quotes."""
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in "'\"":
            i += 1
            while i < n and text[i] != c:
                i += 1
        elif c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif c in "+-*/" and depth == 0:
            return True
        i += 1
    return False


def _rewrite_functions(text: str, notes: list[str]) -> str:
    """Rewrite DIVIDE / SUMX / DATEDIFF calls, innermost-first, left-to-right.

    Scans once with a moving cursor; each recognised call has its arguments
    rewritten (recursively) and is spliced in place. The cursor advances past
    the splice so a rewritten fragment is never re-matched, which guarantees
    termination.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        for head_re, kind in ((_RE_DIVIDE_HEAD, "DIVIDE"), (_RE_SUMX_HEAD, "SUMX"), (_RE_DATEDIFF_HEAD, "DATEDIFF")):
            m = head_re.match(text, i)
            if m:
                break
        else:
            m = None
            kind = ""
        if not m:
            out.append(text[i])
            i += 1
            continue

        close = _find_matching(text, m.end() - 1)
        if close == -1:
            out.append(text[i])
            i += 1
            continue

        args = _split_args(text[m.end():close])
        if kind == "DIVIDE" and len(args) >= 2:
            a = _rewrite_functions(args[0], notes)
            b = _rewrite_functions(args[1], notes)
            notes.append("DIVIDE→/NULLIF")
            # Guard numerator precedence: `a - b / NULLIF(c,0)` would bind the
            # division first, so wrap a compound numerator in parentheses.
            if _has_top_level_binop(a):
                a = f"({a})"
            out.append(f"{a} / NULLIF({b}, 0)")
            i = close + 1
            continue
        if kind == "SUMX" and len(args) == 2:
            inner = _rewrite_functions(args[1], notes)
            notes.append("SUMX→SUM")
            out.append(f"SUM({inner})")
            i = close + 1
            continue
        if kind == "DATEDIFF" and len(args) == 3:
            a = _rewrite_functions(args[0], notes)
            b = _rewrite_functions(args[1], notes)
            part = args[2].strip().strip("'\"").upper()
            notes.append("DATEDIFF arg-order")
            out.append(f"DATEDIFF({part}, {a}, {b})")
            i = close + 1
            continue

        # Unrecognised arity — copy the call head char and move on.
        out.append(text[i])
        i += 1

    return "".join(out)


def _strip_col_tables(text: str) -> str:
    """``'table'[COL]`` → ``COL`` (Snowflake references the bare column)."""
    return _RE_COL_REF.sub(lambda mm: mm.group(2), text)


def _rewrite_in_sets(text: str) -> str:
    def _repl(mm: re.Match[str]) -> str:
        col = mm.group("col")
        vals = ",".join(f"'{v}'" for v in _RE_DBLQ_STR.findall(mm.group("vals")))
        return f"{col} IN ({vals})"

    return _RE_IN_SET.sub(_repl, text)


def dax_measure_to_sql(dax: str, owning_table: str | None = None) -> str:
    """Translate a single DAX expression back to a Snowflake SQL expression."""
    return rewrite_dax(dax, owning_table).expression


def rewrite_dax(dax: str, owning_table: str | None = None) -> SqlRewriteResult:
    notes: list[str] = []
    text = " ".join(dax.split()).strip()
    if not text:
        return SqlRewriteResult(expression="", needs_review=False, notes=notes)

    # 1. Window pattern: CALCULATE(SUM(t[C]), DATESINPERIOD(d[D], ..., -N, MONTH))
    mo = _RE_CALCULATE_DATESINPERIOD.search(text)
    if mo:
        col = mo.group("col")
        dtbl = mo.group("dtbl")
        dcol = mo.group("dcol")
        n = int(mo.group("n")) - 1
        expr = (
            f"SUM(SUM({col})) OVER (ORDER BY {dtbl}.{dcol} "
            f"ROWS BETWEEN {n} PRECEDING AND CURRENT ROW)"
        )
        notes.append("DATESINPERIOD→window")
        return SqlRewriteResult(expression=expr, needs_review=False, notes=notes)

    # 2. COUNTROWS('t') -> COUNT(*)
    text = _RE_COUNTROWS.sub("COUNT(*)", text)

    # 3. DIVIDE / SUMX / DATEDIFF nested calls
    text = _rewrite_functions(text, notes)

    # 4. IN { "a", "b" } -> IN ('a','b')  (calc columns; match forward spacing)
    text = _rewrite_in_sets(text)

    # 5. TODAY() -> CURRENT_DATE()
    text = _RE_TODAY.sub("CURRENT_DATE()", text)

    # 6. 'table'[COL] -> COL  (before measure-ref stripping, so column brackets
    #    are not misread as bare [measure] references).
    text = _strip_col_tables(text)

    # 7. Cross-measure refs [measure] -> measure  (only bare brackets remain now)
    text = _RE_MEASURE_REF.sub(lambda mm: mm.group(1), text)

    # 8. Remaining double-quoted string literals -> single-quoted (SQL)
    text = _RE_DBLQ_STR.sub(lambda mm: "'" + mm.group(1) + "'", text)

    needs_review = bool(re.search(r"\b(VAR|RETURN|EARLIER|RELATED|FILTER|ALL|LASTNONBLANK|FIRSTNONBLANK)\b", text, re.IGNORECASE))
    if needs_review:
        notes.append("unrecognised DAX")
        text = f"/* review: {text} */"

    return SqlRewriteResult(expression=text, needs_review=needs_review, notes=notes)
