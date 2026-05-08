"""
agent/validate.py
-----------------
Static SQL guardrail that runs BEFORE sqlite3 touches the query.

Raises SQLValidationError (a typed exception) on any of:
  - parse failure
  - more than one statement
  - non-SELECT root expression (DROP, INSERT, UPDATE, DELETE, ATTACH, PRAGMA, ...)
  - reference to a table not in SCHEMA (allowing CTE aliases)
  - reference to a column not in SCHEMA (allowing CTE/subquery/SELECT aliases)

The agent.py retry loop catches SQLValidationError specifically and re-prompts
the LLM with the validation message, so the exception type is part of the
public contract.

Dialect is pinned to "sqlite" on every sqlglot call.
"""

from __future__ import annotations

from typing import Iterable

import sqlglot
from sqlglot import exp

from agent.schema import ALL_COLUMNS, TABLE_NAMES

DIALECT = "sqlite"


class SQLValidationError(Exception):
    """Raised when a generated SQL string fails static validation."""


# --- Internal helpers -------------------------------------------------------

def _collect_cte_names(tree: exp.Expression) -> set[str]:
    """Names introduced by WITH ... AS (...) clauses."""
    names: set[str] = set()
    for cte in tree.find_all(exp.CTE):
        alias = cte.alias_or_name
        if alias:
            names.add(alias.lower())
    return names


def _collect_table_aliases(tree: exp.Expression) -> set[str]:
    """Aliases introduced by `FROM users u` / `JOIN sessions AS s`."""
    aliases: set[str] = set()
    for t in tree.find_all(exp.Table):
        alias = t.alias
        if alias:
            aliases.add(alias.lower())
    return aliases


def _collect_output_aliases(tree: exp.Expression) -> set[str]:
    """
    Names introduced by `SELECT ... AS foo` or subquery aliases.
    These are legal column references elsewhere in the query (e.g. ORDER BY).
    """
    aliases: set[str] = set()
    for a in tree.find_all(exp.Alias):
        alias = a.alias
        if alias:
            aliases.add(alias.lower())
    return aliases


def _iter_real_tables(tree: exp.Expression, cte_names: set[str]) -> Iterable[str]:
    """Yield table names that are NOT CTE references."""
    for t in tree.find_all(exp.Table):
        name = (t.name or "").lower()
        if name and name not in cte_names:
            yield name


def _iter_real_columns(tree: exp.Expression) -> Iterable[exp.Column]:
    """Yield Column nodes that look like real column references (not aliases)."""
    for c in tree.find_all(exp.Column):
        yield c


# --- Public API -------------------------------------------------------------

def validate_sql(sql: str) -> None:
    """
    Validate `sql` or raise SQLValidationError.

    On success, returns None. On failure, raises with a message suitable for
    showing to the LLM in a retry prompt.
    """
    if not sql or not sql.strip():
        raise SQLValidationError("empty SQL string")

    # 1. Parse. sqlglot returns a list of top-level statements.
    try:
        statements = sqlglot.parse(sql, dialect=DIALECT)
    except sqlglot.errors.ParseError as e:
        raise SQLValidationError(f"SQL failed to parse: {e}") from e

    # Drop Nones that parse sometimes yields for trailing semicolons.
    statements = [s for s in statements if s is not None]
    if not statements:
        raise SQLValidationError("no parseable statement found")
    if len(statements) > 1:
        raise SQLValidationError(
            f"only one statement allowed; got {len(statements)}"
        )

    tree = statements[0]

    # 2. Must be a read-only query at the root.
    #    - exp.Select covers plain SELECT (including `WITH ... SELECT`, which
    #      sqlglot attaches as a `with` kwarg on the Select).
    #    - exp.SetOperation covers UNION / UNION ALL / INTERSECT / EXCEPT, all
    #      of which are read-only composite SELECTs.
    if not isinstance(tree, (exp.Select, exp.SetOperation)):
        kind = type(tree).__name__.upper()
        raise SQLValidationError(
            f"only SELECT queries are allowed; got {kind}"
        )

    # 3. Collect scopes that are legal to reference.
    cte_names = _collect_cte_names(tree)
    table_aliases = _collect_table_aliases(tree)
    output_aliases = _collect_output_aliases(tree)

    # 4. Table whitelist.
    unknown_tables = sorted({
        name for name in _iter_real_tables(tree, cte_names)
        if name not in TABLE_NAMES
    })
    if unknown_tables:
        raise SQLValidationError(
            f"unknown table(s): {unknown_tables}. "
            f"Allowed: {sorted(TABLE_NAMES)}"
        )

    # 5. Column whitelist (best-effort).
    #    A Column node passes if its .name is:
    #      - a known physical column in SCHEMA, OR
    #      - a CTE name (rare, but can appear in USING/ORDER BY), OR
    #      - an output alias defined via SELECT ... AS foo, OR
    #      - a table alias (e.g. `u.id` makes sqlglot emit a Column with
    #        table="u" name="id" — we check `name`, but `u` showing up as a
    #        bare Column is possible in some parse shapes).
    allowed_names = ALL_COLUMNS | cte_names | output_aliases | table_aliases
    # Lowercase for case-insensitive compare.
    allowed_names_lc = {n.lower() for n in allowed_names}

    unknown_columns: list[str] = []
    for col in _iter_real_columns(tree):
        name = (col.name or "").lower()
        if not name or name == "*":
            continue
        if name in allowed_names_lc:
            continue
        # If the column is qualified by a known table or alias, and the name
        # isn't in ALL_COLUMNS, that's still a hard fail (wrong column on a
        # real table). But if it's unqualified and not in allowed_names, also
        # fail. Either way → unknown.
        unknown_columns.append(col.sql(dialect=DIALECT))

    if unknown_columns:
        # Deduplicate while preserving order.
        seen = set()
        deduped = [c for c in unknown_columns if not (c in seen or seen.add(c))]
        raise SQLValidationError(
            f"unknown column reference(s): {deduped}. "
            f"Allowed columns across all tables: {sorted(ALL_COLUMNS)}"
        )


# ---------------------------------------------------------------------------
# Inline unit checks — run with `python -m agent.validate`
# ---------------------------------------------------------------------------

_GOOD_CASES: list[tuple[str, str]] = [
    (
        "simple aggregate",
        """
        SELECT date, COUNT(DISTINCT user_id) AS dau
        FROM sessions
        WHERE date BETWEEN '2026-04-27' AND '2026-05-03'
        GROUP BY date
        ORDER BY date
        """,
    ),
    (
        "filtered aggregate with JOIN",
        """
        SELECT s.date, COUNT(DISTINCT s.user_id) AS dau
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE u.country = 'US'
          AND s.date >= '2026-04-27'
        GROUP BY s.date
        """,
    ),
    (
        "cohort / D7 retention with CTE",
        """
        WITH cohort AS (
            SELECT id AS user_id
            FROM users
            WHERE signup_date = DATE('2026-04-08')
        )
        SELECT
            COUNT(DISTINCT c.user_id) AS cohort_size,
            COUNT(DISTINCT s.user_id) AS retained_d7
        FROM cohort c
        LEFT JOIN sessions s
          ON s.user_id = c.user_id
         AND s.date = DATE('2026-04-15')
        """,
    ),
    (
        "UNION ALL comparison (this week vs last)",
        """
        SELECT 'this_week' AS period, SUM(amount) AS revenue
        FROM transactions
        WHERE date BETWEEN '2026-05-01' AND '2026-05-07'
        UNION ALL
        SELECT 'last_week' AS period, SUM(amount) AS revenue
        FROM transactions
        WHERE date BETWEEN '2026-04-24' AND '2026-04-30'
        """,
    ),
]

_BAD_CASES: list[tuple[str, str]] = [
    ("DROP statement",            "DROP TABLE users;"),
    ("multi-statement",           "SELECT 1; SELECT 2;"),
    ("unknown table",             "SELECT * FROM orders;"),
    ("unknown column",            "SELECT favorite_color FROM users;"),
    ("garbage",                   "not sql at all !!"),
]


def _run_self_test() -> int:
    good_fails: list[str] = []
    bad_passes: list[str] = []

    print("Running validator self-test (dialect=sqlite)\n")

    print("Good cases (must PASS):")
    for label, sql in _GOOD_CASES:
        try:
            validate_sql(sql)
            print(f"  [PASS] {label}")
        except SQLValidationError as e:
            print(f"  [FAIL] {label} — unexpectedly rejected: {e}")
            good_fails.append(label)

    print("\nBad cases (must RAISE SQLValidationError):")
    for label, sql in _BAD_CASES:
        try:
            validate_sql(sql)
        except SQLValidationError as e:
            print(f"  [PASS] {label} — rejected: {str(e).splitlines()[0][:80]}")
            continue
        except Exception as e:
            print(f"  [FAIL] {label} — raised wrong type {type(e).__name__}: {e}")
            bad_passes.append(label)
            continue
        print(f"  [FAIL] {label} — unexpectedly accepted")
        bad_passes.append(label)

    print("\nSummary:")
    print(f"  good:  {len(_GOOD_CASES) - len(good_fails)}/{len(_GOOD_CASES)} passed")
    print(f"  bad:   {len(_BAD_CASES) - len(bad_passes)}/{len(_BAD_CASES)} rejected")
    return 0 if not (good_fails or bad_passes) else 1


if __name__ == "__main__":
    import sys
    sys.exit(_run_self_test())
