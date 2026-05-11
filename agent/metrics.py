"""
agent/metrics.py
----------------
Metrics catalog layer for the Analytics Q&A Agent.

Loads metrics.yaml and provides:
  - MetricsCatalog.match(question)      → MetricMatch | None
  - MetricsCatalog.render_sql(match, params) → str
  - MetricsCatalog.params_from_question(question, today) → dict

Design rules:
  - No external fuzzy-match libraries. Token overlap (Jaccard) only.
  - Never calls date.today(). Always receives TODAY from agent.schema.
  - Never raises on missing metrics.yaml — degrades to empty catalog.
  - MetricsCatalog is instantiated inside ask(), not at module level,
    so a missing pyyaml import never breaks existing tests.
"""

from __future__ import annotations

import logging
import re
import string
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Dataclasses ─────────────────────────────────────────────────────────────

@dataclass
class MetricMatch:
    """A successful match between a user question and a catalog metric."""
    metric_key:   str   # e.g. "dau"
    metric_name:  str   # e.g. "Daily Active Users"
    sql_template: str   # raw SQL with {start_date} / {end_date} / {cohort_date}
    description:  str   # human-readable definition
    grain:        str   # day | week | month | cohort | period | ranked
    score:        float # Jaccard overlap score that produced this match


@dataclass
class MetricParams:
    """Date parameters extracted from a user question."""
    start_date:   Optional[str] = None   # YYYY-MM-DD
    end_date:     Optional[str] = None   # YYYY-MM-DD
    cohort_date:  Optional[str] = None   # YYYY-MM-DD (cohort metrics only)


# ─── Constants ────────────────────────────────────────────────────────────────

# Minimum Jaccard score to accept a match.
# 0.4 filters out false positives (e.g. "Compare revenue by plan" matching
# net_revenue at 0.33) while still catching legitimate aliases.
MATCH_THRESHOLD = 0.4

# Stop-words stripped before tokenising (common words that carry no signal).
_STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "can",
    "i", "we", "our", "my", "your", "their", "its",
    "in", "on", "at", "for", "to", "of", "by", "from",
    "and", "or", "but", "not", "with", "this", "that",
    "what", "how", "show", "me", "give", "get", "tell",
    "last", "this", "past", "previous", "current", "recent",
    "week", "month", "day", "year", "today", "yesterday",
    "many", "much", "total", "number", "count",
    "days", "ago", "per", "vs", "versus",
}

# ─── Tokeniser ───────────────────────────────────────────────────────────────

def _tokenise(text: str) -> set[str]:
    """
    Lowercase, strip punctuation, split on whitespace, remove stop-words.
    Returns a set of meaningful tokens.
    """
    text = text.lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    tokens = text.split()
    return {t for t in tokens if t not in _STOP_WORDS and len(t) > 1}


def _jaccard(set_a: set[str], set_b: set[str]) -> float:
    """
    Jaccard similarity: |A ∩ B| / |A ∪ B|.
    Returns 0.0 if both sets are empty.
    """
    if not set_a and not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union else 0.0


# ─── MetricsCatalog ──────────────────────────────────────────────────────────

class MetricsCatalog:
    """
    Loads metrics.yaml and provides match/render/params helpers.

    Usage inside ask():
        catalog = MetricsCatalog()
        catalog.load()                          # safe — never raises
        match = catalog.match(question)
        if match:
            params = catalog.params_from_question(question, TODAY)
            sql    = catalog.render_sql(match, params)
    """

    def __init__(self) -> None:
        # _metrics: { key: { name, aliases, description, grain, sql } }
        self._metrics: dict[str, dict] = {}
        # _alias_tokens: { key: [ set_of_tokens_per_alias ] }
        self._alias_tokens: dict[str, list[set[str]]] = {}

    # ── Loading ───────────────────────────────────────────────────────────────

    def load(self, path: str | Path = "metrics.yaml") -> "MetricsCatalog":
        """
        Load and parse metrics.yaml.
        Returns self so callers can chain: catalog = MetricsCatalog().load()
        Never raises — logs a warning and leaves catalog empty on any error.
        """
        try:
            import yaml  # pyyaml — imported lazily so missing dep is non-fatal
        except ImportError:
            logger.warning(
                "pyyaml is not installed. Metrics catalog disabled. "
                "Run: pip install pyyaml"
            )
            return self

        path = Path(path)
        if not path.exists():
            logger.warning(
                "metrics.yaml not found at %s. "
                "Metrics catalog disabled — all questions routed to LLM.",
                path.resolve(),
            )
            return self

        try:
            with path.open("r", encoding="utf-8") as f:
                raw: dict = yaml.safe_load(f) or {}
        except Exception as exc:
            logger.warning("Failed to parse metrics.yaml: %s", exc)
            return self

        for key, defn in raw.items():
            if not isinstance(defn, dict):
                continue
            required = {"name", "aliases", "sql", "grain", "description"}
            if not required.issubset(defn):
                logger.warning("Metric %r missing fields %s — skipped.", key, required - defn.keys())
                continue

            self._metrics[key] = defn
            # Pre-tokenise every alias for fast matching at query time
            self._alias_tokens[key] = [
                _tokenise(alias) for alias in defn["aliases"]
            ]

        logger.debug("Metrics catalog loaded: %d metrics.", len(self._metrics))
        return self

    # ── Matching ──────────────────────────────────────────────────────────────

    def match(self, question: str) -> Optional[MetricMatch]:
        """
        Fuzzy-match question against all metric aliases using Jaccard overlap.

        Returns the best MetricMatch if score >= MATCH_THRESHOLD, else None.

        How it works:
          1. Tokenise the question (lowercase, strip stop-words, strip punct).
          2. For each metric, compute Jaccard(question_tokens, alias_tokens)
             for every alias. Take the max score for that metric.
          3. Return the metric with the highest score above the threshold.
          4. On a tie (rare), prefer the metric whose name appears verbatim
             in the question.

        Intentional non-matches:
          "Which country has the most users?" → None
          "How many users signed up?" → new_signups (not dau — "signed up"
             overlaps with new_signups aliases, not dau aliases)
        """
        if not self._metrics:
            return None

        question_tokens = _tokenise(question)
        if not question_tokens:
            return None

        best_key:   Optional[str]   = None
        best_score: float           = 0.0
        scores: dict[str, float]    = {}

        q_lower = question.lower()
        for key, alias_token_sets in self._alias_tokens.items():
            # Score = max Jaccard across all aliases for this metric
            metric_score = max(
                _jaccard(question_tokens, alias_tokens)
                for alias_tokens in alias_token_sets
            )
            # Exact-key bonus: if the metric key (e.g. "arpu", "d7", "wau")
            # appears as a whole word in the question, this is a very strong
            # signal — boost the score to guarantee it wins over noise.
            if re.search(rf"\b{re.escape(key)}\b", q_lower):
                metric_score = max(metric_score, 1.0)
            scores[key] = metric_score
            if metric_score > best_score:
                best_score = metric_score
                best_key   = key

        if best_key is None or best_score < MATCH_THRESHOLD:
            logger.debug(
                "No metric match for question (best=%.3f < threshold=%.3f): %r",
                best_score, MATCH_THRESHOLD, question,
            )
            return None

        # Tie-detection: if multiple metrics share the best score,
        # the question might be ambiguous (e.g. "active users" matches
        # DAU, WAU, and MAU equally).
        # Before giving up, try to break the tie:
        #   1. Check if any tied metric's key appears as a word in the question
        #      (e.g. "d7" in "D7 retention" → pick d7_retention).
        #   2. Check if any tied metric's name word appears in the question
        #      (e.g. "daily" in "daily active users" → pick DAU).
        # If no tiebreaker is found, return None → LLM will clarify.
        tied_keys = [k for k, s in scores.items() if s == best_score]
        if len(tied_keys) > 1:
            # Attempt tiebreaker 1: metric key substring (e.g. "d7", "arpu")
            key_matches = [
                k for k in tied_keys
                if re.search(rf"\b{re.escape(k)}\b", q_lower)
                or re.search(rf"\b{re.escape(k.split('_')[0])}\b", q_lower)
            ]
            if len(key_matches) == 1:
                best_key = key_matches[0]
            else:
                # Attempt tiebreaker 2: metric name words in question
                name_matches = []
                for k in tied_keys:
                    name_words = set(self._metrics[k]["name"].lower().split())
                    differentiators = name_words - {"users", "active", "retention",
                                                    "rate", "content", "views", "new"}
                    if differentiators & set(q_lower.split()):
                        name_matches.append(k)
                if len(name_matches) == 1:
                    best_key = name_matches[0]
                else:
                    logger.debug(
                        "Ambiguous match — %d metrics tied at %.3f for %r: %s. "
                        "Falling through to LLM for clarification.",
                        len(tied_keys), best_score, question, tied_keys,
                    )
                    return None

        defn = self._metrics[best_key]
        logger.debug(
            "Metric matched: %r → %r (score=%.3f)",
            question, best_key, best_score,
        )
        return MetricMatch(
            metric_key   = best_key,
            metric_name  = defn["name"],
            sql_template = defn["sql"].strip(),
            description  = defn["description"].strip(),
            grain        = defn["grain"],
            score        = best_score,
        )

    # ── SQL rendering ─────────────────────────────────────────────────────────

    def render_sql(
        self,
        match: MetricMatch,
        params: dict[str, str],
    ) -> str:
        """
        Substitute date placeholders in the SQL template.

        Placeholders:
          {start_date}   → 'YYYY-MM-DD'  (wrapped in SQL single quotes)
          {end_date}     → 'YYYY-MM-DD'
          {cohort_date}  → 'YYYY-MM-DD'  (cohort grain only)

        Raises ValueError if a placeholder appears in the template
        but the corresponding param is missing.

        Note: placeholders are substituted as SQL string literals wrapped
        in single quotes. E.g. WHERE date BETWEEN {start_date} AND {end_date}
        becomes WHERE date BETWEEN '2026-04-01' AND '2026-05-08'.
        This is necessary because SQLite interprets unquoted 2026-04-01 as
        arithmetic (2026 - 4 - 1 = 2021).
        """
        sql = match.sql_template

        # Find all placeholders in the template
        placeholders = set(re.findall(r"\{(\w+)\}", sql))

        for ph in placeholders:
            if ph not in params or params[ph] is None:
                raise ValueError(
                    f"Metric '{match.metric_key}' requires parameter "
                    f"'{{{ph}}}' but it was not resolved from the question. "
                    f"Available params: {list(params.keys())}"
                )
            # Wrap the date value in SQL single quotes.
            sql = sql.replace(f"{{{ph}}}", f"'{params[ph]}'")

        return sql

    # ── Date parameter extraction ─────────────────────────────────────────────

    def params_from_question(
        self,
        question: str,
        today: date,
    ) -> dict[str, str]:
        """
        Extract date parameters from a natural-language question.

        Rules (first match wins, case-insensitive):
          "last week"           → start=today-7,  end=today-1
          "this week"           → start=today-6,  end=today
          "last 7 days"         → start=today-7,  end=today-1
          "last 30 days"        → start=today-30, end=today-1
          "last month"          → start=today-30, end=today-1
          "last N days"         → start=today-N,  end=today-1
          "this month"          → start=today-30, end=today
          "yesterday"           → start=today-1,  end=today-1
          "today"               → start=today,    end=today
          "N days ago"          → cohort_date=today-N
          "30 days ago"         → cohort_date=today-30
          "<N> day(s) ago"      → cohort_date=today-N
          no time mention       → start=today-30, end=today-1  (default)

        Always uses the pinned TODAY constant, never date.today().
        Returns a dict with whatever params were resolved.
        The render_sql() method will raise if a required one is missing.
        """
        q = question.lower()

        def fmt(d: date) -> str:
            return d.isoformat()

        # ── Cohort patterns (must check before generic "N days" patterns) ──
        cohort_match = re.search(r"(\d+)\s+days?\s+ago", q)
        if cohort_match:
            n = int(cohort_match.group(1))
            return {"cohort_date": fmt(today - timedelta(days=n))}

        if "yesterday" in q and "cohort" in q:
            return {"cohort_date": fmt(today - timedelta(days=1))}

        # ── Range patterns ────────────────────────────────────────────────
        if "last week" in q or "previous week" in q:
            return {
                "start_date": fmt(today - timedelta(days=7)),
                "end_date":   fmt(today - timedelta(days=1)),
            }

        if "this week" in q or "current week" in q:
            return {
                "start_date": fmt(today - timedelta(days=6)),
                "end_date":   fmt(today),
            }

        # "last N days" — e.g. "last 14 days", "last 90 days"
        n_days_match = re.search(r"last\s+(\d+)\s+days?", q)
        if n_days_match:
            n = int(n_days_match.group(1))
            return {
                "start_date": fmt(today - timedelta(days=n)),
                "end_date":   fmt(today - timedelta(days=1)),
            }

        if "last month" in q or "previous month" in q or "last 30 days" in q:
            return {
                "start_date": fmt(today - timedelta(days=30)),
                "end_date":   fmt(today - timedelta(days=1)),
            }

        if "this month" in q or "current month" in q:
            return {
                "start_date": fmt(today - timedelta(days=30)),
                "end_date":   fmt(today),
            }

        if "yesterday" in q:
            return {
                "start_date": fmt(today - timedelta(days=1)),
                "end_date":   fmt(today - timedelta(days=1)),
            }

        if "today" in q:
            return {
                "start_date": fmt(today),
                "end_date":   fmt(today),
            }

        # ── Default: last 30 days ─────────────────────────────────────────
        logger.debug(
            "No time window found in question — defaulting to last 30 days."
        )
        return {
            "start_date": fmt(today - timedelta(days=30)),
            "end_date":   fmt(today - timedelta(days=1)),
        }

    # ── Introspection helpers ─────────────────────────────────────────────────

    def list_metrics(self) -> list[dict]:
        """
        Return a summary of all loaded metrics.
        Used by the Streamlit schema explorer sidebar.
        """
        return [
            {
                "key":         key,
                "name":        defn["name"],
                "grain":       defn["grain"],
                "description": defn["description"].strip(),
                "aliases":     defn["aliases"],
            }
            for key, defn in self._metrics.items()
        ]

    def is_empty(self) -> bool:
        """True if no metrics were loaded (yaml missing or parse failed)."""
        return len(self._metrics) == 0

    def __len__(self) -> int:
        return len(self._metrics)

    def __repr__(self) -> str:
        return f"MetricsCatalog({len(self._metrics)} metrics)"


# ─── Self-test ────────────────────────────────────────────────────────────────
# Run with: python -m agent.metrics
# Expects metrics.yaml in the current working directory.

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s  %(message)s")

    # Import TODAY from schema (mirrors how agent.py uses it)
    try:
        from agent.schema import TODAY
    except ImportError:
        from datetime import date as _date
        TODAY = _date(2026, 5, 8)
        print(f"(agent.schema not found — using hardcoded TODAY={TODAY})\n")

    catalog = MetricsCatalog().load()
    if catalog.is_empty():
        print("ERROR: catalog is empty. Is metrics.yaml in the current directory?")
        sys.exit(1)

    print(f"Loaded {len(catalog)} metrics.\n")

    # ── Match tests ───────────────────────────────────────────────────────────
    MATCH_CASES: list[tuple[str, Optional[str]]] = [
        # (question,                              expected_metric_key or None)
        ("What was our DAU last week?",           "dau"),
        ("Show me daily active users",            "dau"),
        ("How many users were active yesterday?", None),  # ambiguous: ties DAU/WAU/MAU
        ("Weekly active users this month",        "wau"),
        ("Monthly active users last 30 days",     "mau"),
        ("Average session duration last week",    "avg_session_duration"),
        ("How long are sessions on average?",     "avg_session_duration"),
        ("What was our revenue last week?",       "net_revenue"),
        ("Total earnings this month",             "net_revenue"),
        ("Show me ARPU for last 30 days",         "arpu"),
        ("What is the refund rate this month?",   "refund_rate"),
        ("How many users signed up last week?",   "new_signups"),
        ("User signups yesterday",                "new_signups"),
        ("Plan distribution",                     "plan_distribution"),
        ("Free vs premium breakdown",             "plan_distribution"),
        ("Daily content views last week",         "daily_content_views"),
        ("Show me popular content this month",    "top_content"),
        ("What content is trending?",             "top_content"),
        ("D7 retention for 30 days ago cohort",   "d7_retention"),
        ("Day 7 retention",                       None),  # ambiguous: ties d7/d30
        ("D30 retention",                         "d30_retention"),
        ("Monthly retention for 60 days ago",     "d30_retention"),
        # Should NOT match any metric:
        ("Which country has the most users?",     None),
        ("Compare revenue by plan",               None),
        ("Show me users from India",              None),
    ]

    passed = 0
    failed = 0

    print("Match tests:")
    print(f"  {'Question':<48} {'Expected':<22} {'Got':<22} {'Score':<7} Result")
    print("  " + "-" * 108)

    for question, expected_key in MATCH_CASES:
        result = catalog.match(question)
        got_key = result.metric_key if result else None
        score   = f"{result.score:.3f}" if result else "—"
        ok      = got_key == expected_key
        symbol  = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        print(
            f"  {question:<48} "
            f"{(expected_key or 'None'):<22} "
            f"{(got_key or 'None'):<22} "
            f"{score:<7} "
            f"{symbol}"
        )

    print(f"\nMatch summary: {passed}/{passed + failed} passed")

    # ── Param extraction tests ────────────────────────────────────────────────
    print("\nParam extraction tests:")
    PARAM_CASES = [
        ("last week",                      {"start_date": "2026-05-01", "end_date": "2026-05-07"}),
        ("this week",                      {"start_date": "2026-05-02", "end_date": "2026-05-08"}),
        ("last 7 days",                    {"start_date": "2026-05-01", "end_date": "2026-05-07"}),
        ("last 30 days",                   {"start_date": "2026-04-08", "end_date": "2026-05-07"}),
        ("yesterday",                      {"start_date": "2026-05-07", "end_date": "2026-05-07"}),
        ("30 days ago",                    {"cohort_date": "2026-04-08"}),
        ("60 days ago cohort",             {"cohort_date": "2026-03-09"}),
        ("no time window mentioned here",  {"start_date": "2026-04-08", "end_date": "2026-05-07"}),
    ]

    param_passed = 0
    param_failed = 0
    for question, expected_params in PARAM_CASES:
        got = catalog.params_from_question(question, TODAY)
        ok  = got == expected_params
        symbol = "PASS" if ok else "FAIL"
        if ok:
            param_passed += 1
        else:
            param_failed += 1
        print(f"  {symbol}  {question!r:<42}  got={got}")
        if not ok:
            print(f"        expected={expected_params}")

    print(f"\nParam summary: {param_passed}/{param_passed + param_failed} passed")

    # ── Render test ───────────────────────────────────────────────────────────
    print("\nRender test (DAU last week):")
    m = catalog.match("What was our DAU last week?")
    if m:
        params = catalog.params_from_question("What was our DAU last week?", TODAY)
        sql = catalog.render_sql(m, params)
        print(f"  Metric : {m.metric_name}")
        print(f"  Grain  : {m.grain}")
        print(f"  Params : {params}")
        print(f"  SQL    :\n")
        for line in sql.splitlines():
            print(f"    {line}")

    total_pass = passed + param_passed
    total_fail = failed + param_failed
    total      = total_pass + total_fail
    print(f"\nOverall: {total_pass}/{total} passed")
    sys.exit(0 if total_fail == 0 else 1)
