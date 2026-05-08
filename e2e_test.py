"""
End-to-end test of all 5 question types against a live Claude call
and the generated analytics.db. Prints a per-question summary with SQL,
row count, self-check verdict, and a preview.
"""
from __future__ import annotations

import os
import time
import traceback
from pathlib import Path

from dotenv import load_dotenv

# Ensure .env is read from the project root regardless of cwd.
load_dotenv(Path(__file__).resolve().parent / ".env")

from agent.agent import ask, Answer, ClarificationNeeded, AgentExecutionError
from agent.validate import SQLValidationError
from agent import history

# Use the /tmp DB built during Phase 1 smoke test.
DB = Path("/tmp/analytics.db")

QUESTIONS = [
    ("1. simple aggregate",   "What was our DAU last week?"),
    ("2. filtered aggregate", "DAU for US users last week"),
    ("3. cohort / D7",        "What is D7 retention for users who signed up 30 days ago?"),
    ("4. comparison",         "How did revenue this week compare to last week?"),
    ("5. ambiguous",          "Show me active users"),
]

# Clean history so we can see what this run writes.
history.clear()

total_t = 0.0
for label, q in QUESTIONS:
    print("=" * 78)
    print(f"{label}")
    print(f"Q: {q}")
    t0 = time.time()
    try:
        r = ask(q, db_path=DB)
    except (SQLValidationError, AgentExecutionError) as e:
        dt = time.time() - t0
        total_t += dt
        print(f"[{dt:.1f}s]  FAIL ({type(e).__name__}): {e}")
        continue
    except Exception as e:
        dt = time.time() - t0
        total_t += dt
        print(f"[{dt:.1f}s]  UNEXPECTED {type(e).__name__}: {e}")
        traceback.print_exc()
        continue
    dt = time.time() - t0
    total_t += dt

    if isinstance(r, ClarificationNeeded):
        print(f"[{dt:.1f}s]  CLARIFY: {r.clarification}")
        continue

    assert isinstance(r, Answer)
    print(f"[{dt:.1f}s]  rows={len(r.df)}  chart={'yes' if r.chart is not None else 'no'}  "
          f"self_check.ok={r.self_check.ok}")
    print(f"SQL:\n  {r.sql}")
    print(f"Summary: {r.summary}")
    print(f"Self-check reason: {r.self_check.reason}")
    if len(r.df):
        print("Preview:")
        print(r.df.head(5).to_string(index=False))

print("=" * 78)
print(f"TOTAL: {total_t:.1f}s")

print("\nHistory log after run:")
for h in history.recent(n=10):
    print(f"  {h.ts[:19]}  ok={h.ok}  q={h.question!r}")
