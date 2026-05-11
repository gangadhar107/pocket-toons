"""
ui/app.py — Streamlit UI for the Analytics Q&A Agent.

Mirrors the wireframe's information architecture (not its pixel styling):
  - Dark sidebar with logo, 4 clickable nav items, Recent list
  - Four main views, routed by st.session_state.nav:
      "ask"     → chat UI (default)
      "schema"  → full schema dump
      "history" → full question history
      "charts"  → stub (not implemented by design)
  - Topbar with three status pills
  - Chat area with user/agent bubbles, SQL block, Chart|Table tabs,
    self-check banner, and amber clarification callout
  - st.chat_input for the question

Run with:  streamlit run ui/app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `agent` importable when Streamlit is launched from the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import streamlit as st

from agent import history
from agent.agent import (
    Answer,
    ClarificationNeeded,
    AgentExecutionError,
    ask,
    DEFAULT_DB_PATH,
)
from agent.schema import ROW_COUNTS, SCHEMA, TODAY
from agent.validate import SQLValidationError

# ---------------------------------------------------------------------------
# Page config + CSS
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Analytics Q&A",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      /* --- Sidebar: dark theme ------------------------------------------- */
      [data-testid="stSidebar"] > div:first-child {
          background-color: #1D1D1F;
      }
      [data-testid="stSidebar"] * {
          color: #E0E0E0;
      }

      /* Full-width button container, no leftover margin from Streamlit defaults */
      [data-testid="stSidebar"] .stButton {
          width: 100%;
          margin: 0;
      }

      /* Flat buttons: no border, no outline, no shadow — background-only states */
      [data-testid="stSidebar"] .stButton > button,
      [data-testid="stSidebar"] .stButton > button:hover,
      [data-testid="stSidebar"] .stButton > button:focus,
      [data-testid="stSidebar"] .stButton > button:focus-visible,
      [data-testid="stSidebar"] .stButton > button:active {
          border: none !important;
          outline: none !important;
          box-shadow: none !important;
          text-align: left;
          width: 100%;
          justify-content: flex-start !important;
          font-size: 13px;
          font-weight: 400;
          padding: 7px 10px 7px 12px;
          border-radius: 6px;
          margin-bottom: 2px;
      }
      [data-testid="stSidebar"] .stButton > button {
          background-color: transparent !important;
          color: #BBB !important;
      }
      [data-testid="stSidebar"] .stButton > button:hover,
      [data-testid="stSidebar"] .stButton > button:focus,
      [data-testid="stSidebar"] .stButton > button:focus-visible,
      [data-testid="stSidebar"] .stButton > button:active {
          background-color: #2A2A2C !important;
          color: #FFF !important;
      }
      /* Center the label text inside the button (Streamlit wraps it in a <p>) */
      [data-testid="stSidebar"] .stButton > button p {
          font-size: 13px;
          font-weight: 400;
          margin: 0;
      }

      /* Active nav pill — matches button padding exactly so labels align. */
      .nav-active {
          background: #2A2A2C;
          color: #FFF;
          padding: 7px 10px 7px 12px;
          border-radius: 6px;
          font-size: 13px;
          font-weight: 500;
          margin-bottom: 2px;
          box-shadow: inset 2px 0 0 #7F77DD;
      }

      /* --- Topbar pills ------------------------------------------------- */
      .pill {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          font-size: 11px;
          padding: 3px 10px;
          margin-right: 6px;
          border-radius: 20px;
          border: 0.5px solid #D0D0D5;
          color: #555;
          background: #FAFAFB;
      }
      .pill.active {
          background: #EEEDFE;
          border-color: #AFA9EC;
          color: #3C3489;
      }

      /* --- Canonical metric pill --------------------------------------- */
      .metric-pill {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          font-size: 11px;
          padding: 3px 10px;
          margin-bottom: 8px;
          border-radius: 20px;
          background: #E0F7F1;
          border: 0.5px solid #80CBC4;
          color: #00695C;
          font-weight: 500;
      }

      /* --- SQL code block: dark + green mono ---------------------------- */
      .sql-label {
          font-size: 10px;
          color: #888;
          margin: 10px 0 4px 0;
          text-transform: uppercase;
          letter-spacing: 0.05em;
      }
      .sql-block {
          background: #1D1D1F;
          color: #9F9;
          font-family: ui-monospace, "SF Mono", Menlo, monospace;
          font-size: 12px;
          line-height: 1.55;
          padding: 10px 12px;
          border-radius: 8px;
          white-space: pre-wrap;
          word-break: break-word;
      }

      /* --- Self-check banners ------------------------------------------- */
      .selfcheck-ok,
      .selfcheck-fail {
          display: flex;
          gap: 8px;
          align-items: flex-start;
          padding: 8px 12px;
          border-radius: 6px;
          margin-top: 10px;
          font-size: 12px;
      }
      .selfcheck-ok  { background: #E1F5EE; color: #085041; }
      .selfcheck-fail{ background: #FDECEA; color: #8A1C1C; }

      /* --- Clarification bubble: amber ---------------------------------- */
      .clarify-bubble {
          background: #FAEEDA;
          border: 0.5px solid #FAC775;
          border-radius: 4px 12px 12px 12px;
          padding: 10px 14px;
          color: #633806;
          font-size: 13.5px;
      }

      /* --- Sidebar section label --------------------------------------- */
      .recent-label {
          font-size: 10px;
          color: #666;
          text-transform: uppercase;
          letter-spacing: 0.06em;
          padding: 14px 0 4px 0;
      }

      /* --- Schema view table ------------------------------------------- */
      table.schema-table, table.fallback-df, table.history-table {
          border-collapse: collapse;
          width: 100%;
          font-size: 13px;
          margin-top: 6px;
      }
      table.schema-table th, table.fallback-df th, table.history-table th {
          background: #F5F5F7;
          font-weight: 600;
          color: #1D1D1F;
          text-align: left;
          padding: 6px 10px;
          border-bottom: 0.5px solid #E5E5E7;
      }
      table.schema-table td, table.fallback-df td, table.history-table td {
          padding: 6px 10px;
          border-bottom: 0.5px solid #E5E5E7;
          text-align: left;
          vertical-align: top;
      }
      table.schema-table code {
          background: transparent;
          color: #3C3489;
      }

      section.main > div { padding-top: 1rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Session state bootstrap
# ---------------------------------------------------------------------------

if "messages" not in st.session_state:
    st.session_state.messages = []
if "pending_question" not in st.session_state:
    st.session_state.pending_question = None
if "nav" not in st.session_state:
    st.session_state.nav = "ask"


NAV_ITEMS = [
    ("ask",     "Ask a question"),
    ("schema",  "Schema explorer"),
    ("history", "Query history"),
    ("charts",  "Saved charts"),
]

NAV_TITLES = {k: v for k, v in NAV_ITEMS}


def _set_nav(key: str) -> None:
    st.session_state.nav = key


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown(
        """
        <div style="display:flex; align-items:center; gap:10px; padding:6px 0 16px 0;">
          <div style="width:28px;height:28px;background:#7F77DD;border-radius:6px;
                      display:flex;align-items:center;justify-content:center;
                      color:white;font-weight:700;font-size:14px;">AQ</div>
          <div style="font-weight:600; font-size:14px; color:#FFF;">Analytics Q&amp;A</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Nav buttons — all four are clickable; active one is rendered as a styled pill.
    for key, label in NAV_ITEMS:
        if st.session_state.nav == key:
            st.markdown(f"<div class='nav-active'>{label}</div>", unsafe_allow_html=True)
        else:
            if st.button(label, key=f"nav_{key}"):
                st.session_state.nav = key
                st.rerun()

    # Recent list stays visible across all views for quick access.
    st.markdown("<div class='recent-label'>Recent</div>", unsafe_allow_html=True)
    recent_entries = history.recent(n=10)
    if not recent_entries:
        st.markdown(
            "<div style='color:#555;font-size:12px;padding:6px 10px;'>"
            "No questions yet.</div>",
            unsafe_allow_html=True,
        )
    else:
        for i, entry in enumerate(recent_entries):
            label = entry.question if len(entry.question) <= 40 else entry.question[:37] + "…"
            if st.button(label, key=f"recent_{i}_{entry.ts}"):
                st.session_state.pending_question = entry.question
                st.session_state.nav = "ask"
                st.rerun()


# ---------------------------------------------------------------------------
# Topbar
# ---------------------------------------------------------------------------

col_title, col_pills = st.columns([3, 5])
with col_title:
    st.markdown(
        f"<div style='font-size:14px;font-weight:600;color:#1D1D1F;'>"
        f"{NAV_TITLES[st.session_state.nav]}</div>",
        unsafe_allow_html=True,
    )
with col_pills:
    st.markdown(
        f"""
        <div style="text-align:right;">
          <span class="pill active">Self-check on</span>
          <span class="pill">{DEFAULT_DB_PATH.name}</span>
          <span class="pill">{ROW_COUNTS['users']:,} users</span>
          <span class="pill">TODAY = {TODAY.isoformat()}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
st.markdown("<hr style='margin:8px 0 0 0;border:none;border-top:0.5px solid #E5E5E7;'>",
            unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_dataframe(df) -> None:
    """Render a DataFrame resiliently.

    Streamlit's st.dataframe serializes through pyarrow, which fails in
    environments with a numpy/pyarrow ABI mismatch. Fall back to a plain HTML
    table in that case.
    """
    try:
        st.dataframe(df, use_container_width=True, hide_index=True)
        return
    except Exception as e:
        st.caption(
            f"Streamlit's table renderer failed ({type(e).__name__}) — "
            "showing an HTML fallback. "
            "Fix: `pip install --upgrade numpy pyarrow` or use Python 3.11."
        )
    try:
        html = df.to_html(index=False, classes="fallback-df", border=0)
        st.markdown(html, unsafe_allow_html=True)
    except Exception:
        st.text(df.to_string(index=False) if hasattr(df, "to_string") else repr(df))


def render_user(msg: dict) -> None:
    with st.chat_message("user"):
        st.markdown(msg["content"])


def render_answer(ans: Answer) -> None:
    with st.chat_message("assistant"):
        # Canonical metric pill — shown when the answer came from the catalog.
        if getattr(ans, "metric_name", None):
            st.markdown(
                f"<div class='metric-pill'>canonical metric: {_html_escape(ans.metric_name)}</div>",
                unsafe_allow_html=True,
            )
        st.markdown(ans.summary)

        if ans.chart is not None:
            chart_tab, table_tab = st.tabs(["Chart", "Table"])
            with chart_tab:
                st.pyplot(ans.chart, clear_figure=False)
            with table_tab:
                _render_dataframe(ans.df)
        else:
            (table_tab,) = st.tabs(["Table"])
            with table_tab:
                _render_dataframe(ans.df)

        # Collapsible SQL — hidden by default, reveals on click.
        with st.expander("View SQL query", expanded=False):
            st.markdown(
                f"<div class='sql-block'>{_html_escape(ans.sql)}</div>",
                unsafe_allow_html=True,
            )

        if ans.self_check.ok:
            st.markdown(
                f"<div class='selfcheck-ok'>Self-check passed — "
                f"{_html_escape(ans.self_check.reason)}</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f"<div class='selfcheck-fail'>Self-check flagged this — "
                f"{_html_escape(ans.self_check.reason)}</div>",
                unsafe_allow_html=True,
            )


def render_clarification(text: str) -> None:
    with st.chat_message("assistant"):
        st.markdown(
            f"<div class='clarify-bubble'>{_html_escape(text)}</div>",
            unsafe_allow_html=True,
        )


def render_error(text: str) -> None:
    with st.chat_message("assistant"):
        st.error(text)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def view_ask() -> None:
    for msg in st.session_state.messages:
        if msg["role"] == "user":
            render_user(msg)
        elif msg.get("kind") == "answer":
            render_answer(msg["answer"])
        elif msg.get("kind") == "clarify":
            render_clarification(msg["clarification"])
        elif msg.get("kind") == "error":
            render_error(msg["message"])

    if st.session_state.pending_question:
        q = st.session_state.pending_question
        st.session_state.pending_question = None
        _handle_question(q)

    user_input = st.chat_input("Ask a product analytics question…")
    if user_input:
        _handle_question(user_input)


def view_schema() -> None:
    st.markdown(
        f"<p style='color:#555;font-size:13px;'>"
        f"Read-only view of the 4 tables the agent queries. "
        f"Data window: last 120 days ending TODAY = {TODAY.isoformat()}.</p>",
        unsafe_allow_html=True,
    )
    for table, cols in SCHEMA.items():
        st.markdown(
            f"<h4 style='margin-top:14px;margin-bottom:4px;'>{table} "
            f"<span style='color:#888;font-size:12px;font-weight:400;'>"
            f"({ROW_COUNTS[table]:,} rows)</span></h4>",
            unsafe_allow_html=True,
        )
        rows_html = "".join(
            f"<tr><td><code>{_html_escape(col)}</code></td>"
            f"<td><span style='color:#555;'>{_html_escape(col_type)}</span></td></tr>"
            for col, col_type in cols.items()
        )
        st.markdown(
            f"<table class='schema-table'>"
            f"<thead><tr><th>column</th><th>type</th></tr></thead>"
            f"<tbody>{rows_html}</tbody></table>",
            unsafe_allow_html=True,
        )


def view_history() -> None:
    entries = history.recent(n=100)
    st.markdown(
        f"<p style='color:#555;font-size:13px;'>"
        f"All {len(entries)} logged question(s), newest first. "
        f"Click a question to re-run it.</p>",
        unsafe_allow_html=True,
    )
    if not entries:
        st.info("No questions logged yet. Ask one from the chat view.")
        return
    for i, entry in enumerate(entries):
        badge = (
            "<span style='background:#E1F5EE;color:#085041;font-size:10px;"
            "padding:2px 6px;border-radius:4px;'>ok</span>"
            if entry.ok else
            "<span style='background:#FDECEA;color:#8A1C1C;font-size:10px;"
            "padding:2px 6px;border-radius:4px;'>check</span>"
        )
        cols = st.columns([6, 1])
        with cols[0]:
            st.markdown(
                f"<div style='padding:8px 0;border-bottom:0.5px solid #E5E5E7;'>"
                f"<div style='font-size:13px;color:#1D1D1F;'>{_html_escape(entry.question)}</div>"
                f"<div style='font-size:11px;color:#888;margin-top:2px;'>"
                f"{entry.ts[:19].replace('T',' ')} &middot; {badge} &middot; "
                f"<span style='color:#777;'>{_html_escape(entry.summary[:110])}</span></div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        with cols[1]:
            if st.button("Re-ask", key=f"hist_reask_{i}_{entry.ts}"):
                st.session_state.pending_question = entry.question
                st.session_state.nav = "ask"
                st.rerun()


def view_saved_charts() -> None:
    st.info(
        "Saved charts is a placeholder. It would store named chart snapshots "
        "from past answers and let you reopen them without re-running the SQL. "
        "Intentionally out of scope for the take-home per the plan's cut list."
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _handle_question(q: str) -> None:
    q = (q or "").strip()
    if not q:
        return
    st.session_state.messages.append({"role": "user", "content": q})
    render_user(st.session_state.messages[-1])

    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            try:
                result = ask(q)
            except SQLValidationError as e:
                msg = f"The agent produced SQL that failed validation: {e}"
                st.session_state.messages.append(
                    {"role": "assistant", "kind": "error", "message": msg}
                )
                st.error(msg)
                return
            except AgentExecutionError as e:
                msg = f"SQL executed against the database but failed: {e}"
                st.session_state.messages.append(
                    {"role": "assistant", "kind": "error", "message": msg}
                )
                st.error(msg)
                return
            except Exception as e:
                msg = f"Unexpected error: {type(e).__name__}: {e}"
                st.session_state.messages.append(
                    {"role": "assistant", "kind": "error", "message": msg}
                )
                st.error(msg)
                return

    if isinstance(result, ClarificationNeeded):
        st.session_state.messages.append(
            {"role": "assistant", "kind": "clarify", "clarification": result.clarification}
        )
        render_clarification(result.clarification)
    else:
        assert isinstance(result, Answer)
        st.session_state.messages.append(
            {"role": "assistant", "kind": "answer", "answer": result}
        )
        render_answer(result)


# ---------------------------------------------------------------------------
# Main router
# ---------------------------------------------------------------------------

VIEWS = {
    "ask":     view_ask,
    "schema":  view_schema,
    "history": view_history,
    "charts":  view_saved_charts,
}

VIEWS[st.session_state.nav]()
