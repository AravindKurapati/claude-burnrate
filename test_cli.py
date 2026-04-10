"""Tests for claude-burnrate CLI commands."""

import sqlite3
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
from typer.testing import CliRunner

# Patch DB and config paths before importing cli
import cli as cli_mod

runner = CliRunner()


def _setup_test_db(tmp_path: Path):
    """Redirect DB and config to tmp_path, return fresh connection."""
    db_path = tmp_path / "usage.db"
    config_path = tmp_path / "config.json"
    cli_mod.DB_PATH = db_path
    cli_mod.CONFIG_PATH = config_path
    # Write default config
    config_path.write_text(json.dumps({"plan": "max_5x"}))
    return cli_mod._db()


def _insert_session(conn, started_at, ended_at=None, label="test", task_type="coding",
                    messages=10, tokens_est=500, peak_hour=0, project=None):
    conn.execute(
        "INSERT INTO sessions (started_at, ended_at, label, task_type, messages, tokens_est, peak_hour, project) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (started_at.isoformat(), ended_at.isoformat() if ended_at else None,
         label, task_type, messages, tokens_est, peak_hour, project)
    )
    conn.commit()


# ── week command tests ───────────────────────────────────────────────────────

class TestWeekCommand:
    def test_no_sessions(self, tmp_path):
        """week with zero sessions shows 'no sessions' message."""
        _setup_test_db(tmp_path)
        result = runner.invoke(cli_mod.app, ["week"])
        assert result.exit_code == 0
        assert "no sessions tracked" in result.output.lower()

    def test_single_session_not_enough_data(self, tmp_path):
        """week with <2 days of data shows 'not enough data' message."""
        conn = _setup_test_db(tmp_path)
        now = datetime.now(timezone.utc)
        _insert_session(conn, started_at=now - timedelta(hours=3), ended_at=now - timedelta(hours=1))
        result = runner.invoke(cli_mod.app, ["week"])
        assert result.exit_code == 0
        assert "not enough data" in result.output.lower()

    def test_projection_with_enough_data(self, tmp_path):
        """week with 3+ days of data shows projection panel."""
        conn = _setup_test_db(tmp_path)
        now = datetime.now(timezone.utc)
        # 3 sessions spread over 4 days
        for i in range(3):
            start = now - timedelta(days=4 - i)
            _insert_session(conn, started_at=start, ended_at=start + timedelta(hours=4))

        result = runner.invoke(cli_mod.app, ["week"])
        assert result.exit_code == 0
        assert "sessions used" in result.output.lower() or "used so far" in result.output.lower()
        assert "projected" in result.output.lower()

    def test_warning_when_over_80_pct(self, tmp_path):
        """week warns if projected total exceeds 80% of plan ceiling."""
        conn = _setup_test_db(tmp_path)
        now = datetime.now(timezone.utc)
        # Heavy usage: 8 sessions in 2 days on max_5x plan (50/week)
        # That's 4/day => 28/week projected, which is 56% — need more
        # Use pro plan (10/week) with 5 sessions in 3 days => ~12 projected => 120%
        cli_mod.CONFIG_PATH.write_text(json.dumps({"plan": "pro"}))
        for i in range(5):
            start = now - timedelta(days=3) + timedelta(hours=i * 8)
            _insert_session(conn, started_at=start, ended_at=start + timedelta(hours=4))

        result = runner.invoke(cli_mod.app, ["week"])
        assert result.exit_code == 0
        assert "warning" in result.output.lower() or "⚠" in result.output


# ── export command tests ─────────────────────────────────────────────────────

class TestExportCommand:
    def test_export_no_sessions(self, tmp_path):
        """export with no sessions prints warning, creates no file."""
        _setup_test_db(tmp_path)
        out_file = tmp_path / "should_not_exist.csv"
        result = runner.invoke(cli_mod.app, ["export", "--days", "7", "--output", str(out_file)])
        assert result.exit_code == 0
        assert "no sessions" in result.output.lower()
        assert not out_file.exists()

    def test_export_creates_file(self, tmp_path):
        """export creates a CSV file with data rows."""
        conn = _setup_test_db(tmp_path)
        now = datetime.now(timezone.utc)
        _insert_session(conn, started_at=now - timedelta(hours=5), ended_at=now - timedelta(hours=1))
        _insert_session(conn, started_at=now - timedelta(hours=10), ended_at=now - timedelta(hours=6))

        out_file = tmp_path / "test_export.csv"
        result = runner.invoke(cli_mod.app, ["export", "--output", str(out_file)])
        assert result.exit_code == 0
        assert out_file.exists()

        import csv
        with open(out_file, newline="") as f:
            rows = list(csv.reader(f))
            assert len(rows) == 3  # header + 2 data rows

    def test_export_correct_columns(self, tmp_path):
        """export writes CSV with correct column order and status values."""
        conn = _setup_test_db(tmp_path)
        now = datetime.now(timezone.utc)
        # done session (4h)
        _insert_session(conn, started_at=now - timedelta(hours=5), ended_at=now - timedelta(hours=1))
        # short session (2h)
        _insert_session(conn, started_at=now - timedelta(hours=3), ended_at=now - timedelta(hours=1))
        # active session
        _insert_session(conn, started_at=now - timedelta(hours=1))

        out_file = tmp_path / "test_columns.csv"
        result = runner.invoke(cli_mod.app, ["export", "--output", str(out_file)])
        assert result.exit_code == 0

        import csv
        with open(out_file, newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
            assert header == ["id", "label", "task_type", "started_at", "ended_at",
                              "duration_hrs", "messages", "tokens_est", "peak_hour", "notes", "status"]
            rows = list(reader)
            statuses = {r[10] for r in rows}
            assert statuses == {"done", "short", "active"}
            # active row has empty duration
            active_row = [r for r in rows if r[10] == "active"][0]
            assert active_row[5] == ""  # duration_hrs empty for active

    def test_export_custom_output_path(self, tmp_path):
        """export --output writes to the specified path."""
        conn = _setup_test_db(tmp_path)
        now = datetime.now(timezone.utc)
        _insert_session(conn, started_at=now - timedelta(hours=5), ended_at=now - timedelta(hours=1))

        custom_path = tmp_path / "subdir" / "my_sessions.csv"
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        result = runner.invoke(cli_mod.app, ["export", "--output", str(custom_path)])
        # file exists check is sufficient — panel output wraps long paths
        assert result.exit_code == 0
        assert custom_path.exists()


# ── estimate command tests ──────────────────────────────────────────────────

class TestEstimateCommand:
    def test_estimate_no_history(self, tmp_path):
        """Fallback rate used, full window, all 6 cells present."""
        _setup_test_db(tmp_path)
        # Ensure no active session
        result = runner.invoke(cli_mod.app, ["estimate"])
        assert result.exit_code == 0
        assert "no history yet" in result.output.lower() or "default" in result.output.lower()
        assert f"{cli_mod.SESSION_HOURS}h" in result.output
        # All three size rows present
        assert "Small" in result.output
        assert "Medium" in result.output
        assert "Large" in result.output
        # Sonnet and Opus columns have values (~ prefix)
        assert result.output.count("~") >= 6  # 3 sizes x 2 models

    def test_estimate_with_active_session(self, tmp_path):
        """time_remaining < SESSION_HOURS when a session is active."""
        conn = _setup_test_db(tmp_path)
        now = datetime.now(timezone.utc)
        # Start a session 2 hours ago (no end)
        _insert_session(conn, started_at=now - timedelta(hours=2), ended_at=None, messages=0)
        result = runner.invoke(cli_mod.app, ["estimate"])
        assert result.exit_code == 0
        assert "active session" in result.output.lower()
        # Should show ~3h remaining, not full 5h
        assert f"{cli_mod.SESSION_HOURS}h available" not in result.output

    def test_estimate_peak_penalty(self, tmp_path):
        """When peak, Sonnet/Opus values are lower than off-peak equivalents."""
        conn = _setup_test_db(tmp_path)
        # No active session, no history — uses defaults for both runs
        _setup_test_db(tmp_path)

        # Off-peak run
        with patch.object(cli_mod, '_is_peak', return_value=False):
            result_off = runner.invoke(cli_mod.app, ["estimate"])

        _setup_test_db(tmp_path)

        # Peak run
        with patch.object(cli_mod, '_is_peak', return_value=True):
            result_peak = runner.invoke(cli_mod.app, ["estimate"])

        assert result_peak.exit_code == 0
        assert "peak" in result_peak.output.lower()
        assert "0.75x" in result_peak.output

        # Extract Small/Sonnet number from each (first ~N in the table)
        import re
        off_nums = [int(x) for x in re.findall(r'~(\d+)', result_off.output)]
        peak_nums = [int(x) for x in re.findall(r'~(\d+)', result_peak.output)]
        # Peak values should be strictly lower
        assert all(p <= o for p, o in zip(peak_nums, off_nums))
        assert any(p < o for p, o in zip(peak_nums, off_nums))

    def test_estimate_size_filter(self, tmp_path):
        """--size large shows only large row."""
        _setup_test_db(tmp_path)
        result = runner.invoke(cli_mod.app, ["estimate", "--size", "large"])
        assert result.exit_code == 0
        assert "Large" in result.output
        assert "Small" not in result.output
        assert "Medium" not in result.output


# ── project tagging tests ───────────────────────────────────────────────────

class TestProjectTagging:
    def test_project_tag_stored(self, tmp_path):
        """start with --project stores the value in DB."""
        conn = _setup_test_db(tmp_path)
        result = runner.invoke(cli_mod.app, [
            "start", "--label", "auth", "--task", "coding", "--project", "burnrate-build"
        ])
        assert result.exit_code == 0
        row = conn.execute("SELECT project FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        assert row["project"] == "burnrate-build"

    def test_project_history_filter(self, tmp_path):
        """history --project returns only matching sessions."""
        conn = _setup_test_db(tmp_path)
        now = datetime.now(timezone.utc)
        _insert_session(conn, started_at=now - timedelta(hours=10), ended_at=now - timedelta(hours=6),
                        project="alpha")
        _insert_session(conn, started_at=now - timedelta(hours=5), ended_at=now - timedelta(hours=2),
                        project="alpha")
        _insert_session(conn, started_at=now - timedelta(hours=3), ended_at=now - timedelta(hours=1),
                        project="beta")

        result = runner.invoke(cli_mod.app, ["history", "--project", "alpha"])
        assert result.exit_code == 0
        # Should show 2 sessions, not 3
        assert "alpha" in result.output.lower()
        assert "Project total: 2 sessions" in result.output

    def test_project_history_summary_line(self, tmp_path):
        """Project filter shows totals summary line."""
        conn = _setup_test_db(tmp_path)
        now = datetime.now(timezone.utc)
        _insert_session(conn, started_at=now - timedelta(hours=5), ended_at=now - timedelta(hours=1),
                        messages=20, tokens_est=1000, project="myproj")
        _insert_session(conn, started_at=now - timedelta(hours=10), ended_at=now - timedelta(hours=6),
                        messages=15, tokens_est=800, project="myproj")

        result = runner.invoke(cli_mod.app, ["history", "--project", "myproj"])
        assert result.exit_code == 0
        assert "Project total:" in result.output
        assert "35 messages" in result.output
        assert "1,800 tokens" in result.output

    def test_project_history_no_match(self, tmp_path):
        """Warning shown for unknown project name."""
        _setup_test_db(tmp_path)
        result = runner.invoke(cli_mod.app, ["history", "--project", "nonexistent"])
        assert result.exit_code == 0
        assert "no sessions found for project" in result.output.lower()

    def test_db_migration(self, tmp_path):
        """ALTER TABLE is safe to run twice (no crash)."""
        conn = _setup_test_db(tmp_path)
        # Call _db() again — migration runs a second time
        conn2 = cli_mod._db()
        # Should not crash, and project column should exist
        row = conn2.execute("PRAGMA table_info(sessions)").fetchall()
        col_names = [r["name"] for r in row]
        assert "project" in col_names


# ── config timezone tests ──────────────────────────────────────────────────

class TestConfigTimezone:
    def test_config_tz_named_shortcut_et(self, tmp_path):
        """config --tz et sets timezone_offset to -4."""
        _setup_test_db(tmp_path)
        result = runner.invoke(cli_mod.app, ["config", "--tz", "et"])
        assert result.exit_code == 0
        cfg = json.loads(cli_mod.CONFIG_PATH.read_text())
        assert cfg["timezone_offset"] == -4

    def test_config_tz_named_shortcut_pt(self, tmp_path):
        """config --tz pt sets timezone_offset to -7."""
        _setup_test_db(tmp_path)
        result = runner.invoke(cli_mod.app, ["config", "--tz", "pt"])
        assert result.exit_code == 0
        cfg = json.loads(cli_mod.CONFIG_PATH.read_text())
        assert cfg["timezone_offset"] == -7

    def test_config_tz_raw_offset(self, tmp_path):
        """config --tz -5 sets timezone_offset to -5."""
        _setup_test_db(tmp_path)
        result = runner.invoke(cli_mod.app, ["config", "--tz", "-5"])
        assert result.exit_code == 0
        cfg = json.loads(cli_mod.CONFIG_PATH.read_text())
        assert cfg["timezone_offset"] == -5

    def test_config_tz_invalid(self, tmp_path):
        """config --tz xyz prints error."""
        _setup_test_db(tmp_path)
        result = runner.invoke(cli_mod.app, ["config", "--tz", "xyz"])
        assert result.exit_code == 1
        assert "unknown timezone" in result.output.lower()

    def test_display_tz_conversion(self, tmp_path):
        """_to_display_tz converts UTC to configured offset."""
        dt_utc = datetime(2025, 6, 15, 18, 0, 0, tzinfo=timezone.utc)
        cfg = {"timezone_offset": -4}
        result = cli_mod._to_display_tz(dt_utc, cfg)
        assert result.hour == 14  # 18 - 4 = 14
        assert result.day == 15


# ── plan command tests ─────────────────────────────────────────────────────

class TestPlanCommand:
    def test_plan_no_active_session(self, tmp_path):
        """plan with no active session shows 'available now' and 4 windows."""
        _setup_test_db(tmp_path)
        result = runner.invoke(cli_mod.app, ["plan"])
        assert result.exit_code == 0
        assert "no active session" in result.output.lower()
        assert "Next" in result.output
        assert "+5h" in result.output
        assert "+10h" in result.output
        assert "+15h" in result.output

    def test_plan_with_active_session(self, tmp_path):
        """plan with active session shows remaining time."""
        conn = _setup_test_db(tmp_path)
        now = datetime.now(timezone.utc)
        _insert_session(conn, started_at=now - timedelta(hours=2), ended_at=None, messages=0)
        result = runner.invoke(cli_mod.app, ["plan"])
        assert result.exit_code == 0
        assert "active session" in result.output.lower()
        assert "remaining" in result.output.lower()

    def test_plan_shows_weekly_budget(self, tmp_path):
        """plan shows weekly budget line."""
        _setup_test_db(tmp_path)
        result = runner.invoke(cli_mod.app, ["plan"])
        assert result.exit_code == 0
        assert "weekly budget" in result.output.lower()
        assert "sessions remaining" in result.output.lower()

    def test_plan_shows_ratings(self, tmp_path):
        """plan shows IDEAL/OK/AVOID ratings."""
        _setup_test_db(tmp_path)
        result = runner.invoke(cli_mod.app, ["plan"])
        assert result.exit_code == 0
        # At least one rating should appear
        has_rating = any(r in result.output for r in ["IDEAL", "OK", "AVOID"])
        assert has_rating


# ── sync command tests ────────────────────────────────────────────────────

def _insert_sync(conn, synced_at, session_pct=50, weekly_pct=30,
                 session_expires_at=None, weekly_resets_at=None, source="manual"):
    conn.execute(
        "INSERT INTO sync_snapshots (synced_at, session_pct_used, weekly_pct_used, "
        "session_expires_at, weekly_resets_at, source) VALUES (?,?,?,?,?,?)",
        (synced_at.isoformat(), session_pct, weekly_pct,
         session_expires_at.isoformat() if session_expires_at else None,
         weekly_resets_at, source)
    )
    conn.commit()


class TestSyncCommand:
    def test_sync_manual_all_flags(self, tmp_path):
        """All 4 flags provided, snapshot stored correctly."""
        conn = _setup_test_db(tmp_path)
        result = runner.invoke(cli_mod.app, [
            "sync", "--session", "58", "--weekly", "40",
            "--resets-in", "3h 47m", "--weekly-resets", "Tue 9:00 AM"
        ])
        assert result.exit_code == 0
        assert "42% remaining" in result.output  # 100-58
        assert "60% remaining" in result.output  # 100-40
        row = conn.execute("SELECT * FROM sync_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        assert row["session_pct_used"] == 58
        assert row["weekly_pct_used"] == 40
        assert row["source"] == "manual"
        assert row["session_expires_at"] is not None
        assert row["weekly_resets_at"] == "Tue 9:00 AM"

    def test_sync_manual_session_only(self, tmp_path):
        """Only --session and --resets-in provided."""
        conn = _setup_test_db(tmp_path)
        result = runner.invoke(cli_mod.app, [
            "sync", "--session", "25", "--resets-in", "2h"
        ])
        assert result.exit_code == 0
        assert "75% remaining" in result.output
        row = conn.execute("SELECT * FROM sync_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        assert row["session_pct_used"] == 25
        assert row["weekly_pct_used"] is None
        assert row["source"] == "manual"

    def test_sync_manual_invalid_percentage(self, tmp_path):
        """--session 150 shows error."""
        _setup_test_db(tmp_path)
        result = runner.invoke(cli_mod.app, ["sync", "--session", "150", "--resets-in", "1h"])
        assert result.exit_code == 1
        assert "0-100" in result.output

    def test_sync_paste_valid_text(self, tmp_path):
        """Paste text matching Claude's format, all fields parsed."""
        conn = _setup_test_db(tmp_path)
        paste = "Usage 58% used Resets in 3 hr 47 min 40% used Resets Tue 9:00 AM\n"
        result = runner.invoke(cli_mod.app, ["sync"], input=paste)
        assert result.exit_code == 0
        assert "42% remaining" in result.output
        row = conn.execute("SELECT * FROM sync_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        assert row["session_pct_used"] == 58
        assert row["weekly_pct_used"] == 40
        assert row["source"] == "paste"

    def test_sync_paste_missing_field(self, tmp_path):
        """Paste text without 'Resets in' shows warning."""
        _setup_test_db(tmp_path)
        paste = "Usage 58% used Some other text\n"
        result = runner.invoke(cli_mod.app, ["sync"], input=paste)
        assert result.exit_code == 0
        assert "could not parse" in result.output.lower()

    def test_sync_paste_partial(self, tmp_path):
        """Only session found, weekly missing, stores what it can."""
        conn = _setup_test_db(tmp_path)
        paste = "Usage 58% used Resets in 2 hr 30 min\n"
        result = runner.invoke(cli_mod.app, ["sync"], input=paste)
        assert result.exit_code == 0
        row = conn.execute("SELECT * FROM sync_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        assert row["session_pct_used"] == 58
        assert row["weekly_pct_used"] is None

    def test_sync_freshness(self, tmp_path):
        """Snapshot older than 2h returns False from _sync_is_fresh."""
        conn = _setup_test_db(tmp_path)
        old_time = datetime.now(timezone.utc) - timedelta(hours=3)
        _insert_sync(conn, synced_at=old_time)
        snap = cli_mod._latest_sync(conn)
        assert not cli_mod._sync_is_fresh(snap)

        # Fresh one should be True
        fresh_time = datetime.now(timezone.utc) - timedelta(minutes=30)
        _insert_sync(conn, synced_at=fresh_time)
        snap2 = cli_mod._latest_sync(conn)
        assert cli_mod._sync_is_fresh(snap2)

    def test_status_uses_sync_when_fresh(self, tmp_path):
        """status shows synced % not elapsed bar when sync is fresh."""
        conn = _setup_test_db(tmp_path)
        now = datetime.now(timezone.utc)
        # Active session
        _insert_session(conn, started_at=now - timedelta(hours=2), ended_at=None, messages=0)
        # Fresh sync
        _insert_sync(conn, synced_at=now - timedelta(minutes=5), session_pct=45,
                     weekly_pct=30, session_expires_at=now + timedelta(hours=3))
        result = runner.invoke(cli_mod.app, ["status"])
        assert result.exit_code == 0
        assert "45%" in result.output
        assert "synced" in result.output.lower()
        assert "Live data" in result.output

    def test_estimate_uses_sync_expires_at(self, tmp_path):
        """estimate uses synced expiry time."""
        conn = _setup_test_db(tmp_path)
        now = datetime.now(timezone.utc)
        _insert_session(conn, started_at=now - timedelta(hours=2), ended_at=None, messages=0)
        _insert_sync(conn, synced_at=now - timedelta(minutes=5),
                     session_expires_at=now + timedelta(hours=2))
        result = runner.invoke(cli_mod.app, ["estimate"])
        assert result.exit_code == 0
        assert "last sync" in result.output.lower()

    def test_plan_uses_sync_next_window(self, tmp_path):
        """plan uses synced expiry as window start."""
        conn = _setup_test_db(tmp_path)
        now = datetime.now(timezone.utc)
        _insert_session(conn, started_at=now - timedelta(hours=2), ended_at=None, messages=0)
        _insert_sync(conn, synced_at=now - timedelta(minutes=5),
                     session_expires_at=now + timedelta(hours=1))
        result = runner.invoke(cli_mod.app, ["plan"])
        assert result.exit_code == 0
        assert "last sync" in result.output.lower()
