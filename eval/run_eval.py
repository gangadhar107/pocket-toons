"""
eval/run_eval.py
----------------
Run the agent against eval/questions.jsonl and print a scorecard.

Three metrics:
  - Execution success rate  (Answer returned, no exception)
  - Clarification precision (ambiguous questions that DID trigger clarify)
  - Numeric match rate      (scalar questions whose result matches a truth SQL)

Usage (from project root):
    python eval/run_eval.py
    ANALYTICS_DB_PATH=/tmp/analytics.db python eval/run_eval.py

Cost: ~20 questions x ~$0.04 = ~$0.80 per run at claude-sonnet-4-5 list pricing.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Add project root to sys.path.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from agent.agent import ask, Answer, ClarificationNeeded, AgentExecutionError, DEFAULT_DB_PATH
from agent.validate import SQLValidationError

QUESTIONS_PATH = ROOT / "eval" / "questions.jsonl"
DEFAULT_TOLERANCE_PCT = 2.0  # ±2% default for numeric checks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    row: dict
    status: str                           # "pass" | "fail" | "error"
    reason: str = ""
    elapsed_s: float = 0.0
    agent_result: Any = None              # Answer | ClarificationNeeded | None
    numeric_agent: Optional[float] = None
    numeric_truth: Optional[float] = None


def _load_questions(path: Path) -> list[dict]:
    out: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(json.loads(line))
    return out


def _truth_scalar(sql: str, db_path: Path) -> Optional[float]:
    """Run a truth SQL against the DB and return the first cell as a float."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(sql).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    v = row[0]
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _agent_scalar(ans: Answer, value_hint: Optional[str]) -> Optional[float]:
    """Pull a scalar out of the agent's DataFrame.

    Strategy:
      1. If value_hint is given and matches a column, use that column from row 0.
      2. Else pick the first numeric column in row 0.
      3. Return None if no numeric value is present.
    """
    df = ans.df
    if df is None or df.empty:
        return None

    # Hint match
    if value_hint and value_hint in df.columns:
        try:
            return float(df.iloc[0][value_hint])
        except (TypeError, ValueError):
            return None

    # First numeric column in row 0
    for col in df.columns:
        v = df.iloc[0][col]
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _within_tolerance(agent: float, truth: float, tol_pct: float) -> bool:
    if truth == 0:
        return abs(agent) <= 0.5  # rounding slack for a "zero" truth
    return abs(agent - truth) / abs(truth) * 100.0 <= tol_pct


def _fmt(v: Optional[float]) -> str:
    if v is None:
        return "—"
    if abs(v - round(v)) < 1e-6:
        return f"{int(round(v))}"
    return f"{v:.2f}"


# ---------------------------------------------------------------------------
# Per-row evaluator
# ---------------------------------------------------------------------------

def evaluate(row: dict, db_path: Path) -> Outcome:
    t0 = time.time()
    try:
        result = ask(row["question"], db_path=db_path)
    except (SQLValidationError, AgentExecutionError) as e:
        return Outcome(row=row, status="error", reason=f"{type(e).__name__}: {e}",
                       elapsed_s=time.time() - t0)
    except Exception as e:
        return Outcome(row=row, status="error",
                       reason=f"unexpected {type(e).__name__}: {e}",
                       elapsed_s=time.time() - t0)

    dt = time.time() - t0
    check = row["check"]

    # Clarify check
    if check == "clarify":
        if isinstance(result, ClarificationNeeded):
            return Outcome(row=row, status="pass",
                           reason="clarified as expected",
                           elapsed_s=dt, agent_result=result)
        return Outcome(row=row, status="fail",
                       reason="expected clarification, got an Answer",
                       elapsed_s=dt, agent_result=result)

    # Any check that expects an Answer must have actually got one.
    if not isinstance(result, Answer):
        return Outcome(row=row, status="fail",
                       reason="expected Answer, got ClarificationNeeded",
                       elapsed_s=dt, agent_result=result)

    if check == "executes":
        return Outcome(row=row, status="pass",
                       reason=f"executed; self_check.ok={result.self_check.ok}",
                       elapsed_s=dt, agent_result=result)

    if check == "numeric":
        truth = _truth_scalar(row["truth_sql"], db_path)
        hint = row.get("value_hint")
        agent_val = _agent_scalar(result, hint)
        tol = float(row.get("tolerance_pct", DEFAULT_TOLERANCE_PCT))
        if truth is None or agent_val is None:
            return Outcome(row=row, status="fail",
                           reason=f"could not extract scalar (truth={truth}, agent={agent_val})",
                           elapsed_s=dt, agent_result=result,
                           numeric_truth=truth, numeric_agent=agent_val)
        ok = _within_tolerance(agent_val, truth, tol)
        return Outcome(
            row=row,
            status="pass" if ok else "fail",
            reason=f"agent={_fmt(agent_val)} vs truth={_fmt(truth)} "
                   f"(tol ±{tol}%)",
            elapsed_s=dt, agent_result=result,
            numeric_truth=truth, numeric_agent=agent_val,
        )

    return Outcome(row=row, status="error", reason=f"unknown check type: {check}",
                   elapsed_s=dt, agent_result=result)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    db_path = Path(os.getenv("ANALYTICS_DB_PATH", str(DEFAULT_DB_PATH)))
    if not db_path.exists():
        print(f"ERROR: db not found at {db_path}. Run `python data/generate_db.py` first.")
        return 2

    questions = _load_questions(QUESTIONS_PATH)
    print(f"Running {len(questions)} questions against {db_path} ...\n")

    results: list[Outcome] = []
    total_t = 0.0

    # Per-row table header
    print(f"{'id':<5} {'type':<10} {'check':<9} {'status':<6} {'t(s)':>5}  detail")
    print("-" * 110)
    for row in questions:
        out = evaluate(row, db_path)
        results.append(out)
        total_t += out.elapsed_s
        q_preview = row["question"] if len(row["question"]) <= 42 else row["question"][:39] + "..."
        detail = out.reason if out.status != "fail" else f"FAIL — {out.reason}"
        print(f"{row['id']:<5} {row['type']:<10} {row['check']:<9} "
              f"{out.status:<6} {out.elapsed_s:>5.1f}  {q_preview}")
        if out.status != "pass":
            print(f"{'':<40}↳ {detail}")

    # --- Scorecard ---------------------------------------------------------
    by_check = {"executes": [], "numeric": [], "clarify": []}
    for o in results:
        by_check[o.row["check"]].append(o)

    def _rate(outs: list[Outcome]) -> str:
        if not outs:
            return "n/a"
        passed = sum(1 for o in outs if o.status == "pass")
        return f"{passed}/{len(outs)} ({100 * passed / len(outs):.0f}%)"

    # Execution success = non-clarify questions that produced an Answer without error
    non_clarify = [o for o in results if o.row["check"] != "clarify"]
    exec_ok = sum(1 for o in non_clarify
                  if isinstance(o.agent_result, Answer))
    # Clarify precision = clarify questions that actually clarified
    clarify_ok = sum(1 for o in by_check["clarify"] if o.status == "pass")
    # Numeric match = numeric questions that passed
    numeric_ok = sum(1 for o in by_check["numeric"] if o.status == "pass")

    total_pass = sum(1 for o in results if o.status == "pass")

    print()
    print("=" * 78)
    print("SCORECARD")
    print("=" * 78)
    print(f"Total questions              : {len(results)}")
    print(f"Overall pass                 : {total_pass}/{len(results)} "
          f"({100 * total_pass / len(results):.0f}%)")
    print(f"  Execution success          : {exec_ok}/{len(non_clarify)} "
          f"({100 * exec_ok / max(len(non_clarify),1):.0f}%) "
          f"(Answer returned, no exception)")
    print(f"  Clarification precision    : {clarify_ok}/{len(by_check['clarify'])} "
          f"({100 * clarify_ok / max(len(by_check['clarify']),1):.0f}%) "
          f"(ambiguous Qs that clarified)")
    print(f"  Numeric match (±tolerance) : {numeric_ok}/{len(by_check['numeric'])} "
          f"({100 * numeric_ok / max(len(by_check['numeric']),1):.0f}%)")
    print(f"  Per-check pass rate        : executes={_rate(by_check['executes'])}  "
          f"numeric={_rate(by_check['numeric'])}  "
          f"clarify={_rate(by_check['clarify'])}")
    print(f"Total wall time              : {total_t:.1f}s "
          f"({total_t / max(len(results),1):.1f}s/question)")

    # Non-zero exit if anything failed, so this is CI-friendly.
    return 0 if total_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
