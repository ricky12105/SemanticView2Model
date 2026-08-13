"""Execute Snowflake DDL to sync semantic-view changes back to Snowflake.

Reads connection details from the environment so no secrets are passed on the
command line or through prompts:

    SNOWFLAKE_ACCOUNT      (required)
    SNOWFLAKE_USER         (required)
    SNOWFLAKE_PASSWORD     or SNOWFLAKE_TOKEN (one required)
    SNOWFLAKE_ROLE         (optional)
    SNOWFLAKE_WAREHOUSE    (optional)
    SNOWFLAKE_DATABASE     (optional)
    SNOWFLAKE_SCHEMA       (optional)

``snowflake-connector-python`` is an optional dependency; install it with the
``[snowflake]`` extra. ``execute_ddl`` defaults to a dry run and only touches
Snowflake when ``dry_run=False``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class SnowflakeExecResult:
    executed: bool
    statements: list[str]
    messages: list[str]


def _split_statements(ddl: str) -> list[str]:
    """Split a DDL script on top-level semicolons (ignoring those in strings)."""
    stmts: list[str] = []
    buf: list[str] = []
    in_str = False
    for ch in ddl:
        if ch == "'":
            in_str = not in_str
        if ch == ";" and not in_str:
            s = "".join(buf).strip()
            if s:
                stmts.append(s)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def _connect_params() -> dict:
    account = os.environ.get("SNOWFLAKE_ACCOUNT")
    user = os.environ.get("SNOWFLAKE_USER")
    password = os.environ.get("SNOWFLAKE_PASSWORD")
    token = os.environ.get("SNOWFLAKE_TOKEN")
    if not account or not user or not (password or token):
        raise EnvironmentError(
            "Missing Snowflake credentials: set SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, "
            "and SNOWFLAKE_PASSWORD (or SNOWFLAKE_TOKEN)."
        )
    params: dict = {"account": account, "user": user}
    if token:
        params["token"] = token
        params["authenticator"] = "oauth"
    else:
        params["password"] = password
    for key, env in (
        ("role", "SNOWFLAKE_ROLE"),
        ("warehouse", "SNOWFLAKE_WAREHOUSE"),
        ("database", "SNOWFLAKE_DATABASE"),
        ("schema", "SNOWFLAKE_SCHEMA"),
    ):
        val = os.environ.get(env)
        if val:
            params[key] = val
    return params


def execute_ddl(ddl: str, *, dry_run: bool = True) -> SnowflakeExecResult:
    """Run each statement in ``ddl`` against Snowflake.

    With ``dry_run=True`` (default) nothing is executed; the parsed statements
    are returned so the caller can display them for review.
    """
    statements = _split_statements(ddl)
    if dry_run:
        return SnowflakeExecResult(executed=False, statements=statements, messages=["dry run — nothing executed"])

    try:
        import snowflake.connector  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ImportError(
            "snowflake-connector-python is required to execute DDL. "
            "Install it with: pip install 'sv2m[snowflake]'"
        ) from exc

    messages: list[str] = []
    conn = snowflake.connector.connect(**_connect_params())
    try:
        cur = conn.cursor()
        try:
            for stmt in statements:
                cur.execute(stmt)
                messages.append(f"OK: {stmt.splitlines()[0][:80]}")
        finally:
            cur.close()
    finally:
        conn.close()
    return SnowflakeExecResult(executed=True, statements=statements, messages=messages)
