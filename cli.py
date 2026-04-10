"""
claude-burnrate: Track your Claude session and weekly usage so you never waste limits again.
"""

import re
import typer
import json
import csv
import sqlite3
from math import floor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TextColumn
from rich import box
from rich.text import Text
from rich.columns import Columns

app = typer.Typer(
    name="claude-budget",
    help="Track Claude session + weekly usage. Never lose limits to waste again.",
    add_completion=False,
)
console = Console()

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH = Path.home() / ".claude_budget" / "usage.db"
CONFIG_PATH = Path.home() / ".claude_budget" / "config.json"

SESSION_HOURS = 5          # Claude's rolling session window
PEAK_START_PT = 5          # 5am PT
PEAK_END_PT   = 11         # 11am PT

DEFAULT_CONFIG = {
    "plan": "max_5x",      # pro | max_5x | max_20x
    "timezone_offset": -4,  # hours offset from UTC (ET=-4, PT=-7, GMT=0)
    "weekly_reset_day": None,  # ISO weekday 1=Mon..7=Sun, None = auto-detect
    "weekly_reset_time": None, # ISO string of first session start this week
}

TZ_SHORTCUTS = {
    "et": -4, "edt": -4,
    "est": -5,
    "ct": -5, "cdt": -5,
    "cst": -6,
    "mt": -6, "mdt": -6,
    "mst": -7,
    "pt": -7, "pdt": -7,
    "pst": -8,
    "gmt": 0, "utc": 0,
    "ist": 6,
}

PLAN_LABELS = {
    "pro":     "Pro",
    "max_5x":  "Max 5x  ($100/mo)",
    "max_20x": "Max 20x ($200/mo)",
}

# ── DB helpers ────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  TEXT NOT NULL,
            ended_at    TEXT,
            label       TEXT,
            task_type   TEXT,
            notes       TEXT,
            tokens_est  INTEGER DEFAULT 0,
            messages    INTEGER DEFAULT 0,
            peak_hour   INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_snapshots (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            synced_at          TEXT NOT NULL,
            session_pct_used   INTEGER,
            weekly_pct_used    INTEGER,
            session_expires_at TEXT,
            weekly_resets_at   TEXT,
            source             TEXT DEFAULT 'manual'
        )
    """)
    # Migration: add project column if missing
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN project TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    return conn


def _load_config() -> dict:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    return DEFAULT_CONFIG.copy()


def _save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_pt(dt: datetime, tz_offset: int = 0) -> datetime:
    """Convert UTC datetime to PT (UTC-7 in summer, UTC-8 in winter)."""
    pt_offset = -7  # PDT approximation
    return dt + timedelta(hours=pt_offset)


def _to_display_tz(dt: datetime, cfg: dict) -> datetime:
    """Convert UTC datetime to user's display timezone."""
    offset = cfg.get("timezone_offset", -4)
    return dt + timedelta(hours=offset)


def _tz_label(cfg: dict) -> str:
    """Return a label like 'UTC-4' for display."""
    offset = cfg.get("timezone_offset", -4)
    if offset >= 0:
        return f"UTC+{offset}"
    return f"UTC{offset}"


def _is_peak(dt_utc: datetime, tz_offset: int = 0) -> bool:
    """True if dt_utc falls in Anthropic peak hours (5am-11am PT on weekdays)."""
    pt = _to_pt(dt_utc, tz_offset)
    if pt.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return PEAK_START_PT <= pt.hour < PEAK_END_PT


def _active_session(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
    ).fetchone()


def _sessions_this_week(conn: sqlite3.Connection) -> list:
    cutoff = (_now_utc() - timedelta(days=7)).isoformat()
    return conn.execute(
        "SELECT * FROM sessions WHERE started_at > ? ORDER BY started_at ASC", (cutoff,)
    ).fetchall()


def _week_token_estimate(sessions: list) -> int:
    return sum(s["tokens_est"] for s in sessions)


def _week_messages(sessions: list) -> int:
    return sum(s["messages"] for s in sessions)


def _session_duration_hrs(session: sqlite3.Row) -> float:
    start = datetime.fromisoformat(session["started_at"])
    end_str = session["ended_at"]
    end = datetime.fromisoformat(end_str) if end_str else _now_utc()
    return (end - start).total_seconds() / 3600


def _latest_sync(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """Return the most recent sync snapshot or None."""
    return conn.execute(
        "SELECT * FROM sync_snapshots ORDER BY synced_at DESC LIMIT 1"
    ).fetchone()


def _sync_is_fresh(snapshot: Optional[sqlite3.Row]) -> bool:
    """True if snapshot exists and synced_at is within 2 hours of now."""
    if snapshot is None:
        return False
    synced_at = datetime.fromisoformat(snapshot["synced_at"])
    return (_now_utc() - synced_at).total_seconds() < 7200


# ── Plan capacity estimates ───────────────────────────────────────────────────

PLAN_WEEKLY_SESSIONS = {
    # Rough estimates based on community observations
    # Pro = ~10 sessions/week normal, Max 5x = ~50, Max 20x = ~200
    "pro":     10,
    "max_5x":  50,
    "max_20x": 200,
}

# Relative cost multipliers for estimate command (Opus ~ 5x Sonnet)
ESTIMATE_COSTS = {
    "small":  {"sonnet": 1,  "opus": 5},
    "medium": {"sonnet": 3,  "opus": 15},
    "large":  {"sonnet": 8,  "opus": 40},
}

ESTIMATE_SIZE_DESC = {
    "small":  "Quick question / clarification",
    "medium": "Concept / code review / para",
    "large":  "File review / agentic / research",
}

DEFAULT_MSG_RATE = 10  # fallback msg/hr when no history

# ── Commands ──────────────────────────────────────────────────────────────────

@app.command("start")
def start_session(
    label: str = typer.Option("", "--label", "-l", help="What you're working on"),
    task: str = typer.Option("", "--task", "-t", help="Task type (coding/research/writing/other)"),
    notes: str = typer.Option("", "--notes", "-n", help="Any notes"),
    project: str = typer.Option("", "--project", "-p", help="Project name for grouping sessions"),
):
    """Start a new Claude session and begin tracking it."""
    conn = _db()
    cfg = _load_config()

    active = _active_session(conn)
    if active:
        duration = _session_duration_hrs(active)
        remaining = max(0, SESSION_HOURS - duration)
        console.print(Panel(
            f"[yellow]Session already active:[/yellow] [bold]{active['label'] or 'unlabeled'}[/bold]\n"
            f"Started: {active['started_at'][:16]} UTC\n"
            f"Running: [cyan]{duration:.1f}h[/cyan] / {SESSION_HOURS}h  |  "
            f"[green]{remaining:.1f}h remaining[/green]\n\n"
            f"Run [bold]claude-budget end[/bold] to close it first.",
            title="⚠ Active Session", border_style="yellow"
        ))
        raise typer.Exit(1)

    now = _now_utc()
    peak = _is_peak(now, cfg.get("timezone_offset", 0))

    conn.execute(
        "INSERT INTO sessions (started_at, label, task_type, notes, peak_hour, project) VALUES (?,?,?,?,?,?)",
        (now.isoformat(), label or "unlabeled", task or "general", notes, int(peak), project or None)
    )
    conn.commit()

    peak_warning = ""
    if peak:
        peak_warning = "\n[red bold]⚡ PEAK HOURS[/red bold] (5am-11am PT) - sessions drain [bold]faster[/bold] right now. Consider waiting."

    sessions_this_week = _sessions_this_week(conn)
    plan_sessions = PLAN_WEEKLY_SESSIONS.get(cfg["plan"], 50)
    used = len(sessions_this_week)
    pct = min(100, int(used / plan_sessions * 100))
    budget_bar = _make_bar(pct, 30)

    display_time = _to_display_tz(now, cfg)
    console.print(Panel(
        f"[green bold]✓ Session started[/green bold]  [dim]{now.strftime('%Y-%m-%d %H:%M')} UTC  ({display_time.strftime('%I:%M%p')} {_tz_label(cfg)})[/dim]\n"
        f"Label: [cyan]{label or 'unlabeled'}[/cyan]  |  Task: [cyan]{task or 'general'}[/cyan]\n"
        f"Window: [bold]{SESSION_HOURS}h[/bold] rolling{peak_warning}\n\n"
        f"Weekly sessions used: {budget_bar}  {used}/{plan_sessions} est. ({pct}%)",
        title="🟢 Session Started", border_style="green"
    ))


@app.command("end")
def end_session(
    messages: int = typer.Option(0, "--messages", "-m", help="How many messages you sent"),
    tokens: int = typer.Option(0, "--tokens", "-t", help="Token estimate (rough is fine)"),
    notes: str = typer.Option("", "--notes", "-n", help="Session notes"),
):
    """End the current session and log usage."""
    conn = _db()
    active = _active_session(conn)

    if not active:
        console.print("[red]No active session. Run [bold]claude-budget start[/bold] first.[/red]")
        raise typer.Exit(1)

    now = _now_utc()
    duration = _session_duration_hrs(active)
    remaining_was = max(0, SESSION_HOURS - duration)

    conn.execute(
        "UPDATE sessions SET ended_at=?, messages=?, tokens_est=?, notes=? WHERE id=?",
        (now.isoformat(), messages, tokens, notes or active["notes"], active["id"])
    )
    conn.commit()

    efficiency = "unknown"
    if messages > 0 and duration > 0:
        msgs_per_hr = messages / duration
        efficiency = f"{msgs_per_hr:.1f} msg/hr"

    console.print(Panel(
        f"[bold]Session:[/bold] {active['label']}\n"
        f"Duration: [cyan]{duration:.2f}h[/cyan] / {SESSION_HOURS}h  |  "
        f"Remaining unused: [yellow]{remaining_was:.2f}h[/yellow]\n"
        f"Messages: [cyan]{messages}[/cyan]  |  "
        f"Tokens est: [cyan]{tokens:,}[/cyan]  |  "
        f"Efficiency: [cyan]{efficiency}[/cyan]",
        title="🔴 Session Ended", border_style="red"
    ))

    if remaining_was > 0.5:
        console.print(
            f"\n[yellow]💡 Tip:[/yellow] You had ~[bold]{remaining_was:.1f}h[/bold] left in this session window. "
            f"Next time, front-load heavier tasks to use more of each window."
        )


@app.command("status")
def status(verbose: bool = typer.Option(False, "--verbose", "-v")):
    """Show current session status and weekly budget at a glance."""
    conn = _db()
    cfg = _load_config()
    now = _now_utc()

    active = _active_session(conn)
    sessions_week = _sessions_this_week(conn)
    plan = cfg.get("plan", "max_5x")
    plan_sessions = PLAN_WEEKLY_SESSIONS.get(plan, 50)

    # ── Weekly summary ──────────────────────────────────────────
    total_sessions = len(sessions_week)
    total_msgs = _week_messages(sessions_week)
    total_tokens = _week_token_estimate(sessions_week)
    pct_used = min(100, int(total_sessions / plan_sessions * 100))

    # Wasted sessions: ended before 4h (left >1h on table)
    completed = [s for s in sessions_week if s["ended_at"]]
    wasted = [s for s in completed if _session_duration_hrs(s) < (SESSION_HOURS - 1.0)]
    wasted_hrs = sum(SESSION_HOURS - _session_duration_hrs(s) for s in wasted)

    # Peak vs off-peak
    peak_sessions = sum(1 for s in sessions_week if s["peak_hour"])
    offpeak_sessions = total_sessions - peak_sessions

    # ── Sync overlay ──────────────────────────────────────────────
    latest_sync = _latest_sync(conn)
    sync_fresh = _sync_is_fresh(latest_sync)
    sync_ago_min = None
    if sync_fresh:
        sync_ago_min = int((_now_utc() - datetime.fromisoformat(latest_sync["synced_at"])).total_seconds() / 60)

    # ── Active session panel ─────────────────────────────────────
    if active:
        dur = _session_duration_hrs(active)
        remaining = max(0, SESSION_HOURS - dur)
        pct_session = min(100, int(dur / SESSION_HOURS * 100))
        peak_now = _is_peak(now, cfg.get("timezone_offset", 0))
        peak_flag = " [red bold]⚡ PEAK[/red bold]" if peak_now else " [green]✓ off-peak[/green]"

        started_utc = datetime.fromisoformat(active['started_at'])
        started_local = _to_display_tz(started_utc, cfg)

        if sync_fresh:
            session_pct = latest_sync["session_pct_used"]
            expires_line = ""
            if latest_sync["session_expires_at"]:
                exp_dt = _to_display_tz(datetime.fromisoformat(latest_sync["session_expires_at"]), cfg)
                expires_line = f"\nExpires at: {exp_dt.strftime('%I:%M%p').lstrip('0').lower()} {_tz_label(cfg)}"
            console.print(Panel(
                f"[bold]{active['label']}[/bold]  [{active['task_type']}]{peak_flag}\n"
                f"Session: [cyan]{session_pct}%[/cyan] used (synced {sync_ago_min}m ago){expires_line}\n"
                f"[dim]Live data from Settings > Usage[/dim]",
                title="Active Session", border_style="green"
            ))
        else:
            console.print(Panel(
                f"[bold]{active['label']}[/bold]  [{active['task_type']}]{peak_flag}\n"
                f"Started: {active['started_at'][:16]} UTC  ({started_local.strftime('%I:%M%p')} {_tz_label(cfg)})\n"
                f"Elapsed: {_make_bar(pct_session, 25)} [cyan]{dur:.1f}h[/cyan] / {SESSION_HOURS}h\n"
                f"Remaining: [green bold]{remaining:.1f}h[/green bold]",
                title="Active Session", border_style="green"
            ))
    else:
        pt_now = _to_pt(now)
        peak_now = _is_peak(now, cfg.get("timezone_offset", 0))
        peak_msg = (
            f"[red]⚡ PEAK HOURS now (5am-11am PT) - sessions drain faster[/red]"
            if peak_now else
            f"[green]✓ Off-peak now - good time to start a heavy session[/green]"
        )
        console.print(Panel(
            f"No active session.\n{peak_msg}\n"
            f"PT time: {pt_now.strftime('%H:%M')}  |  "
            f"Run [bold]claude-budget start[/bold] to begin tracking.",
            title="⬜ No Active Session", border_style="dim"
        ))

    # ── Weekly budget panel ──────────────────────────────────────
    waste_line = ""
    if wasted_hrs > 0.1:
        waste_line = f"\n[red]Wasted:[/red] ~[bold]{wasted_hrs:.1f}h[/bold] across {len(wasted)} short sessions - time you paid for but didn't use"

    if sync_fresh and latest_sync["weekly_pct_used"] is not None:
        weekly_pct_synced = latest_sync["weekly_pct_used"]
        weekly_lines = (
            f"Plan: [bold]{PLAN_LABELS.get(plan, plan)}[/bold]\n"
            f"Weekly: [cyan]{weekly_pct_synced}%[/cyan] used (synced {sync_ago_min}m ago)\n"
            f"[dim]Live data from Settings > Usage[/dim]"
        )
        console.print(Panel(weekly_lines, title="Weekly Budget", border_style="blue"))
    else:
        console.print(Panel(
            f"Plan: [bold]{PLAN_LABELS.get(plan, plan)}[/bold]\n"
            f"Sessions (7d): {_make_bar(pct_used, 30)} [cyan]{total_sessions}[/cyan] / ~{plan_sessions} est.\n"
            f"Messages (7d): [cyan]{total_msgs:,}[/cyan]  |  Tokens est: [cyan]{total_tokens:,}[/cyan]\n"
            f"Peak: [red]{peak_sessions}[/red] sessions  |  Off-peak: [green]{offpeak_sessions}[/green] sessions"
            + waste_line,
            title="Weekly Budget", border_style="blue"
        ))

    if verbose:
        _show_session_table(sessions_week)


@app.command("history")
def history(
    days: int = typer.Option(7, "--days", "-d", help="How many days back to show"),
    project: str = typer.Option("", "--project", "-p", help="Filter by project name"),
):
    """Show session history."""
    conn = _db()
    cutoff = (_now_utc() - timedelta(days=days)).isoformat()

    if project:
        sessions = conn.execute(
            "SELECT * FROM sessions WHERE started_at > ? AND project = ? ORDER BY started_at DESC",
            (cutoff, project)
        ).fetchall()
    else:
        sessions = conn.execute(
            "SELECT * FROM sessions WHERE started_at > ? ORDER BY started_at DESC", (cutoff,)
        ).fetchall()

    if not sessions:
        if project:
            console.print(f"[yellow]No sessions found for project '{project}'[/yellow]")
        else:
            console.print(f"[dim]No sessions in the last {days} days.[/dim]")
        return

    title = f"Sessions - last {days} days"
    if project:
        title += f" - project: {project}"
    _show_session_table(sessions, title=title)

    if project:
        total_msgs = sum(s["messages"] or 0 for s in sessions)
        total_tokens = sum(s["tokens_est"] or 0 for s in sessions)
        console.print(
            f"\n[bold]Project total:[/bold] {len(sessions)} sessions, "
            f"{total_msgs} messages, ~{total_tokens:,} tokens est."
        )


@app.command("advice")
def advice():
    """Get personalised tips to stop wasting your weekly limit."""
    conn = _db()
    cfg = _load_config()
    sessions_week = _sessions_this_week(conn)
    now = _now_utc()

    completed = [s for s in sessions_week if s["ended_at"]]
    wasted = [s for s in completed if _session_duration_hrs(s) < (SESSION_HOURS - 1.0)]
    wasted_hrs = sum(SESSION_HOURS - _session_duration_hrs(s) for s in wasted)
    peak_sessions = sum(1 for s in sessions_week if s["peak_hour"])
    total = len(sessions_week)

    tips = []

    if wasted_hrs > 2:
        tips.append(
            f"[red]● Short sessions:[/red] You left ~{wasted_hrs:.1f}h unused across {len(wasted)} sessions. "
            "Front-load heavier tasks - start with the most token-hungry work so each 5h window is fully used."
        )

    if peak_sessions > total * 0.5 and total > 3:
        tips.append(
            f"[red]● Peak hours:[/red] {peak_sessions}/{total} sessions were during peak (5am-11am PT). "
            "Shifting heavy sessions to evenings/nights gives you the same 5h window but it drains slower."
        )

    if total == 0:
        tips.append("[dim]No sessions tracked yet. Run [bold]claude-budget start[/bold] before your next session.[/dim]")
    elif len(wasted) == 0 and peak_sessions < total * 0.3:
        tips.append("[green]✓ Solid usage pattern:[/green] You're using most of each session and avoiding peak hours.")

    plan_sessions = PLAN_WEEKLY_SESSIONS.get(cfg.get("plan", "max_5x"), 50)
    if total > plan_sessions * 0.8:
        tips.append(
            f"[yellow]● Budget warning:[/yellow] You've used ~{total}/{plan_sessions} estimated sessions this week. "
            "Prioritise high-value tasks for remaining sessions. Consider /clear between unrelated tasks instead of starting new sessions."
        )

    # General structural tips
    tips.append(
        "[blue]● Context hygiene:[/blue] Use [bold]/clear[/bold] between unrelated tasks within a session "
        "rather than ending the session - you keep your 5h window running without burning a new one."
    )
    tips.append(
        "[blue]● Batch your messages:[/blue] Group related questions into single messages. "
        "Claude's usage counts per-message, not per-token on claude.ai."
    )
    tips.append(
        "[blue]● Defer research to sub-agents:[/blue] Long research prompts balloon the main context. "
        "Use Claude Code sub-agents for research tasks to keep the main session lean."
    )

    console.print(Panel(
        "\n\n".join(tips),
        title=" Usage Advice", border_style="cyan", padding=(1, 2)
    ))


@app.command("week")
def week():
    """Project your end-of-week budget based on current burn pace."""
    conn = _db()
    cfg = _load_config()
    now = _now_utc()

    sessions = _sessions_this_week(conn)

    if not sessions:
        console.print(Panel(
            "No sessions tracked this week.\n"
            "Run [bold]claude-burnrate start[/bold] to begin tracking.",
            title="Weekly Projection", border_style="dim"
        ))
        return

    # Calculate days elapsed since oldest session this week
    oldest_start = datetime.fromisoformat(sessions[0]["started_at"])
    elapsed_days = (now - oldest_start).total_seconds() / 86400
    elapsed_days = max(elapsed_days, 0.1)  # guard against division by zero

    if elapsed_days < 2:
        console.print(Panel(
            f"Not enough data yet - only {elapsed_days:.1f} day(s) tracked.\n"
            "Check back tomorrow for a meaningful projection.",
            title="Weekly Projection", border_style="dim"
        ))
        return

    total_used = len(sessions)
    avg_per_day = total_used / elapsed_days

    # Project total by reset (7 days from first session)
    days_until_reset = max(0, 7 - elapsed_days)
    projected_total = total_used + avg_per_day * days_until_reset

    plan = cfg.get("plan", "max_5x")
    plan_ceiling = PLAN_WEEKLY_SESSIONS.get(plan, 50)
    projected_remaining = max(0, plan_ceiling - projected_total)

    pct_projected = projected_total / plan_ceiling * 100

    warning = ""
    if pct_projected > 80:
        warning = (
            f"\n[red bold]⚠ Warning:[/red bold] At this pace you'll use "
            f"~{projected_total:.0f}/{plan_ceiling} sessions ({pct_projected:.0f}%) by reset day."
        )

    console.print(Panel(
        f"[bold]Sessions used so far:[/bold] [cyan]{total_used}[/cyan]\n"
        f"[bold]Average sessions/day:[/bold] [cyan]{avg_per_day:.1f}[/cyan]\n"
        f"[bold]Projected total by reset:[/bold] [cyan]{projected_total:.0f}[/cyan] / {plan_ceiling}\n"
        f"[bold]Sessions remaining (projected):[/bold] [cyan]{projected_remaining:.0f}[/cyan]\n"
        f"[bold]Days until weekly reset:[/bold] [cyan]{days_until_reset:.1f}[/cyan]"
        + warning,
        title="Weekly Projection", border_style="blue"
    ))


@app.command("export")
def export_cmd(
    days: int = typer.Option(30, "--days", "-d", help="How many days back to export"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output CSV path"),
):
    """Export session history to a CSV file."""
    conn = _db()
    cutoff = (_now_utc() - timedelta(days=days)).isoformat()
    sessions = conn.execute(
        "SELECT * FROM sessions WHERE started_at > ? ORDER BY started_at ASC", (cutoff,)
    ).fetchall()

    if not sessions:
        console.print(f"[yellow]No sessions found in the last {days} days. Nothing to export.[/yellow]")
        return

    if output is None:
        output = f"burnrate_export_{_now_utc().strftime('%Y%m%d')}.csv"

    out_path = Path(output)
    columns = ["id", "label", "task_type", "started_at", "ended_at",
               "duration_hrs", "messages", "tokens_est", "peak_hour", "notes", "status"]

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for s in sessions:
            if s["ended_at"]:
                dur = _session_duration_hrs(s)
                dur_str = f"{dur:.2f}"
                status = "short" if dur < 4 else "done"
            else:
                dur_str = ""
                status = "active"

            writer.writerow([
                s["id"],
                s["label"] or "",
                s["task_type"] or "",
                s["started_at"],
                s["ended_at"] or "",
                dur_str,
                s["messages"] or 0,
                s["tokens_est"] or 0,
                s["peak_hour"],
                s["notes"] or "",
                status,
            ])

    # Date range for display
    first_date = sessions[0]["started_at"][:10]
    last_date = sessions[-1]["started_at"][:10]

    console.print(Panel(
        f"[green bold]Exported {len(sessions)} sessions[/green bold]\n"
        f"File: [cyan]{out_path}[/cyan]\n"
        f"Date range: {first_date} -> {last_date}",
        title="CSV Export", border_style="green"
    ))


@app.command("estimate")
def estimate(
    size: Optional[str] = typer.Option(None, "--size", "-s", help="Filter to one size: small | medium | large"),
):
    """Estimate questions remaining in the current session window."""
    conn = _db()
    cfg = _load_config()
    now = _now_utc()

    # Step 1 - time remaining
    latest_sync = _latest_sync(conn)
    sync_fresh = _sync_is_fresh(latest_sync)

    active = _active_session(conn)
    if sync_fresh and latest_sync["session_expires_at"]:
        session_expires = datetime.fromisoformat(latest_sync["session_expires_at"])
        time_remaining = max(0, (session_expires - now).total_seconds() / 3600)
        sync_ago = int((now - datetime.fromisoformat(latest_sync["synced_at"])).total_seconds() / 60)
        time_line = f"Active session: [cyan]{time_remaining:.1f}h[/cyan] remaining\n[dim]Session time from last sync ({sync_ago}m ago)[/dim]"
    elif active:
        elapsed = _session_duration_hrs(active)
        time_remaining = max(0, SESSION_HOURS - elapsed)
        time_line = f"Active session: [cyan]{time_remaining:.1f}h[/cyan] remaining"
    else:
        time_remaining = SESSION_HOURS
        time_line = f"No active session - full [cyan]{SESSION_HOURS}h[/cyan] available"

    # Step 2 - historical message rate
    sessions_week = _sessions_this_week(conn)
    completed = [s for s in sessions_week if s["ended_at"]]
    total_msgs = sum(s["messages"] for s in completed)
    total_dur = sum(_session_duration_hrs(s) for s in completed)

    if total_dur > 0 and total_msgs > 0:
        avg_msgs_per_hr = total_msgs / total_dur
        rate_line = f"Your rate: [cyan]{avg_msgs_per_hr:.1f}[/cyan] msg/hr (7-day history)"
    else:
        avg_msgs_per_hr = DEFAULT_MSG_RATE
        rate_line = f"Using default: [cyan]{DEFAULT_MSG_RATE}[/cyan] msg/hr (no history yet)"

    # Step 3 - peak hour penalty
    peak_line = ""
    if _is_peak(now, cfg.get("timezone_offset", 0)):
        effective_time = time_remaining * 0.75
        peak_line = f"\n[red bold]Peak hours[/red bold] - effective time reduced to [cyan]{effective_time:.1f}h[/cyan] (0.75x penalty)"
    else:
        effective_time = time_remaining

    # Step 4 - base capacity
    base_capacity = effective_time * avg_msgs_per_hr

    # Check if session exhausted
    if base_capacity <= 0:
        console.print(Panel(
            "Session window likely exhausted - start a fresh session.",
            title="Estimate", border_style="dim"
        ))
        return

    # Step 5 - build estimates table
    sizes_to_show = [size] if size and size in ESTIMATE_COSTS else list(ESTIMATE_COSTS.keys())

    table = Table(box=box.SIMPLE_HEAVY, show_lines=False)
    table.add_column("Size", style="bold", width=8)
    table.add_column("What it means", style="dim", min_width=30)
    table.add_column("Sonnet", justify="right", style="cyan", width=8)
    table.add_column("Opus", justify="right", style="cyan", width=8)

    for sz in sizes_to_show:
        costs = ESTIMATE_COSTS[sz]
        sonnet_est = floor(base_capacity / costs["sonnet"])
        opus_est = floor(base_capacity / costs["opus"])
        table.add_row(
            sz.capitalize(),
            ESTIMATE_SIZE_DESC[sz],
            f"~{sonnet_est}",
            f"~{opus_est}",
        )

    # Assemble output
    context = f"{time_line}\n{rate_line}{peak_line}"
    caveat = "[dim]Estimates based on your usage history. Actual limits depend on message complexity, features used, and Anthropic's capacity management.[/dim]"

    console.print(Panel(
        f"{context}\n",
        title="Estimate", border_style="cyan"
    ))
    console.print(table)
    console.print(f"\n{caveat}")


@app.command("plan")
def plan_cmd():
    """Plan your next session to avoid wasting budget on peak hours."""
    conn = _db()
    cfg = _load_config()
    now = _now_utc()

    # Step 1 — current session state
    latest_sync = _latest_sync(conn)
    sync_fresh = _sync_is_fresh(latest_sync)

    active = _active_session(conn)
    if sync_fresh and latest_sync["session_expires_at"]:
        next_window_start = datetime.fromisoformat(latest_sync["session_expires_at"])
        remaining = max(0, (next_window_start - now).total_seconds() / 3600)
        display_expires = _to_display_tz(next_window_start, cfg)
        sync_ago = int((now - datetime.fromisoformat(latest_sync["synced_at"])).total_seconds() / 60)
        state_line = (
            f"Active session: [cyan]{remaining:.1f}h[/cyan] remaining, "
            f"expires at [bold]{display_expires.strftime('%I:%M%p').lstrip('0').lower()}[/bold] ({_tz_label(cfg)})\n"
            f"[dim]Window timing from last sync ({sync_ago}m ago)[/dim]"
        )
    elif active:
        started = datetime.fromisoformat(active["started_at"])
        next_window_start = started + timedelta(hours=SESSION_HOURS)
        remaining = max(0, (next_window_start - now).total_seconds() / 3600)
        display_expires = _to_display_tz(next_window_start, cfg)
        state_line = (
            f"Active session: [cyan]{remaining:.1f}h[/cyan] remaining, "
            f"expires at [bold]{display_expires.strftime('%I:%M%p').lstrip('0').lower()}[/bold] ({_tz_label(cfg)})"
        )
    else:
        next_window_start = now
        state_line = "No active session -- next window available now"

    # Step 2 — generate 4 candidate windows
    windows = []
    for i in range(4):
        w_start = next_window_start + timedelta(hours=5 * i)
        w_end = w_start + timedelta(hours=SESSION_HOURS)
        overlap = _peak_overlap_hours(w_start, w_end)
        effective = 5.0 - (overlap * 0.25)

        if overlap == 0:
            rating, color, icon = "IDEAL", "green", "✓"
        elif overlap <= 2:
            rating, color, icon = "OK", "yellow", "~"
        else:
            rating, color, icon = "AVOID", "red", "✗"

        display_dt = _to_display_tz(w_start, cfg)
        windows.append({
            "start_utc": w_start,
            "display_dt": display_dt,
            "overlap": overlap,
            "effective": effective,
            "rating": rating,
            "color": color,
            "icon": icon,
        })

    # Step 3 — build table
    table = Table(box=box.SIMPLE_HEAVY, show_lines=False)
    table.add_column("Window", style="bold", width=8)
    table.add_column("Local Time", width=22)
    table.add_column("Effective hrs", justify="right", width=14)
    table.add_column("Rating", width=10)

    labels = ["Next", "+5h", "+10h", "+15h"]
    for label, w in zip(labels, windows):
        dt = w["display_dt"]
        # Format: "Mon 10:00pm (UTC-4)"
        time_str = f"{dt.strftime('%a %I:%M%p').lstrip('0')} ({_tz_label(cfg)})"
        table.add_row(
            label,
            time_str,
            f"{w['effective']:.1f}h",
            f"[{w['color']}]{w['rating']} {w['icon']}[/{w['color']}]",
        )

    # Step 4 — find best window
    best = None
    for w in windows:
        if w["rating"] == "IDEAL":
            best = w
            break
    if best is None:
        for w in windows:
            if w["rating"] == "OK":
                best = w
                break
    if best is None:
        best = windows[0]

    # Step 5 — recommendation
    if not active and windows[0]["rating"] == "IDEAL":
        rec_line = "[green]Start now[/green] — you're off-peak, full 5h window available."
    elif not active and windows[0]["rating"] == "AVOID":
        time_to_best = (best["start_utc"] - now).total_seconds() / 3600
        hrs = int(time_to_best)
        mins = int((time_to_best - hrs) * 60)
        lost = 5.0 - windows[0]["effective"]
        rec_line = (
            f"[yellow]Wait[/yellow] — starting now costs you ~{lost:.1f}h of effective time. "
            f"Next IDEAL window in {hrs}h {mins}m."
        )
    else:
        best_dt = best["display_dt"]
        best_time = best_dt.strftime('%a %I:%M%p').lstrip('0').lower()
        rec_line = (
            f"[green]Recommendation:[/green] Start your next session at {best_time} "
            f"— full {best['effective']:.1f}h effective window, no peak hour drain."
        )

    # Step 6 — weekly budget
    plan = cfg.get("plan", "max_5x")
    plan_ceiling = PLAN_WEEKLY_SESSIONS.get(plan, 50)
    sessions_left = max(0, plan_ceiling - len(_sessions_this_week(conn)))
    budget_line = f"Weekly budget: [cyan]{sessions_left}[/cyan] sessions remaining (estimated)"

    # Assemble panel
    console.print(Panel(
        f"{state_line}\n",
        title="Session Planner", border_style="cyan"
    ))
    console.print(table)
    console.print(f"\n{rec_line}\n")
    console.print(f"[dim]{budget_line}[/dim]")


def _peak_overlap_hours(start_utc: datetime, end_utc: datetime) -> float:
    """Calculate how many hours of [start_utc, end_utc] overlap with peak hours (5am-11am PT, weekdays only)."""
    total_overlap = 0.0
    # Iterate day by day over the window
    current = start_utc
    while current < end_utc:
        pt = _to_pt(current)
        # Only weekdays
        if pt.weekday() < 5:
            # Peak window for this day in PT
            day_start_pt = pt.replace(hour=PEAK_START_PT, minute=0, second=0, microsecond=0)
            day_end_pt = pt.replace(hour=PEAK_END_PT, minute=0, second=0, microsecond=0)
            # Convert back to UTC for comparison
            pt_offset = -7
            day_start_utc = day_start_pt - timedelta(hours=pt_offset)
            day_end_utc = day_end_pt - timedelta(hours=pt_offset)
            # Overlap with our window
            overlap_start = max(start_utc, day_start_utc)
            overlap_end = min(end_utc, day_end_utc)
            if overlap_end > overlap_start:
                total_overlap += (overlap_end - overlap_start).total_seconds() / 3600

        # Move to next day
        next_day_pt = _to_pt(current).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        current = next_day_pt - timedelta(hours=-7)  # convert PT midnight back to UTC

    return total_overlap


def _parse_resets_in(text: str) -> Optional[timedelta]:
    """Parse duration strings like '3h 47m', '47m', '3h' into timedelta."""
    m = re.match(r'^\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*$', text)
    if not m or (m.group(1) is None and m.group(2) is None):
        return None
    hours = int(m.group(1)) if m.group(1) else 0
    minutes = int(m.group(2)) if m.group(2) else 0
    td = timedelta(hours=hours, minutes=minutes)
    return td if td.total_seconds() > 0 else None


def _store_sync(conn, session_pct: int, weekly_pct: Optional[int],
                session_expires_at: Optional[datetime], weekly_resets_at: Optional[str],
                source: str):
    """Write a sync snapshot to the DB."""
    conn.execute(
        "INSERT INTO sync_snapshots (synced_at, session_pct_used, weekly_pct_used, "
        "session_expires_at, weekly_resets_at, source) VALUES (?,?,?,?,?,?)",
        (
            _now_utc().isoformat(),
            session_pct,
            weekly_pct,
            session_expires_at.isoformat() if session_expires_at else None,
            weekly_resets_at,
            source,
        )
    )
    conn.commit()


def _sync_confirmation_panel(session_pct: int, weekly_pct: Optional[int],
                              session_expires_at: Optional[datetime],
                              weekly_resets_at: Optional[str], source: str, cfg: dict):
    """Print the sync confirmation panel."""
    session_remaining = 100 - session_pct
    lines = [f"[green bold]Synced from {'pasted text' if source == 'paste' else 'manual input'}[/green bold]"]
    if session_expires_at:
        display_exp = _to_display_tz(session_expires_at, cfg)
        lines.append(f"Session: [cyan]{session_remaining}%[/cyan] remaining (expires at {display_exp.strftime('%I:%M%p').lstrip('0').lower()} {_tz_label(cfg)})")
    else:
        lines.append(f"Session: [cyan]{session_remaining}%[/cyan] remaining")
    if weekly_pct is not None:
        lines.append(f"Weekly:  [cyan]{100 - weekly_pct}%[/cyan] remaining")
    if weekly_resets_at:
        lines.append(f"Resets:  {weekly_resets_at} {_tz_label(cfg)}")
    console.print(Panel("\n".join(lines), title="Sync", border_style="green"))


@app.command("sync")
def sync_cmd(
    session: Optional[int] = typer.Option(None, "--session", "-s", help="Session percentage used (0-100)"),
    weekly: Optional[int] = typer.Option(None, "--weekly", "-w", help="Weekly percentage used (0-100)"),
    resets_in: Optional[str] = typer.Option(None, "--resets-in", "-r", help="Time until session resets e.g. '3h 47m'"),
    weekly_resets: Optional[str] = typer.Option(None, "--weekly-resets", help="When weekly limit resets e.g. 'Tue 9:00 AM'"),
):
    """Sync burnrate with real numbers from Claude's Settings > Usage page."""
    conn = _db()
    cfg = _load_config()
    now = _now_utc()

    any_flags = session is not None or weekly is not None or resets_in is not None or weekly_resets is not None

    if any_flags:
        # ── Mode A: direct input ──────────────────────────────────
        if session is not None and (session < 0 or session > 100):
            console.print("[red]--session must be 0-100[/red]")
            raise typer.Exit(1)
        if weekly is not None and (weekly < 0 or weekly > 100):
            console.print("[red]--weekly must be 0-100[/red]")
            raise typer.Exit(1)

        session_expires_at = None
        if resets_in is not None:
            td = _parse_resets_in(resets_in)
            if td is None:
                console.print("[red]Could not parse --resets-in. Use format like '3h 47m', '47m', or '3h'.[/red]")
                raise typer.Exit(1)
            session_expires_at = now + td

        _store_sync(conn, session or 0, weekly, session_expires_at, weekly_resets, "manual")
        _sync_confirmation_panel(session or 0, weekly, session_expires_at, weekly_resets, "manual", cfg)

    else:
        # ── Mode B: interactive paste ─────────────────────────────
        console.print(Panel(
            "Paste all text from the [bold]Settings > Usage[/bold] page as a single line when prompted, then press Enter.\n"
            "Or use flags instead: [bold]claude-burnrate sync --session X --weekly Y --resets-in '3h 47m'[/bold]",
            title="Sync from Claude", border_style="cyan"
        ))

        text = typer.prompt("Paste text")
        if not text.strip():
            console.print("[yellow]No text pasted. Aborting.[/yellow]")
            raise typer.Exit(0)

        # Parse session percentage
        session_matches = re.findall(r'(\d+)%\s*used', text)
        session_pct = int(session_matches[0]) if session_matches else None

        # Parse resets in
        resets_match = re.search(r'Resets in\s+(?:(\d+)\s*hr?\s*)?(?:(\d+)\s*min)?', text)
        parsed_td = None
        if resets_match and (resets_match.group(1) or resets_match.group(2)):
            hrs = int(resets_match.group(1)) if resets_match.group(1) else 0
            mins = int(resets_match.group(2)) if resets_match.group(2) else 0
            parsed_td = timedelta(hours=hrs, minutes=mins)

        # Parse weekly percentage (second occurrence)
        weekly_pct = int(session_matches[1]) if len(session_matches) > 1 else None

        # Parse weekly reset day
        weekly_resets_text = None
        weekly_resets_match = re.search(
            r'Resets\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d+:\d+\s*[AP]M)', text
        )
        if weekly_resets_match:
            weekly_resets_text = f"{weekly_resets_match.group(1)} {weekly_resets_match.group(2)}"

        # Validate required fields
        if session_pct is None:
            console.print("[yellow]Could not parse session percentage from pasted text.[/yellow]")
            console.print("Try manual input: [bold]claude-burnrate sync --session X --weekly Y --resets-in '3h 47m'[/bold]")
            raise typer.Exit(0)
        if parsed_td is None:
            console.print("[yellow]Could not parse 'Resets in' from pasted text.[/yellow]")
            console.print("Try manual input: [bold]claude-burnrate sync --session X --weekly Y --resets-in '3h 47m'[/bold]")
            raise typer.Exit(0)

        session_expires_at = now + parsed_td
        _store_sync(conn, session_pct, weekly_pct, session_expires_at, weekly_resets_text, "paste")
        _sync_confirmation_panel(session_pct, weekly_pct, session_expires_at, weekly_resets_text, "paste", cfg)


@app.command("config")
def config_cmd(
    plan: Optional[str] = typer.Option(None, "--plan", "-p", help="pro | max_5x | max_20x"),
    tz: Optional[str] = typer.Option(None, "--tz", help="UTC offset in hours (ET=-4, CT=-5, PT=-7, GMT=0) or name (et, pt, gmt, ist)"),
    show: bool = typer.Option(False, "--show", "-s", help="Show current config"),
):
    """View or update your plan config."""
    cfg = _load_config()

    if plan:
        if plan not in PLAN_LABELS:
            console.print(f"[red]Unknown plan '{plan}'. Use: pro | max_5x | max_20x[/red]")
            raise typer.Exit(1)
        cfg["plan"] = plan
        console.print(f"[green]Plan set to: {PLAN_LABELS[plan]}[/green]")

    if tz is not None:
        tz_lower = tz.strip().lower()
        if tz_lower in TZ_SHORTCUTS:
            offset = TZ_SHORTCUTS[tz_lower]
        else:
            try:
                offset = int(tz)
            except ValueError:
                valid = ", ".join(sorted(TZ_SHORTCUTS.keys()))
                console.print(f"[red]Unknown timezone '{tz}'. Use a named shortcut ({valid}) or an integer UTC offset (e.g. -4).[/red]")
                raise typer.Exit(1)
        cfg["timezone_offset"] = offset
        console.print(f"[green]Timezone set to: {_tz_label(cfg)}[/green]")

    if plan or tz is not None:
        _save_config(cfg)

    if show or (not plan and tz is None):
        label = _tz_label(cfg)
        console.print(Panel(
            f"Plan:            [cyan]{PLAN_LABELS.get(cfg['plan'], cfg['plan'])}[/cyan]\n"
            f"Timezone:        [cyan]{label}[/cyan]  (change with: claude-burnrate config --tz -5)\n"
            f"DB path:         [dim]{DB_PATH}[/dim]\n"
            f"Config path:     [dim]{CONFIG_PATH}[/dim]",
            title="⚙ Config", border_style="dim"
        ))


@app.command("reset")
def reset(confirm: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation")):
    """Wipe all session history (keeps config)."""
    if not confirm:
        typer.confirm("This will delete all session history. Continue?", abort=True)
    conn = _db()
    conn.execute("DELETE FROM sessions")
    conn.commit()
    console.print("[green]Session history cleared.[/green]")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_bar(pct: int, width: int = 20) -> str:
    filled = int(width * pct / 100)
    empty = width - filled
    color = "green" if pct < 60 else "yellow" if pct < 85 else "red"
    return f"[{color}]{'█' * filled}[/{color}][dim]{'░' * empty}[/dim]"


def _show_session_table(sessions: list, title: str = "Sessions"):
    table = Table(title=title, box=box.SIMPLE_HEAVY, show_lines=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("Label", style="cyan", max_width=20)
    table.add_column("Task", style="dim", width=10)
    table.add_column("Started", width=16)
    table.add_column("Dur (h)", justify="right", width=7)
    table.add_column("Msgs", justify="right", width=5)
    table.add_column("Tokens", justify="right", width=8)
    table.add_column("Peak", width=5)
    table.add_column("Status", width=8)

    for i, s in enumerate(sessions, 1):
        dur = _session_duration_hrs(s)
        remaining = SESSION_HOURS - dur
        status_str = (
            "[green]active[/green]" if not s["ended_at"]
            else "[red]short[/red]" if dur < SESSION_HOURS - 1.0
            else "[dim]done[/dim]"
        )
        peak_str = "[red]●[/red]" if s["peak_hour"] else "[green]✓[/green]"
        table.add_row(
            str(i),
            s["label"] or "-",
            s["task_type"] or "-",
            s["started_at"][:16],
            f"{dur:.1f}",
            str(s["messages"] or "-"),
            f"{s['tokens_est']:,}" if s["tokens_est"] else "-",
            peak_str,
            status_str,
        )

    console.print(table)


if __name__ == "__main__":
    app()
