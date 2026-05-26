"""SignalPipe: Travel Price Protection Dashboard

Streamlit frontend for the B2B Travel Price Protection Engine.

Run:
    streamlit run src/dashboard.py
"""

import hmac
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")

_AUTH_USERNAME = os.environ.get("DASHBOARD_USERNAME", "")
_AUTH_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
_AUTH_ENABLED  = bool(_AUTH_USERNAME and _AUTH_PASSWORD)

# ─────────────────────────────────────────────────────────────────────────────
# Page config — must be the first Streamlit call
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="SignalPipe | Travel Price Protection",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─────────────────────────────────────────────────────────────────────────────
# Custom CSS
# ─────────────────────────────────────────────────────────────────────────────

st.markdown(
    """
    <style>
        html, body, [class*="css"] { font-family: 'Inter', 'Segoe UI', sans-serif; }
        .sp-header { padding: 1.5rem 0 0.5rem 0; }
        .sp-logo {
            font-size: 2.4rem; font-weight: 800; letter-spacing: -1px;
            background: linear-gradient(90deg, #0369A1, #0EA5E9);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }
        .sp-tagline { font-size: 0.95rem; color: #6B7280; margin-top: -0.3rem; letter-spacing: 0.04em; }
        [data-testid="stMetricValue"] { font-size: 2.2rem !important; font-weight: 700 !important; }
        [data-testid="stMetricLabel"] {
            font-size: 0.8rem !important; font-weight: 600 !important;
            text-transform: uppercase; letter-spacing: 0.06em; color: #6B7280 !important;
        }
        [data-testid="stForm"] {
            background: rgba(3, 105, 161, 0.04);
            border: 1px solid rgba(3, 105, 161, 0.15);
            border-radius: 12px; padding: 1.5rem;
        }
        [data-testid="stFormSubmitButton"] button {
            background: linear-gradient(90deg, #0369A1, #0EA5E9) !important;
            color: white !important; border: none !important;
            border-radius: 8px !important; font-weight: 600 !important;
            padding: 0.5rem 1.5rem !important; transition: opacity 0.2s ease;
        }
        [data-testid="stFormSubmitButton"] button:hover { opacity: 0.88; }
        .sp-section {
            font-size: 1rem; font-weight: 700; color: #374151;
            border-left: 3px solid #0369A1; padding-left: 0.6rem;
            margin: 1.5rem 0 0.8rem 0;
        }
        .badge-ok   { color: #059669; font-weight: 700; }
        .badge-warn { color: #D97706; font-weight: 700; }
        .badge-dead { color: #DC2626; font-weight: 700; }
        [data-testid="stDataFrame"] { border-radius: 8px; overflow: hidden; }
        [data-testid="stTab"] { font-weight: 600; font-size: 0.92rem; }
        #MainMenu, footer { visibility: hidden; }
        .login-card {
            background: #ffffff; border: 1px solid #E5E7EB;
            border-radius: 16px; padding: 2.5rem 2.8rem;
            width: 100%; max-width: 400px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.07);
        }
        .login-logo {
            font-size: 2rem; font-weight: 800; letter-spacing: -1px;
            background: linear-gradient(90deg, #0369A1, #0EA5E9);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            text-align: center; margin-bottom: 0.25rem;
        }
        .login-subtitle { text-align: center; color: #6B7280; font-size: 0.88rem; margin-bottom: 1.8rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ─────────────────────────────────────────────────────────────────────────────
# Authentication gate
# ─────────────────────────────────────────────────────────────────────────────

def _credentials_match(username: str, password: str) -> bool:
    """Timing-safe comparison against env-configured credentials."""
    user_ok = hmac.compare_digest(
        username.encode(), _AUTH_USERNAME.encode()
    )
    pass_ok = hmac.compare_digest(
        password.encode(), _AUTH_PASSWORD.encode()
    )
    return user_ok and pass_ok


def _render_login_page() -> None:
    """Render the branded login card and handle form submission."""
    _, center, _ = st.columns([1, 1.6, 1])
    with center:
        st.markdown(
            '<div class="login-card">'
            '<div class="login-logo">✈️ SignalPipe</div>'
            '<div class="login-subtitle">Travel Price Protection Platform</div>'
            '</div>',
            unsafe_allow_html=True,
        )

        if st.session_state.get("_login_failed"):
            st.error("Invalid username or password. Please try again.", icon="🔒")

        with st.form("login_form", clear_on_submit=True):
            username = st.text_input("Username", placeholder="Enter username")
            password = st.text_input(
                "Password", type="password", placeholder="Enter password"
            )
            submitted = st.form_submit_button(
                "Sign In", type="primary", use_container_width=True
            )

        if submitted:
            if _credentials_match(username, password):
                st.session_state["authenticated"] = True
                st.session_state["_login_failed"] = False
                st.session_state["_auth_user"] = username
                st.rerun()
            else:
                st.session_state["_login_failed"] = True
                st.rerun()

        st.caption(
            "Access is restricted. Contact your administrator for credentials."
        )


def _require_auth() -> None:
    """Block rendering and show the login page if auth is enabled and the
    user is not yet authenticated. Call this before any app content."""
    if not _AUTH_ENABLED:
        return  # Auth disabled — open access (dev / no env vars set)

    if not st.session_state.get("authenticated"):
        _render_login_page()
        st.stop()  # Hard stop — nothing below this runs until logged in


_require_auth()

# ─────────────────────────────────────────────────────────────────────────────
# Header
# ─────────────────────────────────────────────────────────────────────────────

st.markdown(
    """
    <div class="sp-header">
        <div class="sp-logo">✈️ SignalPipe</div>
        <div class="sp-tagline">
            Travel Price Protection &nbsp;·&nbsp;
            Live Rate Monitoring &nbsp;·&nbsp;
            Savings Alert Engine
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# Data helpers
# ─────────────────────────────────────────────────────────────────────────────

def _deadline_countdown(deadline_str: str) -> str:
    """Return a human-readable countdown from an ISO deadline string."""
    try:
        dt = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt - datetime.now(timezone.utc)
        if delta.total_seconds() <= 0:
            return "⛔ EXPIRED"
        days = delta.days
        hours, rem = divmod(delta.seconds, 3600)
        mins = rem // 60
        if days > 0:
            return f"⏳ {days}d {hours}h"
        if hours > 0:
            return f"⚠️ {hours}h {mins}m"
        return f"🔴 {mins}m"
    except Exception:
        return "—"


def _savings_display(booked: float | None, current: float | None) -> str:
    """Format the savings amount and percentage between booked and current rate."""
    if booked is None or current is None:
        return "—"
    savings = booked - current
    pct = (savings / booked * 100) if booked else 0
    sign = "+" if savings >= 0 else ""
    return f"{sign}${savings:,.2f} ({sign}{pct:.1f}%)"


@st.cache_data(ttl=45, show_spinner=False)
def _fetch_bookings(status_filter: str | None = None) -> list[dict]:
    """Fetch bookings from the API, optionally filtered by status."""
    try:
        params: dict = {}
        if status_filter:
            params["status"] = status_filter
        resp = requests.get(f"{API_BASE_URL}/bookings", params=params, timeout=8)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return []


@st.cache_data(ttl=45, show_spinner=False)
def _fetch_summary_metrics() -> tuple[int, int, int]:
    """Return (total_monitoring, expiring_within_48h, savings_opportunities)."""
    bookings = _fetch_bookings("monitoring")
    total = len(bookings)
    now = datetime.now(timezone.utc)
    expiring_soon = 0
    savings_detected = 0
    for b in bookings:
        dl = b.get("cancellation_deadline")
        if dl:
            try:
                dt = datetime.fromisoformat(dl.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                secs = (dt - now).total_seconds()
                if 0 < secs <= 48 * 3600:
                    expiring_soon += 1
            except Exception:
                pass
        br = b.get("booked_rate")
        cr = b.get("current_rate")
        if br is not None and cr is not None and float(br) > float(cr):
            savings_detected += 1
    return total, expiring_soon, savings_detected


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar — connection status
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    if _AUTH_ENABLED:
        user = st.session_state.get("_auth_user", "user")
        st.markdown(f"**Signed in as** `{user}`")
        if st.button("Sign Out", type="secondary", use_container_width=True):
            st.session_state["authenticated"] = False
            st.session_state["_login_failed"] = False
            st.session_state["_auth_user"] = ""
            st.rerun()
        st.divider()

    st.markdown("### ⚙️ Connection")
    st.caption(f"**API Base URL**\n\n`{API_BASE_URL}`")

    try:
        ping = requests.get(f"{API_BASE_URL}/docs", timeout=3)
        if ping.status_code == 200:
            st.success("API Online")
        else:
            st.warning(f"API Responded {ping.status_code}")
    except Exception:
        st.error("API Offline")

    st.divider()
    st.caption(
        "Set `API_BASE_URL` environment variable to point to your "
        "deployed FastAPI endpoint."
    )

# ─────────────────────────────────────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────────────────────────────────────

tab_monitor, tab_add, tab_batch = st.tabs([
    "📋  Live Monitor",
    "➕  Add Booking",
    "📂  Batch Upload",
])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Live Monitor
# ══════════════════════════════════════════════════════════════════════════════

with tab_monitor:

    refresh_col, filter_col, _ = st.columns([1, 2, 4])
    with refresh_col:
        if st.button("↺  Refresh", type="secondary"):
            st.cache_data.clear()
    with filter_col:
        status_filter = st.selectbox(
            "Filter by status",
            ["monitoring", "ceiling_truncated", "expired_cancellation_passed", "rebooked", "— all —"],
            label_visibility="collapsed",
        )

    # ── Portfolio summary metrics ──────────────────────────────────────────────
    with st.spinner("Loading metrics…"):
        total_mon, expiring_soon, savings_detected = _fetch_summary_metrics()

    st.markdown('<div class="sp-section">Portfolio Summary</div>', unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    c1.metric(
        "Bookings Under Monitoring",
        f"{total_mon:,}",
        delta="Active",
        delta_color="off",
        help="Total bookings currently in 'monitoring' status.",
    )
    c2.metric(
        "Expiring Within 48 h",
        f"{expiring_soon:,}",
        delta="Urgent review required" if expiring_soon > 0 else "None critical",
        delta_color="inverse" if expiring_soon > 0 else "normal",
        help="Bookings whose cancellation deadline falls within the next 48 hours.",
    )
    c3.metric(
        "Savings Opportunities",
        f"{savings_detected:,}",
        delta="Price drops detected" if savings_detected > 0 else "No drops yet",
        delta_color="normal" if savings_detected > 0 else "off",
        help="Bookings where current_rate is below booked_rate.",
    )

    st.divider()

    # ── Live itineraries table ─────────────────────────────────────────────────
    st.markdown('<div class="sp-section">Live Tracked Itineraries</div>', unsafe_allow_html=True)

    chosen_status = None if status_filter == "— all —" else status_filter
    with st.spinner("Fetching bookings…"):
        bookings = _fetch_bookings(chosen_status)

    if not bookings:
        st.info("No bookings found for the selected status filter.")
    else:
        rows = []
        for b in bookings:
            br = float(b["booked_rate"]) if b.get("booked_rate") is not None else None
            cr = float(b["current_rate"]) if b.get("current_rate") is not None else None
            rows.append({
                "Booking ID":      b.get("booking_id", "—"),
                "Guest Name":      b.get("client_name", "—"),
                "Room / Class":    b.get("room_or_ticket_class", "—"),
                "Booked Rate":     br,
                "Current Rate":    cr,
                "Current Savings": _savings_display(br, cr),
                "Deadline":        _deadline_countdown(b["cancellation_deadline"])
                                   if b.get("cancellation_deadline") else "—",
                "Status":          b.get("status", "—"),
                "Provider URL":    b.get("provider_url", "—"),
            })

        df_monitor = pd.DataFrame(rows)
        st.dataframe(
            df_monitor,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Booking ID":      st.column_config.TextColumn("Booking ID", width="medium"),
                "Guest Name":      st.column_config.TextColumn("Guest Name", width="medium"),
                "Room / Class":    st.column_config.TextColumn("Room / Class", width="medium"),
                "Booked Rate":     st.column_config.NumberColumn("Booked Rate", format="$%.2f"),
                "Current Rate":    st.column_config.NumberColumn("Current Rate", format="$%.2f"),
                "Current Savings": st.column_config.TextColumn("Current Savings", width="medium"),
                "Deadline":        st.column_config.TextColumn("Cancellation In", width="medium"),
                "Status":          st.column_config.TextColumn("Status"),
                "Provider URL":    st.column_config.LinkColumn("Provider URL", display_text="Open ↗"),
            },
        )
        st.caption(f"Showing **{len(rows)}** booking(s). Data refreshes every 45 s.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Add Booking
# ══════════════════════════════════════════════════════════════════════════════

with tab_add:
    st.markdown('<div class="sp-section">Register Single Booking</div>', unsafe_allow_html=True)
    st.caption(
        "Enter the client's confirmed booking details. The engine will begin "
        "monitoring the provider URL immediately after registration."
    )

    with st.form("add_booking_form", clear_on_submit=True):
        col_a, col_b = st.columns(2)
        with col_a:
            f_booking_id  = st.text_input("Booking ID *", placeholder="HTL-2024-00123")
            f_client_name = st.text_input("Client Name *", placeholder="Jane Smith")
            f_booked_rate = st.number_input(
                "Booked Rate (USD) *", min_value=0.0, step=10.0, format="%.2f"
            )
            f_threshold   = st.number_input(
                "Savings Threshold (USD) *",
                min_value=0.0, value=50.0, step=5.0, format="%.2f",
                help="Minimum saving required to trigger a rebooking alert.",
            )
        with col_b:
            f_provider_url = st.text_input(
                "Provider URL *", placeholder="https://booking.com/hotel/..."
            )
            f_room_class   = st.text_input(
                "Room / Ticket Class *", placeholder="Superior King, Economy, etc."
            )
            f_cancel_date  = st.date_input(
                "Cancellation Deadline *",
                min_value=datetime.now(timezone.utc).date(),
                help="Last date the booking can be cancelled without penalty.",
            )

        add_submitted = st.form_submit_button(
            "➕  Register Booking", type="primary", use_container_width=True
        )

    if add_submitted:
        errors = []
        if not f_booking_id.strip():
            errors.append("Booking ID is required.")
        if not f_client_name.strip():
            errors.append("Client Name is required.")
        if not f_provider_url.strip():
            errors.append("Provider URL is required.")
        if not f_room_class.strip():
            errors.append("Room / Ticket Class is required.")
        if f_booked_rate <= 0:
            errors.append("Booked Rate must be greater than 0.")

        if errors:
            for e in errors:
                st.error(e)
        else:
            from datetime import time as _dtime
            deadline_dt = datetime.combine(f_cancel_date, _dtime.max).replace(
                tzinfo=timezone.utc
            )
            payload = {
                "booking_id":               f_booking_id.strip(),
                "client_name":              f_client_name.strip(),
                "provider_url":             f_provider_url.strip(),
                "booked_rate":              float(f_booked_rate),
                "current_rate":             None,
                "cancellation_deadline":    deadline_dt.isoformat(),
                "room_or_ticket_class":     f_room_class.strip(),
                "status":                   "monitoring",
                "target_savings_threshold": float(f_threshold),
            }
            with st.spinner("Registering booking…"):
                try:
                    resp = requests.post(
                        f"{API_BASE_URL}/bookings", json=payload, timeout=10
                    )
                    if resp.status_code in (200, 201):
                        st.success(
                            f"✅ Booking **{f_booking_id.strip()}** registered for "
                            f"**{f_client_name.strip()}**. Monitoring is now active."
                        )
                        st.cache_data.clear()
                    else:
                        st.error(
                            f"❌ API returned **{resp.status_code}**\n\n"
                            f"```\n{resp.text[:400]}\n```"
                        )
                except requests.exceptions.ConnectionError:
                    st.warning(
                        f"⚠️ Could not reach API at `{API_BASE_URL}`. "
                        "Is the server running?"
                    )
                except requests.exceptions.Timeout:
                    st.error("❌ Request timed out.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — Batch Upload
# ══════════════════════════════════════════════════════════════════════════════

with tab_batch:
    st.markdown('<div class="sp-section">Batch Manifest Upload</div>', unsafe_allow_html=True)
    st.caption(
        "Upload a standard agency CSV manifest to register multiple bookings at once. "
        "The engine validates every row before inserting into PostgreSQL."
    )

    with st.expander("📄  Required CSV Format", expanded=False):
        sample_df = pd.DataFrame([{
            "booking_id":               "HTL-2024-00123",
            "client_name":              "Jane Smith",
            "provider_url":             "https://booking.com/hotel/xyz",
            "booked_rate":              850.00,
            "room_or_ticket_class":     "Superior King",
            "cancellation_deadline":    "2024-12-20",
            "target_savings_threshold": 50.00,
        }])
        st.dataframe(sample_df, use_container_width=True, hide_index=True)
        st.caption("`target_savings_threshold` is optional — defaults to **50.00** if omitted.")
        st.download_button(
            "⬇️  Download Sample CSV",
            data=sample_df.to_csv(index=False),
            file_name="signalpipe_manifest_sample.csv",
            mime="text/csv",
        )

    REQUIRED_COLS = {
        "booking_id", "client_name", "provider_url",
        "booked_rate", "room_or_ticket_class", "cancellation_deadline",
    }

    uploaded = st.file_uploader(
        "Drop your agency manifest CSV here",
        type=["csv"],
        help="UTF-8 encoded CSV. Maximum 500 rows per upload.",
    )

    if uploaded is not None:
        try:
            df_raw = pd.read_csv(uploaded)
        except Exception as parse_err:
            st.error(f"❌ Could not parse CSV: {parse_err}")
            df_raw = None

        if df_raw is not None:
            df_raw.columns = df_raw.columns.str.strip().str.lower()
            missing_cols = REQUIRED_COLS - set(df_raw.columns)

            if missing_cols:
                st.error(
                    f"❌ Missing required columns: **{', '.join(sorted(missing_cols))}**"
                )
            else:
                valid_rows: list[dict] = []
                invalid_rows: list[dict] = []

                for idx, row in df_raw.iterrows():
                    row_errors: list[str] = []

                    bid = str(row.get("booking_id", "")).strip()
                    if not bid:
                        row_errors.append("booking_id empty")

                    client = str(row.get("client_name", "")).strip()
                    if not client:
                        row_errors.append("client_name empty")

                    url = str(row.get("provider_url", "")).strip()
                    if not url.startswith("http"):
                        row_errors.append("provider_url invalid")

                    try:
                        rate = float(row["booked_rate"])
                        if rate <= 0:
                            raise ValueError
                    except (ValueError, TypeError):
                        row_errors.append("booked_rate must be > 0")
                        rate = 0.0

                    room_class = str(row.get("room_or_ticket_class", "")).strip()
                    if not room_class:
                        row_errors.append("room_or_ticket_class empty")

                    deadline_iso: str | None = None
                    try:
                        dl = pd.to_datetime(row["cancellation_deadline"])
                        deadline_iso = (
                            dl.replace(hour=23, minute=59, second=59).isoformat()
                            + "+00:00"
                        )
                    except Exception:
                        row_errors.append("cancellation_deadline invalid date")

                    try:
                        threshold = (
                            float(row["target_savings_threshold"])
                            if "target_savings_threshold" in df_raw.columns
                            and pd.notna(row.get("target_savings_threshold"))
                            else 50.0
                        )
                    except (ValueError, TypeError):
                        threshold = 50.0

                    if row_errors:
                        invalid_rows.append({
                            "CSV row": int(idx) + 2,
                            "Errors": "; ".join(row_errors),
                        })
                    else:
                        valid_rows.append({
                            "booking_id":               bid,
                            "client_name":              client,
                            "provider_url":             url,
                            "booked_rate":              rate,
                            "current_rate":             None,
                            "cancellation_deadline":    deadline_iso,
                            "room_or_ticket_class":     room_class,
                            "status":                   "monitoring",
                            "target_savings_threshold": threshold,
                        })

                # ── Validation summary ─────────────────────────────────────
                st.markdown(
                    f"**{len(df_raw)} rows parsed** — "
                    f"✅ {len(valid_rows)} valid, "
                    + (f"❌ {len(invalid_rows)} invalid" if invalid_rows else "✅ 0 invalid")
                )

                if invalid_rows:
                    with st.expander(
                        f"⚠️  {len(invalid_rows)} invalid row(s) — click to review"
                    ):
                        st.dataframe(
                            pd.DataFrame(invalid_rows),
                            use_container_width=True,
                            hide_index=True,
                        )

                if valid_rows:
                    st.markdown(
                        '<div class="sp-section">Preview (first 5 valid rows)</div>',
                        unsafe_allow_html=True,
                    )
                    preview_cols = [
                        "booking_id", "client_name", "room_or_ticket_class",
                        "booked_rate", "cancellation_deadline",
                    ]
                    st.dataframe(
                        pd.DataFrame(valid_rows)[preview_cols].head(5),
                        use_container_width=True,
                        hide_index=True,
                    )

                    if st.button(
                        f"🚀  Insert {len(valid_rows)} Booking(s) into PostgreSQL",
                        type="primary",
                    ):
                        progress = st.progress(0, text="Starting batch insert…")
                        inserted, failed = 0, 0

                        for i, row_payload in enumerate(valid_rows):
                            try:
                                resp = requests.post(
                                    f"{API_BASE_URL}/bookings",
                                    json=row_payload,
                                    timeout=10,
                                )
                                if resp.status_code in (200, 201):
                                    inserted += 1
                                else:
                                    failed += 1
                            except Exception:
                                failed += 1

                            progress.progress(
                                (i + 1) / len(valid_rows),
                                text=f"Inserting… {i + 1} / {len(valid_rows)}",
                            )

                        progress.empty()

                        if failed == 0:
                            st.success(
                                f"✅ All **{inserted}** booking(s) registered successfully."
                            )
                            st.cache_data.clear()
                        else:
                            st.warning(
                                f"⚠️ Completed with issues: **{inserted}** inserted, "
                                f"**{failed}** failed. Check the API logs for details."
                            )


# ─────────────────────────────────────────────────────────────────────────────
# Footer
# ─────────────────────────────────────────────────────────────────────────────

st.divider()
st.caption(
    f"SignalPipe · Travel Price Protection Engine · "
    f"Data refreshes every 45 s · Last updated: "
    f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
)
