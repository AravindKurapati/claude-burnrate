"""
claude-burnrate: Track your Claude session and weekly usage so you never waste limits again.
"""

import typer
import json
import sqlite3
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
    "timezone_offset": 0,  # hours offset from PT (e.g. ET = +3, GMT = +8)
    "weekly_reset_day": None,  # ISO weekday 1=Mon..7=Sun, None = auto-detect
    "weekly_reset_time": None, # ISO string of first session start this week
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
    """Convert UTC datetime to PT (UTC-7 in summer, UTC-8 in winter). tz_offset adjusts for user's local."""
    pt_offset = -7  # PDT approximation
    return dt + timedelta(hours=pt_offset)


def _is_peak(dt_utc: datetime, tz_offset: int = 0) -> bool:
    """True if dt_utc falls in Anthropic peak hours (5am-11am PT)."""
    pt = _to_pt(dt_utc, tz_offset)
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


# ── Plan capacity estimates ───────────────────────────────────────────────────

PLAN_WEEKLY_SESSIONS = {
    # Rough estimates based on community observations
    # Pro = ~10 sessions/week normal, Max 5x = ~50, Max 20x = ~200
    "pro":     10,
    "max_5x":  50,
    "max_20x": 200,
}

# ── Commands ──────────────────────────────────────────────────────────────────

@app.command("start")
def start_session(
    label: str = typer.Option("", "--label", "-l", help="What you're working on"),
    task: str = typer.Option("", "--task", "-t", help="Task type (coding/research/writing/other)"),
    notes: str = typer.Option("", "--notes", "-n", help="Any notes"),
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
        "INSERT INTO sessions (started_at, label, task_type, notes, peak_hour) VALUES (?,?,?,?,?)",
        (now.isoformat(), label or "unlabeled", task or "general", notes, int(peak))
    )
    conn.commit()

    peak_warning = ""
    if peak:
        peak_warning = "\n[red bold]⚡ PEAK HOURS[/red bold] (5am-11am PT) — sessions drain [bold]faster[/bold] right now. Consider waiting."

    sessions_this_week = _sessions_this_week(conn)
    plan_sessions = PLAN_WEEKLY_SESSIONS.get(cfg["plan"], 50)
    used = len(sessions_this_week)
    pct = min(100, int(used / plan_sessions * 100))
    budget_bar = _make_bar(pct, 30)

    console.print(Panel(
        f"[green bold]✓ Session started[/green bold]  [dim]{now.strftime('%Y-%m-%d %H:%M')} UTC[/dim]\n"
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

    # ── Active session panel ─────────────────────────────────────
    if active:
        dur = _session_duration_hrs(active)
        remaining = max(0, SESSION_HOURS - dur)
        pct_session = min(100, int(dur / SESSION_HOURS * 100))
        peak_now = _is_peak(now, cfg.get("timezone_offset", 0))
        peak_flag = " [red bold]⚡ PEAK[/red bold]" if peak_now else " [green]✓ off-peak[/green]"

        console.print(Panel(
            f"[bold]{active['label']}[/bold]  [{active['task_type']}]{peak_flag}\n"
            f"Started: {active['started_at'][:16]} UTC\n"
            f"Elapsed: {_make_bar(pct_session, 25)} [cyan]{dur:.1f}h[/cyan] / {SESSION_HOURS}h\n"
            f"Remaining: [green bold]{remaining:.1f}h[/green bold]",
            title="🟢 Active Session", border_style="green"
        ))
    else:
        pt_now = _to_pt(now)
        peak_now = _is_peak(now, cfg.get("timezone_offset", 0))
        peak_msg = (
            f"[red]⚡ PEAK HOURS now (5am-11am PT) — sessions drain faster[/red]"
            if peak_now else
            f"[green]✓ Off-peak now — good time to start a heavy session[/green]"
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
        waste_line = f"\n[red]⚠ Wasted:[/red] ~[bold]{wasted_hrs:.1f}h[/bold] across {len(wasted)} short sessions — time you paid for but didn't use"

    console.print(Panel(
        f"Plan: [bold]{PLAN_LABELS.get(plan, plan)}[/bold]\n"
        f"Sessions (7d): {_make_bar(pct_used, 30)} [cyan]{total_sessions}[/cyan] / ~{plan_sessions} est.\n"
        f"Messages (7d): [cyan]{total_msgs:,}[/cyan]  |  Tokens est: [cyan]{total_tokens:,}[/cyan]\n"
        f"Peak: [red]{peak_sessions}[/red] sessions  |  Off-peak: [green]{offpeak_sessions}[/green] sessions"
        + waste_line,
        title="📊 Weekly Budget", border_style="blue"
    ))

    if verbose:
        _show_session_table(sessions_week)


@app.command("history")
def history(days: int = typer.Option(7, "--days", "-d", help="How many days back to show")):
    """Show session history."""
    conn = _db()
    cutoff = (_now_utc() - timedelta(days=days)).isoformat()
    sessions = conn.execute(
        "SELECT * FROM sessions WHERE started_at > ? ORDER BY started_at DESC", (cutoff,)
    ).fetchall()

    if not sessions:
        console.print(f"[dim]No sessions in the last {days} days.[/dim]")
        return

    _show_session_table(sessions, title=f"Sessions — last {days} days")


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
            "Front-load heavier tasks — start with the most token-hungry work so each 5h window is fully used."
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
        "rather than ending the session — you keep your 5h window running without burning a new one."
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
        title="💡 Usage Advice", border_style="cyan", padding=(1, 2)
    ))


@app.command("config")
def config_cmd(
    plan: Optional[str] = typer.Option(None, "--plan", "-p", help="pro | max_5x | max_20x"),
    tz_offset: Optional[int] = typer.Option(None, "--tz", help="Hours ahead of PT (ET=3, GMT=8, IST=13.5→14)"),
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

    if tz_offset is not None:
        cfg["timezone_offset"] = tz_offset
        console.print(f"[green]Timezone offset set to: +{tz_offset}h from PT[/green]")

    if plan or tz_offset is not None:
        _save_config(cfg)

    if show or (not plan and tz_offset is None):
        console.print(Panel(
            f"Plan:            [cyan]{PLAN_LABELS.get(cfg['plan'], cfg['plan'])}[/cyan]\n"
            f"TZ offset (PT+): [cyan]{cfg.get('timezone_offset', 0)}h[/cyan]\n"
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
        peak_str = "[red]⚡[/red]" if s["peak_hour"] else "[green]✓[/green]"
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