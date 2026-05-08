"""
agent/chart.py
--------------
Best-effort chart rendering for the agent's result DataFrame.

Contract (per PLAN.md):
  - Returns None for scalar results (1x1 or a single row). The UI is expected
    to show the Table tab only when render() returns None — no error.
  - Returns a matplotlib Figure otherwise.
  - Chooses chart type from the DataFrame shape:
      * a date-like column present  -> line chart (time series)
      * one categorical + one numeric (small N) -> bar chart
      * anything else -> None (UI falls back to Table)

Never raises. Any internal failure returns None.
"""

from __future__ import annotations

import re
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # headless-safe; Streamlit / Jupyter both render Figures
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.figure import Figure

_MAX_CATEGORICAL_BARS = 20
_DATE_COL_PATTERN = re.compile(r"^(date|day|week|month|year|.*_date|.*_at)$", re.I)


def _looks_like_date_col(series: pd.Series, name: str) -> bool:
    """True if the column is either a name match (e.g. 'date') or parses as datetime."""
    if _DATE_COL_PATTERN.match(name or ""):
        # Confirm at least the first few values parse as dates. Pass
        # format="ISO8601" to silence pandas' "Could not infer format" warning
        # — our dates come from SQLite and are always ISO-8601.
        try:
            pd.to_datetime(series.head(5), errors="raise", format="ISO8601")
            return True
        except Exception:
            return False
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    return False


def _numeric_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]


def render(df: pd.DataFrame) -> Optional[Figure]:
    """Pick a chart type from the DataFrame shape and render it.

    Returns None for scalar-ish results (single row, or no plottable columns).
    Never raises.
    """
    try:
        # Scalar guard — per PLAN.md contract.
        if df is None or df.empty:
            return None
        if len(df) <= 1:
            return None

        # --- Time series path ---------------------------------------------------
        for col in df.columns:
            if _looks_like_date_col(df[col], col):
                numeric = _numeric_columns(df)
                if not numeric:
                    return None
                sorted_df = df.copy()
                try:
                    sorted_df[col] = pd.to_datetime(sorted_df[col])
                except Exception:
                    return None
                sorted_df = sorted_df.sort_values(col)

                fig, ax = plt.subplots(figsize=(7, 3.2))
                for measure in numeric:
                    ax.plot(
                        sorted_df[col],
                        sorted_df[measure],
                        marker="o",
                        linewidth=1.8,
                        label=measure,
                    )
                ax.set_xlabel(col)
                ax.set_ylabel(numeric[0] if len(numeric) == 1 else "value")
                if len(numeric) > 1:
                    ax.legend(loc="best", fontsize=9)
                ax.grid(True, alpha=0.3)
                fig.autofmt_xdate()
                fig.tight_layout()
                return fig

        # --- Categorical bar path ----------------------------------------------
        #   Need exactly one non-numeric column + at least one numeric column,
        #   and a reasonable bar count.
        non_numeric = [c for c in df.columns if not pd.api.types.is_numeric_dtype(df[c])]
        numeric = _numeric_columns(df)
        if len(non_numeric) == 1 and numeric and len(df) <= _MAX_CATEGORICAL_BARS:
            cat_col = non_numeric[0]
            measure = numeric[0]
            fig, ax = plt.subplots(figsize=(7, 3.2))
            ax.bar(df[cat_col].astype(str), df[measure])
            ax.set_xlabel(cat_col)
            ax.set_ylabel(measure)
            ax.grid(True, axis="y", alpha=0.3)
            if len(df) > 6:
                plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
            fig.tight_layout()
            return fig

        # Anything else — no chart.
        return None
    except Exception:
        # The chart is never allowed to break the main flow.
        return None
