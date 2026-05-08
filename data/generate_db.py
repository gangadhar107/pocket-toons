"""
data/generate_db.py
-------------------
Generates a reproducible SQLite database of synthetic PocketToons-style product
analytics data for the Analytics Q&A Agent.

Targets (fixed):
  - users:         10,000 rows
  - sessions:      50,000 rows
  - transactions:  15,000 rows
  - content_views: 50,000 rows

Indexes: (user_id) and (date) on each fact table.

Reproducibility:
  - Faker seed = 42
  - random seed = 42
  - TODAY is pinned (not datetime.today()) so reviewers see identical numbers.

Idempotency:
  - Running this script drops and recreates all tables every time. Safe to re-run.

Usage:
    python data/generate_db.py
    # writes ./analytics.db next to the project root
"""

from __future__ import annotations

import os
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from faker import Faker

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SEED = 42

# Pinned "today" — mirrored in agent/schema.py so prompts, validator, and data agree.
# Chosen to sit a few days after the assignment date so "last week" is well-defined.
TODAY = date(2026, 5, 8)
WINDOW_DAYS = 120
WINDOW_START = TODAY - timedelta(days=WINDOW_DAYS - 1)  # inclusive

N_USERS = 10_000
N_SESSIONS = 50_000
N_TRANSACTIONS = 15_000
N_CONTENT_VIEWS = 50_000

N_CONTENT_ITEMS = 500  # content_id space (1..500)

# Country distribution — weighted toward the big three for PocketFM-ish realism.
COUNTRY_WEIGHTS = [
    ("US", 35),
    ("IN", 25),
    ("BR", 12),
    ("MX", 6),
    ("ID", 6),
    ("PH", 4),
    ("GB", 3),
    ("DE", 3),
    ("FR", 2),
    ("CA", 2),
    ("AU", 2),
]
PLAN_WEIGHTS = [("free", 70), ("trial", 10), ("premium", 20)]
TXN_TYPE_WEIGHTS = [("subscription", 65), ("ppv", 30), ("refund", 5)]

DB_PATH = Path(__file__).resolve().parent.parent / "analytics.db"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def weighted_choice(rng: random.Random, pairs):
    """Pick a value from [(value, weight), ...] using the given RNG."""
    values, weights = zip(*pairs)
    return rng.choices(values, weights=weights, k=1)[0]


def random_date_in_window(rng: random.Random, start: date, end: date) -> date:
    """Uniform random date in [start, end] inclusive."""
    delta_days = (end - start).days
    return start + timedelta(days=rng.randint(0, delta_days))


def skewed_user_id(rng: random.Random, n_users: int) -> int:
    """
    Return a user id (1..n_users) with a mild power-law-ish skew so a subset
    of users are responsible for a disproportionate share of activity.
    Not a true Pareto — just enough to make DAU/retention queries interesting.
    """
    # triangular gives a natural skew toward low ids; then we occasionally
    # jump to a random user to avoid zero tail.
    if rng.random() < 0.15:
        return rng.randint(1, n_users)
    # triangular(low, high, mode) — mode near the low end biases toward it
    return int(rng.triangular(1, n_users, n_users * 0.25))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
DROP TABLE IF EXISTS content_views;
DROP TABLE IF EXISTS transactions;
DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS users;

CREATE TABLE users (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    country     TEXT    NOT NULL,
    plan        TEXT    NOT NULL CHECK (plan IN ('free', 'trial', 'premium')),
    signup_date DATE    NOT NULL
);

CREATE TABLE sessions (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    date         DATE    NOT NULL,
    duration_sec INTEGER NOT NULL
);

CREATE TABLE transactions (
    id      INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    amount  REAL    NOT NULL,
    date    DATE    NOT NULL,
    type    TEXT    NOT NULL CHECK (type IN ('subscription', 'ppv', 'refund'))
);

CREATE TABLE content_views (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    content_id INTEGER NOT NULL,
    date       DATE    NOT NULL
);
"""

INDEX_SQL = """
CREATE INDEX idx_sessions_user_id       ON sessions(user_id);
CREATE INDEX idx_sessions_date          ON sessions(date);
CREATE INDEX idx_transactions_user_id   ON transactions(user_id);
CREATE INDEX idx_transactions_date      ON transactions(date);
CREATE INDEX idx_content_views_user_id  ON content_views(user_id);
CREATE INDEX idx_content_views_date     ON content_views(date);
"""


# ---------------------------------------------------------------------------
# Row generators
# ---------------------------------------------------------------------------

def gen_users(fake: Faker, rng: random.Random):
    """Yield N_USERS tuples for the users table."""
    # Signup dates spread across a longer window so some users are "old" and
    # some "new" — required for cohort/retention questions to be meaningful.
    signup_start = WINDOW_START - timedelta(days=180)
    for i in range(1, N_USERS + 1):
        yield (
            i,
            fake.name(),
            weighted_choice(rng, COUNTRY_WEIGHTS),
            weighted_choice(rng, PLAN_WEIGHTS),
            random_date_in_window(rng, signup_start, TODAY).isoformat(),
        )


def gen_sessions(rng: random.Random):
    """Yield N_SESSIONS tuples for the sessions table."""
    for i in range(1, N_SESSIONS + 1):
        uid = skewed_user_id(rng, N_USERS)
        d = random_date_in_window(rng, WINDOW_START, TODAY).isoformat()
        # Session durations: log-normal-ish, clipped to [30s, 2h].
        duration = max(30, min(7200, int(rng.lognormvariate(mu=6.0, sigma=0.7))))
        yield (i, uid, d, duration)


def gen_transactions(rng: random.Random):
    """Yield N_TRANSACTIONS tuples for the transactions table."""
    for i in range(1, N_TRANSACTIONS + 1):
        uid = skewed_user_id(rng, N_USERS)
        d = random_date_in_window(rng, WINDOW_START, TODAY).isoformat()
        ttype = weighted_choice(rng, TXN_TYPE_WEIGHTS)
        if ttype == "subscription":
            amount = rng.choice([4.99, 9.99, 14.99, 19.99])
        elif ttype == "ppv":
            amount = round(rng.uniform(0.99, 6.99), 2)
        else:  # refund
            amount = -round(rng.uniform(0.99, 19.99), 2)
        yield (i, uid, amount, d, ttype)


def gen_content_views(rng: random.Random):
    """Yield N_CONTENT_VIEWS tuples for the content_views table."""
    for i in range(1, N_CONTENT_VIEWS + 1):
        uid = skewed_user_id(rng, N_USERS)
        cid = rng.randint(1, N_CONTENT_ITEMS)
        d = random_date_in_window(rng, WINDOW_START, TODAY).isoformat()
        yield (i, uid, cid, d)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(db_path: Path = DB_PATH) -> None:
    # Deterministic RNGs
    Faker.seed(SEED)
    rng = random.Random(SEED)
    fake = Faker()

    # Fresh DB file
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)

        # Bulk inserts in one transaction — ~400x faster than per-row commits.
        with conn:
            conn.executemany(
                "INSERT INTO users (id, name, country, plan, signup_date) "
                "VALUES (?, ?, ?, ?, ?);",
                gen_users(fake, rng),
            )
            conn.executemany(
                "INSERT INTO sessions (id, user_id, date, duration_sec) "
                "VALUES (?, ?, ?, ?);",
                gen_sessions(rng),
            )
            conn.executemany(
                "INSERT INTO transactions (id, user_id, amount, date, type) "
                "VALUES (?, ?, ?, ?, ?);",
                gen_transactions(rng),
            )
            conn.executemany(
                "INSERT INTO content_views (id, user_id, content_id, date) "
                "VALUES (?, ?, ?, ?);",
                gen_content_views(rng),
            )

        # Indexes after bulk insert (faster than maintaining during insert).
        conn.executescript(INDEX_SQL)
        conn.execute("ANALYZE;")
        conn.commit()

        # Sanity summary
        print(f"Database written: {db_path}")
        print(f"  TODAY pinned to:  {TODAY.isoformat()}")
        print(f"  Window:           {WINDOW_START.isoformat()} .. {TODAY.isoformat()} "
              f"({WINDOW_DAYS} days)")
        for table, expected in [
            ("users", N_USERS),
            ("sessions", N_SESSIONS),
            ("transactions", N_TRANSACTIONS),
            ("content_views", N_CONTENT_VIEWS),
        ]:
            (count,) = conn.execute(f"SELECT COUNT(*) FROM {table};").fetchone()
            flag = "OK" if count == expected else "MISMATCH"
            print(f"  {table:<14} {count:>8,}  (expected {expected:>8,})  [{flag}]")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
