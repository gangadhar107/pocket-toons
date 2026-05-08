"""
agent/agent.py
--------------
Core orchestrator for the Analytics Q&A Agent.

Public API:
    ask(question: str) -> Answer | ClarificationNeeded

Pipeline:
    1. SYSTEM_GENERATE call -> {mode, clarification, sql}
    2. If mode="clarify": append to history, return ClarificationNeeded.
    3. validate_sql() (sqlglot). On SQLValidationError: one retry with the
       error text forwarded to the LLM.
    4. Append defensive LIMIT 1000 if the query has no top-level LIMIT.
    5. Execute via sqlite3 -> pandas.DataFrame. On OperationalError: one
       retry with the error text forwarded to the LLM.
    6. SYSTEM_SELFCHECK call -> {ok, reason, summary}
    7. chart.render(df) -> Figure | None  (None for scalars — UI hides tab).
    8. history.append(...)
    9. Return Answer.

Design notes:
  - Synchronous by design (one user, one question).
  - Single Anthropic client, reused across calls.
  - SQLValidationError and AgentExecutionError are the two typed failures
    callers should handle. Everything else bubbles as a generic exception.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import pandas as pd
import sqlglot
from dotenv import load_dotenv
from matplotlib.figure import Figure

from agent import chart, history
from agent.prompts import SYSTEM_GENERATE, SYSTEM_SELFCHECK
from agent.validate import DIALECT, SQLValidationError, validate_sql

load_dotenv()

# --- Config -----------------------------------------------------------------

DEFAULT_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
DEFAULT_DB_PATH = Path(os.getenv("ANALYTICS_DB_PATH", "analytics.db"))
DEFAULT_ROW_CAP = 1000
MAX_PREVIEW_ROWS = 20  # rows sent to the self-check prompt

# --- Typed results ----------------------------------------------------------


class AgentExecutionError(Exception):
    """Raised when SQL execution fails even after one LLM-assisted retry."""


@dataclass(frozen=True)
class SelfCheck:
    ok: bool
    reason: str


@dataclass
class Answer:
    question: str
    sql: str
    df: pd.DataFrame
    summary: str
    self_check: SelfCheck
    chart: Optional[Figure] = field(default=None)


@dataclass(frozen=True)
class ClarificationNeeded:
    question: str
    clarification: str


# --- Anthropic client (lazy singleton) --------------------------------------

_client = None


def _get_client():
    global _client
    if _client is None:
        from anthropic import Anthropic  # imported lazily so imports don't fail without the key
        _client = Anthropic()  # reads ANTHROPIC_API_KEY from env
    return _client


# --- JSON extraction --------------------------------------------------------

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a Claude response. Raises ValueError."""
    text = (text or "").strip()
    # Strip common markdown fences the model sometimes adds despite instructions.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    # Fast path: whole thing is JSON.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: first {...} block.
    m = _JSON_BLOCK.search(text)
    if not m:
        raise ValueError(f"no JSON object in response: {text[:200]!r}")
    return json.loads(m.group(0))


def _call_claude(system: str, user: str, *, max_tokens: int = 1024) -> dict:
    """One call → JSON dict. One retry on JSON-parse failure."""
    client = _get_client()
    last_err: Optional[Exception] = None
    for attempt in range(2):
        try:
            resp = client.messages.create(
                model=DEFAULT_MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(
                block.text for block in resp.content if getattr(block, "type", "") == "text"
            )
            return _extract_json(text)
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            user = (
                user
                + "\n\nReminder: respond with a single JSON object matching the schema, "
                  "no prose, no code fences."
            )
    raise RuntimeError(f"LLM failed to return valid JSON after 2 attempts: {last_err}")


# --- SQL helpers ------------------------------------------------------------

def _append_row_cap(sql: str, cap: int = DEFAULT_ROW_CAP) -> str:
    """Append a top-level LIMIT if the SELECT doesn't already have one.

    Uses sqlglot so we don't accidentally touch LIMITs inside subqueries or CTEs.
    Falls back to the original SQL on any parse error (validator would have
    already caught hard parse failures upstream).
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=DIALECT)
        if tree.args.get("limit") is None:
            tree.set("limit", sqlglot.exp.Limit(expression=sqlglot.exp.Literal.number(cap)))
            return tree.sql(dialect=DIALECT)
    except Exception:
        pass
    return sql


def _execute(sql: str, db_path: Path) -> pd.DataFrame:
    """Run the SQL and return a DataFrame. Lets sqlite3.OperationalError bubble."""
    conn = sqlite3.connect(db_path)
    try:
        return pd.read_sql_query(sql, conn)
    finally:
        conn.close()


# --- Public API -------------------------------------------------------------

def ask(
    question: str,
    *,
    db_path: Path = DEFAULT_DB_PATH,
) -> Union[Answer, ClarificationNeeded]:
    """Answer a natural-language analytics question end-to-end.

    Returns either an Answer (with SQL, DataFrame, summary, self-check, chart)
    or a ClarificationNeeded (agent decided the question was ambiguous).

    Raises:
        SQLValidationError   — validator rejected the SQL after one retry.
        AgentExecutionError  — sqlite3 rejected the SQL after one retry.
        RuntimeError         — LLM failed to return valid JSON after retries.
    """
    question = question.strip()
    if not question:
        raise ValueError("empty question")

    # 1. Generate / classify.
    gen = _call_claude(SYSTEM_GENERATE, question)
    mode = gen.get("mode")

    # 2. Clarification branch.
    if mode == "clarify":
        clar = (gen.get("clarification") or "").strip() or \
               "Could you narrow that down — which metric and time window?"
        history.append(question=question, sql=None, summary=clar, ok=False)
        return ClarificationNeeded(question=question, clarification=clar)

    if mode != "answer":
        raise RuntimeError(f"unexpected mode from LLM: {mode!r}")

    sql = (gen.get("sql") or "").strip()
    if not sql:
        raise RuntimeError("LLM returned mode='answer' with empty SQL")

    # 3. Validate. One retry on SQLValidationError.
    try:
        validate_sql(sql)
    except SQLValidationError as e:
        retry_user = (
            f"{question}\n\nThe SQL you returned failed validation:\n{e}\n"
            f"Please return corrected JSON."
        )
        gen = _call_claude(SYSTEM_GENERATE, retry_user)
        sql = (gen.get("sql") or "").strip()
        if not sql:
            raise SQLValidationError(f"retry returned empty SQL after: {e}") from e
        validate_sql(sql)  # raises if still bad

    # 4. Defensive row cap.
    sql_exec = _append_row_cap(sql)

    # 5. Execute. One retry on sqlite OperationalError.
    try:
        df = _execute(sql_exec, db_path)
    except sqlite3.OperationalError as e:
        retry_user = (
            f"{question}\n\nThe SQL you returned failed at execution:\n{e}\n"
            f"Previous SQL:\n{sql}\nPlease return corrected JSON."
        )
        gen = _call_claude(SYSTEM_GENERATE, retry_user)
        sql = (gen.get("sql") or "").strip()
        if not sql:
            raise AgentExecutionError(f"retry returned empty SQL after: {e}") from e
        validate_sql(sql)
        sql_exec = _append_row_cap(sql)
        try:
            df = _execute(sql_exec, db_path)
        except sqlite3.OperationalError as e2:
            raise AgentExecutionError(f"execution failed twice: {e2}") from e2

    # 6. Self-check.
    preview_rows = df.head(MAX_PREVIEW_ROWS).to_dict(orient="records")
    sc_user = json.dumps(
        {
            "question": question,
            "sql": sql,
            "row_count": int(len(df)),
            "preview_rows": preview_rows,
            "columns": list(df.columns),
        },
        default=str,
        ensure_ascii=False,
    )
    try:
        sc = _call_claude(SYSTEM_SELFCHECK, sc_user, max_tokens=512)
        self_check = SelfCheck(
            ok=bool(sc.get("ok", False)),
            reason=str(sc.get("reason", "")).strip(),
        )
        summary = str(sc.get("summary", "")).strip() or "Query executed; see results."
    except Exception as e:
        # Self-check is best-effort — never let it break the answer.
        self_check = SelfCheck(ok=False, reason=f"self-check failed: {e}")
        summary = f"Query executed and returned {len(df)} row(s)."

    # 7. Chart (None-safe).
    fig = chart.render(df)

    # 8. History.
    history.append(question=question, sql=sql, summary=summary, ok=self_check.ok)

    # 9. Return.
    return Answer(
        question=question,
        sql=sql,
        df=df,
        summary=summary,
        self_check=self_check,
        chart=fig,
    )
