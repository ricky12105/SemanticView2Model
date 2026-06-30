"""Parse a Snowflake `CREATE SEMANTIC VIEW` DDL into the IR.

Hand-rolled parser focused on the grammar exercised by our fixtures:
    CREATE [OR REPLACE] SEMANTIC VIEW <db>.<schema>.<name>
      TABLES (...)
      RELATIONSHIPS (...)
      FACTS (...)
      DIMENSIONS (...)
      METRICS (...)
      [COMMENT = '...']
      [AI_VERIFIED_QUERIES (...)]
      [WITH TAG (...)]
      ;

`sqlglot` does not parse this Snowflake-only DDL form fully, so we implement
the minimum required ourselves while staying respectful of strings/comments.
"""

from __future__ import annotations

import re
from pathlib import Path

from translator.ir import (
    AccessModifier,
    BaseTableRef,
    Dimension,
    Fact,
    LogicalTable,
    Metric,
    NonAdditiveDim,
    NullOrder,
    Relationship,
    RelationshipColumn,
    SemanticView,
    SortDirection,
    Tag,
    TagRef,
    VerifiedQuery,
)


# ---------------------------------------------------------------------------
# Preprocessing
# ---------------------------------------------------------------------------

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_comments(text: str) -> str:
    """Remove `--` and /* */ comments without touching string literals."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "'" and j + 1 < n and text[j + 1] == "'":
                    j += 2
                    continue
                if text[j] == "'":
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
            continue
        if c == "-" and i + 1 < n and text[i + 1] == "-":
            j = text.find("\n", i)
            if j == -1:
                break
            i = j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            if j == -1:
                break
            i = j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split `text` on `sep` at paren depth 0, respecting quoted strings."""
    out: list[str] = []
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
                    if j + 1 < n and text[j + 1] == "'":
                        buf.append(text[j + 1])
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            i = j
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if depth == 0 and text[i:i + len(sep)] == sep:
            out.append("".join(buf).strip())
            buf = []
            i += len(sep)
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _find_keyword_clause(text: str, keyword: str, start: int = 0) -> tuple[int, int, str] | None:
    """Find `KEYWORD (...)` at top level. Returns (kw_start, kw_end, inner_text)."""
    pattern = re.compile(r"\b" + re.escape(keyword) + r"\b", re.IGNORECASE)
    pos = start
    while True:
        m = _find_at_top_level(text, pattern, pos)
        if not m:
            return None
        i = m.end()
        # Skip whitespace to find opening paren
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text) or text[i] != "(":
            pos = m.end()
            continue
        # Consume balanced parens
        depth = 1
        j = i + 1
        n = len(text)
        while j < n and depth > 0:
            ch = text[j]
            if ch == "'":
                k = j + 1
                while k < n:
                    if text[k] == "'":
                        if k + 1 < n and text[k + 1] == "'":
                            k += 2
                            continue
                        k += 1
                        break
                    k += 1
                j = k
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return (m.start(), j + 1, text[i + 1:j])
            j += 1
        return None


def _find_at_top_level(text: str, pattern: re.Pattern[str], start: int) -> re.Match[str] | None:
    """Find first match of `pattern` at paren depth 0, skipping quoted strings."""
    n = len(text)
    i = start
    depth = 0
    while i < n:
        c = text[i]
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            i = j
            continue
        if c == "(":
            depth += 1
            i += 1
            continue
        if c == ")":
            depth -= 1
            i += 1
            continue
        if depth == 0:
            m = pattern.match(text, i)
            if m:
                return m
        i += 1
    return None


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    return s


def _split_csv_list(inner: str) -> list[str]:
    return [x.strip() for x in _split_top_level(inner) if x.strip()]


def _parse_synonyms_list(inner: str) -> list[str]:
    return [_strip_quotes(x) for x in _split_csv_list(inner)]


# ---------------------------------------------------------------------------
# Trailer extraction (WITH SYNONYMS / COMMENT / LABELS = (FILTER))
# ---------------------------------------------------------------------------

_RE_WITH_SYN = re.compile(r"\bWITH\s+SYNONYMS\s*=\s*\(", re.IGNORECASE)
_RE_COMMENT = re.compile(r"\bCOMMENT\s*=\s*'", re.IGNORECASE)
_RE_LABELS = re.compile(r"\bLABELS\s*=\s*\(", re.IGNORECASE)
_RE_NON_ADD = re.compile(r"\bNON\s+ADDITIVE\s+BY\s*\(", re.IGNORECASE)
_RE_PRIMARY_KEY = re.compile(r"\bPRIMARY\s+KEY\s*\(", re.IGNORECASE)
_RE_UNIQUE = re.compile(r"\bUNIQUE\s*\(", re.IGNORECASE)


def _consume_paren(text: str, open_idx: int) -> tuple[str, int]:
    """Given index of `(`, return (inner_text, end_idx_after_close)."""
    depth = 1
    j = open_idx + 1
    n = len(text)
    while j < n and depth > 0:
        ch = text[j]
        if ch == "'":
            k = j + 1
            while k < n:
                if text[k] == "'":
                    if k + 1 < n and text[k + 1] == "'":
                        k += 2
                        continue
                    k += 1
                    break
                k += 1
            j = k
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:j], j + 1
        j += 1
    raise ValueError("Unbalanced parens")


def _consume_string(text: str, quote_idx: int) -> tuple[str, int]:
    """Given index of `'`, return (raw_value_unquoted, end_idx_after_close_quote)."""
    n = len(text)
    j = quote_idx + 1
    parts: list[str] = []
    while j < n:
        if text[j] == "'":
            if j + 1 < n and text[j + 1] == "'":
                parts.append("'")
                j += 2
                continue
            return "".join(parts), j + 1
        parts.append(text[j])
        j += 1
    raise ValueError("Unterminated string literal")


def _extract_trailers(item_text: str) -> tuple[str, dict]:
    """Pull WITH SYNONYMS / LABELS / COMMENT / NON ADDITIVE BY off the end of an item.

    Returns (expr_text_without_trailers, {synonyms, labels, comment, non_additive}).
    """
    found: list[tuple[int, str, str, int]] = []
    for label, pat in (
        ("syn", _RE_WITH_SYN),
        ("labels", _RE_LABELS),
        ("nonadd", _RE_NON_ADD),
    ):
        for m in pat.finditer(item_text):
            # Only consider if at depth 0 in expr
            if _depth_at(item_text, m.start()) == 0:
                found.append((m.start(), label, m.group(0), m.end()))
    for m in _RE_COMMENT.finditer(item_text):
        if _depth_at(item_text, m.start()) == 0:
            found.append((m.start(), "comment", m.group(0), m.end()))

    if not found:
        return item_text.strip(), {}

    found.sort(key=lambda x: x[0])
    first_start = found[0][0]
    expr_text = item_text[:first_start].rstrip().rstrip(",").rstrip()

    out: dict = {"synonyms": [], "labels": [], "comment": None, "non_additive": []}
    for start, kind, _g, paren_or_quote_start in found:
        if kind == "comment":
            # paren_or_quote_start points just past the opening quote position;
            # actually _RE_COMMENT ends at the `'`, so quote_idx = end-1.
            qidx = paren_or_quote_start - 1
            val, _end = _consume_string(item_text, qidx)
            out["comment"] = val
        else:
            # opening paren is at paren_or_quote_start - 1
            open_idx = paren_or_quote_start - 1
            inner, _end = _consume_paren(item_text, open_idx)
            if kind == "syn":
                out["synonyms"] = _parse_synonyms_list(inner)
            elif kind == "labels":
                out["labels"] = [x.strip().lower() for x in _split_csv_list(inner)]
            elif kind == "nonadd":
                out["non_additive"] = _parse_non_additive(inner)
    return expr_text, out


def _depth_at(text: str, pos: int) -> int:
    """Compute paren depth at `pos`, ignoring quoted strings."""
    depth = 0
    i = 0
    while i < pos:
        c = text[i]
        if c == "'":
            j = i + 1
            while j < pos:
                if text[j] == "'":
                    if j + 1 < len(text) and text[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            i = j
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    return depth


def _parse_non_additive(inner: str) -> list[NonAdditiveDim]:
    out: list[NonAdditiveDim] = []
    for item in _split_csv_list(inner):
        # <table>.<dim> [ASC|DESC] [NULLS FIRST|NULLS LAST]
        m = re.match(
            r"(?P<tbl>\w+)\.(?P<dim>\w+)\s*(?P<dir>ASC|DESC)?\s*(?:NULLS\s+(?P<nulls>FIRST|LAST))?",
            item,
            re.IGNORECASE,
        )
        if not m:
            raise ValueError(f"Cannot parse NON ADDITIVE BY entry: {item!r}")
        direction = SortDirection.DESC if (m.group("dir") or "").upper() == "DESC" else SortDirection.ASC
        nulls_raw = (m.group("nulls") or "").upper()
        nulls = NullOrder.FIRST if nulls_raw == "FIRST" else (NullOrder.LAST if nulls_raw == "LAST" else None)
        out.append(NonAdditiveDim(table=m.group("tbl"), dimension=m.group("dim"), sort_direction=direction, null_order=nulls))
    return out


# ---------------------------------------------------------------------------
# Per-clause item parsers
# ---------------------------------------------------------------------------

_RE_TABLE_HEAD = re.compile(r"^(?P<name>\w+)\s+AS\s+(?P<fqn>[\w.]+)", re.IGNORECASE)
_RE_REL = re.compile(
    r"^(?P<name>\w+)\s+AS\s+(?P<lt>\w+)\s*\((?P<lc>[^)]+)\)\s*REFERENCES\s+(?P<rt>\w+)\s*\((?P<rc>[^)]+)\)",
    re.IGNORECASE,
)
_RE_QUAL = re.compile(r"^(?P<priv>PRIVATE\s+)?(?P<tbl>\w+)\.(?P<col>\w+)\s+AS\s+", re.IGNORECASE)
_RE_VIEW_METRIC = re.compile(r"^(?P<name>\w+)\s+AS\s+", re.IGNORECASE)


def _parse_tables(inner: str) -> list[LogicalTable]:
    tables: list[LogicalTable] = []
    for item in _split_csv_list(inner):
        m = _RE_TABLE_HEAD.match(item)
        if not m:
            raise ValueError(f"Bad TABLES entry: {item!r}")
        name = m.group("name")
        fqn = m.group("fqn").split(".")
        if len(fqn) != 3:
            raise ValueError(f"Expected DB.SCHEMA.TABLE in {item!r}")
        rest = item[m.end():]

        primary_key: list[str] = []
        uniques: list[list[str]] = []
        synonyms: list[str] = []
        comment: str | None = None

        # PRIMARY KEY
        pm = _RE_PRIMARY_KEY.search(rest)
        if pm:
            inner_pk, _ = _consume_paren(rest, pm.end() - 1)
            primary_key = _split_csv_list(inner_pk)

        # UNIQUE (possibly multiple)
        for um in _RE_UNIQUE.finditer(rest):
            inner_u, _ = _consume_paren(rest, um.end() - 1)
            uniques.append(_split_csv_list(inner_u))

        # WITH SYNONYMS
        sm = _RE_WITH_SYN.search(rest)
        if sm:
            inner_s, _ = _consume_paren(rest, sm.end() - 1)
            synonyms = _parse_synonyms_list(inner_s)

        # COMMENT
        cm = _RE_COMMENT.search(rest)
        if cm:
            comment, _ = _consume_string(rest, cm.end() - 1)

        tables.append(
            LogicalTable(
                name=name,
                description=comment,
                synonyms=synonyms,
                base_table=BaseTableRef(database=fqn[0], schema=fqn[1], table=fqn[2]),
                primary_key=primary_key,
                unique_constraints=uniques,
            )
        )
    return tables


def _parse_relationships(inner: str) -> list[Relationship]:
    rels: list[Relationship] = []
    for item in _split_csv_list(inner):
        m = _RE_REL.match(item)
        if not m:
            raise ValueError(f"Bad RELATIONSHIPS entry: {item!r}")
        lcols = [x.strip() for x in m.group("lc").split(",")]
        rcols = [x.strip() for x in m.group("rc").split(",")]
        if len(lcols) != len(rcols):
            raise ValueError(f"Relationship column count mismatch in {item!r}")
        rels.append(
            Relationship(
                name=m.group("name"),
                left_table=m.group("lt"),
                right_table=m.group("rt"),
                relationship_columns=[
                    RelationshipColumn(left_column=lc, right_column=rc) for lc, rc in zip(lcols, rcols)
                ],
            )
        )
    return rels


def _parse_qualified_items(inner: str) -> list[tuple[str, str, str, str, dict]]:
    """Parse `[PRIVATE] tbl.col AS <expr> [trailers]` items.

    Returns list of (access_modifier_str, table, column, expr, trailers).
    """
    out: list[tuple[str, str, str, str, dict]] = []
    for item in _split_csv_list(inner):
        # Snowflake allows `LABELS = (...)` to appear between `tbl.col` and
        # `AS`. Pre-strip it so `_RE_QUAL` matches, then merge it back into
        # trailers.
        pre_labels: list[str] = []
        lm = _RE_LABELS.search(item)
        if lm and _depth_at(item, lm.start()) == 0:
            # Ensure LABELS is before the first top-level AS
            as_match = re.search(r"\bAS\b", item, re.IGNORECASE)
            if as_match and lm.start() < as_match.start():
                open_idx = lm.end() - 1
                inner_lbl, end_idx = _consume_paren(item, open_idx)
                pre_labels = [x.strip().lower() for x in _split_csv_list(inner_lbl)]
                item = (item[:lm.start()] + " " + item[end_idx:]).strip()
        m = _RE_QUAL.match(item)
        if not m:
            raise ValueError(f"Unparseable qualified item: {item!r}")
        access = AccessModifier.PRIVATE.value if m.group("priv") else AccessModifier.PUBLIC.value
        rest = item[m.end():]
        expr, trailers = _extract_trailers(rest)
        if pre_labels:
            trailers["labels"] = sorted(set((trailers.get("labels") or []) + pre_labels))
        out.append((access, m.group("tbl"), m.group("col"), expr, trailers))
    return out


def _parse_metrics(inner: str) -> tuple[list[tuple[str, str, str, str, dict]], list[tuple[str, str, dict]]]:
    """Return (table_scoped_items, view_scoped_items)."""
    table_items: list[tuple[str, str, str, str, dict]] = []
    view_items: list[tuple[str, str, dict]] = []
    for item in _split_csv_list(inner):
        # Snowflake allows `NON ADDITIVE BY (...)` to appear between
        # `tbl.col` and the `AS <expr>`. Pre-strip so the expression
        # extraction sees only post-AS trailers.
        pre_non_add: list = []
        nam = _RE_NON_ADD.search(item)
        if nam and _depth_at(item, nam.start()) == 0:
            as_match = re.search(r"\bAS\b", item, re.IGNORECASE)
            if as_match and nam.start() < as_match.start():
                open_idx = nam.end() - 1
                inner_na, end_idx = _consume_paren(item, open_idx)
                pre_non_add = _parse_non_additive(inner_na)
                item = (item[:nam.start()] + " " + item[end_idx:]).strip()
        # Try qualified first
        m = _RE_QUAL.match(item)
        if m:
            access = AccessModifier.PRIVATE.value if m.group("priv") else AccessModifier.PUBLIC.value
            rest = item[m.end():]
            expr, trailers = _extract_trailers(rest)
            if pre_non_add:
                trailers["non_additive"] = (trailers.get("non_additive") or []) + pre_non_add
            table_items.append((access, m.group("tbl"), m.group("col"), expr, trailers))
            continue
        m2 = _RE_VIEW_METRIC.match(item)
        if not m2:
            raise ValueError(f"Unparseable metric item: {item!r}")
        rest = item[m2.end():]
        expr, trailers = _extract_trailers(rest)
        if pre_non_add:
            trailers["non_additive"] = (trailers.get("non_additive") or []) + pre_non_add
        view_items.append((m2.group("name"), expr, trailers))
    return table_items, view_items


def _parse_verified_queries(inner: str) -> list[VerifiedQuery]:
    out: list[VerifiedQuery] = []
    for item in _split_csv_list(inner):
        m = re.match(r"^(?P<name>\w+)\s+AS\s*\(", item, re.IGNORECASE)
        if not m:
            raise ValueError(f"Bad AI_VERIFIED_QUERIES entry: {item!r}")
        body_inner, _ = _consume_paren(item, m.end() - 1)
        # body_inner contains QUESTION '...' [ONBOARDING_QUESTION TRUE|FALSE] SQL '...'
        q_match = re.search(r"\bQUESTION\b\s*'", body_inner, re.IGNORECASE)
        if not q_match:
            raise ValueError(f"Missing QUESTION in verified query {m.group('name')!r}")
        question, after_q = _consume_string(body_inner, q_match.end() - 1)

        onboarding = False
        ob_match = re.search(r"\bONBOARDING_QUESTION\b\s+(TRUE|FALSE)\b", body_inner[after_q:], re.IGNORECASE)
        if ob_match:
            onboarding = ob_match.group(1).upper() == "TRUE"

        sql_match = re.search(r"\bSQL\b\s*'", body_inner, re.IGNORECASE)
        if not sql_match:
            raise ValueError(f"Missing SQL in verified query {m.group('name')!r}")
        sql_value, _ = _consume_string(body_inner, sql_match.end() - 1)

        out.append(
            VerifiedQuery(
                name=m.group("name"),
                question=question,
                sql=sql_value,
                use_as_onboarding_question=onboarding,
            )
        )
    return out


def _parse_tags(inner: str) -> list[Tag]:
    out: list[Tag] = []
    for item in _split_csv_list(inner):
        # <db>.<schema>.<tag> = '<value>'
        m = re.match(r"^(?P<fqn>[\w.]+)\s*=\s*'", item)
        if not m:
            raise ValueError(f"Bad WITH TAG entry: {item!r}")
        parts = m.group("fqn").split(".")
        value, _ = _consume_string(item, m.end() - 1)
        if len(parts) == 3:
            tag = TagRef(database=parts[0], schema=parts[1], tag=parts[2])
        elif len(parts) == 1:
            tag = TagRef(tag=parts[0])
        else:
            raise ValueError(f"Unexpected tag FQN: {m.group('fqn')!r}")
        out.append(Tag(name=tag, value=value))
    return out


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

_RE_HEADER = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?SEMANTIC\s+VIEW\s+(?P<fqn>[\w.]+)",
    re.IGNORECASE,
)


def parse_sql(source: str | Path) -> SemanticView:
    if isinstance(source, Path) or (isinstance(source, str) and "\n" not in source and Path(source).exists()):
        text = Path(source).read_text(encoding="utf-8")
    else:
        text = source
    text = _strip_comments(text)

    header = _RE_HEADER.search(text)
    if not header:
        raise ValueError("CREATE [OR REPLACE] SEMANTIC VIEW header not found")
    view_fqn = header.group("fqn").split(".")
    view_name = view_fqn[-1].lower()

    body_start = header.end()

    # Locate top-level clauses
    tables_clause = _find_keyword_clause(text, "TABLES", body_start)
    rels_clause = _find_keyword_clause(text, "RELATIONSHIPS", body_start)
    facts_clause = _find_keyword_clause(text, "FACTS", body_start)
    dims_clause = _find_keyword_clause(text, "DIMENSIONS", body_start)
    metrics_clause = _find_keyword_clause(text, "METRICS", body_start)
    verified_clause = _find_keyword_clause(text, "AI_VERIFIED_QUERIES", body_start)

    if not tables_clause:
        raise ValueError("TABLES clause is required")
    tables = _parse_tables(tables_clause[2])
    tables_by_name = {t.name: t for t in tables}

    relationships: list[Relationship] = []
    if rels_clause:
        relationships = _parse_relationships(rels_clause[2])

    if facts_clause:
        for access, tbl, col, expr, tr in _parse_qualified_items(facts_clause[2]):
            t = tables_by_name.get(tbl)
            if not t:
                raise ValueError(f"FACT references unknown table {tbl!r}")
            t.facts.append(
                Fact(
                    name=col,
                    expr=expr,
                    description=tr.get("comment"),
                    synonyms=tr.get("synonyms") or [],
                    labels=tr.get("labels") or [],
                    access_modifier=AccessModifier(access),
                )
            )

    if dims_clause:
        for access, tbl, col, expr, tr in _parse_qualified_items(dims_clause[2]):
            t = tables_by_name.get(tbl)
            if not t:
                raise ValueError(f"DIMENSION references unknown table {tbl!r}")
            t.dimensions.append(
                Dimension(
                    name=col,
                    expr=expr,
                    description=tr.get("comment"),
                    synonyms=tr.get("synonyms") or [],
                    labels=tr.get("labels") or [],
                )
            )

    view_metrics: list[Metric] = []
    if metrics_clause:
        table_items, view_items = _parse_metrics(metrics_clause[2])
        for access, tbl, col, expr, tr in table_items:
            t = tables_by_name.get(tbl)
            if not t:
                raise ValueError(f"METRIC references unknown table {tbl!r}")
            t.metrics.append(
                Metric(
                    name=col,
                    expr=expr,
                    description=tr.get("comment"),
                    synonyms=tr.get("synonyms") or [],
                    access_modifier=AccessModifier(access),
                    non_additive_dimensions=tr.get("non_additive") or [],
                )
            )
        for name, expr, tr in view_items:
            view_metrics.append(
                Metric(
                    name=name,
                    expr=expr,
                    description=tr.get("comment"),
                    synonyms=tr.get("synonyms") or [],
                )
            )

    verified_queries: list[VerifiedQuery] = []
    if verified_clause:
        verified_queries = _parse_verified_queries(verified_clause[2])

    # View-level COMMENT = '...' (top-level, not inside any clause)
    view_comment: str | None = None
    for cm in _RE_COMMENT.finditer(text, body_start):
        if _depth_at(text, cm.start()) == 0:
            view_comment, _ = _consume_string(text, cm.end() - 1)
            break

    # View-level WITH TAG (...)
    tags: list[Tag] = []
    tag_clause = _find_keyword_clause(text, "TAG", body_start)
    if tag_clause:
        # ensure preceded by WITH
        prefix = text[:tag_clause[0]].rstrip()
        if prefix.lower().endswith("with"):
            tags = _parse_tags(tag_clause[2])

    return SemanticView(
        name=view_name,
        description=view_comment,
        tables=tables,
        relationships=relationships,
        metrics=view_metrics,
        verified_queries=verified_queries,
        tags=tags,
    )
