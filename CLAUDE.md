# claude-burnrate — CLAUDE.md

## What this project is
A Python CLI tool that tracks Claude session windows and weekly usage.
Helps users avoid wasting their 5-hour rolling sessions and weekly caps.

## Stack
- Python 3.9+
- Typer (CLI framework)
- Rich (terminal output)
- SQLite via stdlib sqlite3 (local DB at ~/.claude_budget/usage.db)
- No external API calls — everything runs locally

## File structure
```
claude-burnrate/
  cli.py              ← all commands live here (single file for now)
  Pyproject.toml      ← package config, entry point: claude-burnrate = cli:app
  CLAUDE.md           ← this file
  SPEC.md             ← feature specs (read before implementing)
  README.md
  LICENSE
```

## Conventions
- Single-file CLI — keep everything in cli.py until it exceeds ~600 lines
- All DB access through _db() helper — never raw sqlite3.connect() elsewhere
- Config loaded via _load_config() — never read the JSON file directly
- UTC everywhere internally — convert to PT only for display and peak detection
- Plan constants in PLAN_WEEKLY_SESSIONS dict — never hardcode session counts inline
- Rich panels for all user-facing output — no bare print() calls
- Helper functions prefixed with _ (private)
- Test by running: python cli.py <command>

## What NOT to do
- Do not add external dependencies beyond typer and rich
- Do not make any network requests
- Do not restructure into multiple files unless explicitly asked
- Do not rename existing commands — users may have these aliased

## Current commands
start, end, status, history, advice, config, reset

## Adding new commands
Add as @app.command() functions in cli.py.
Update README.md command table when adding new commands.
Keep help strings concise — one line.
