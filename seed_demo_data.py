"""Seed the claude-burnrate DB with 6 realistic demo sessions."""

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path.home() / ".claude_budget" / "usage.db"


def seed():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
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
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN project TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
    conn.commit()

    now = datetime.now(timezone.utc)

    sessions = [
        # 1: Full session, off-peak, coding — 5 days ago
        {
            "started_at": now - timedelta(days=5, hours=2),
            "ended_at":   now - timedelta(days=4, hours=22),
            "label":      "RAG pipeline",
            "task_type":  "coding",
            "notes":      "Set up FAISS index and Modal endpoint",
            "tokens_est": 42000,
            "messages":   52,
            "peak_hour":  0,
            "project":    "llm-sec-filings",
        },
        # 2: Short session, peak, research — 4 days ago
        {
            "started_at": now - timedelta(days=4, hours=5),
            "ended_at":   now - timedelta(days=4, hours=3),
            "label":      "RAGAS evals",
            "task_type":  "research",
            "notes":      "Explored RAGAS metrics, got rate-limited",
            "tokens_est": 11000,
            "messages":   18,
            "peak_hour":  1,
            "project":    "llm-sec-filings",
        },
        # 3: Full session, off-peak, coding — 3 days ago
        {
            "started_at": now - timedelta(days=3, hours=1),
            "ended_at":   now - timedelta(days=2, hours=20, minutes=30),
            "label":      "claude-burnrate build",
            "task_type":  "coding",
            "notes":      "Implemented week and export commands",
            "tokens_est": 38000,
            "messages":   61,
            "peak_hour":  0,
            "project":    "burnrate-build",
        },
        # 4: Short session, peak, writing — 2 days ago
        {
            "started_at": now - timedelta(days=2, hours=6),
            "ended_at":   now - timedelta(days=2, hours=4, minutes=15),
            "label":      "README polish",
            "task_type":  "writing",
            "notes":      "Rewrote install and usage sections",
            "tokens_est": 8500,
            "messages":   14,
            "peak_hour":  1,
            "project":    "burnrate-build",
        },
        # 5: Full session, off-peak, coding — 1 day ago
        {
            "started_at": now - timedelta(days=1, hours=3),
            "ended_at":   now - timedelta(hours=22, minutes=20),
            "label":      "estimate command",
            "task_type":  "coding",
            "notes":      "Built estimate command with peak penalty logic",
            "tokens_est": 35000,
            "messages":   47,
            "peak_hour":  0,
            "project":    "burnrate-build",
        },
        # 6: Active session, off-peak, research — started ~1h ago
        {
            "started_at": now - timedelta(hours=1, minutes=10),
            "ended_at":   None,
            "label":      "vLLM benchmarks",
            "task_type":  "research",
            "notes":      "",
            "tokens_est": 0,
            "messages":   0,
            "peak_hour":  0,
            "project":    "llm-sec-filings",
        },
    ]

    conn.execute("DELETE FROM sessions")
    for s in sessions:
        conn.execute(
            "INSERT INTO sessions (started_at, ended_at, label, task_type, notes, tokens_est, messages, peak_hour, project) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                s["started_at"].isoformat(),
                s["ended_at"].isoformat() if s["ended_at"] else None,
                s["label"],
                s["task_type"],
                s["notes"],
                s["tokens_est"],
                s["messages"],
                s["peak_hour"],
                s["project"],
            ),
        )
    conn.commit()
    conn.close()
    print(f"Seeded {len(sessions)} sessions into {DB_PATH}")


if __name__ == "__main__":
    seed()
