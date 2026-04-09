# SPEC.md — Features to implement

Read CLAUDE.md first for project conventions before touching any code.

---

## Feature 1: `week` command

### What it does
Projects your end-of-week budget based on current burn pace.
Answers: "if I keep using Claude at this rate, how many sessions will I have left by reset day?"

### Command signature
```
claude-burnrate week
```

### Logic
1. Get all sessions in the last 7 days from the DB
2. Calculate days elapsed since the oldest session this week
3. Calculate average sessions per day based on elapsed days
4. Project total sessions by reset day (7 days from first session this week)
5. Compare projected total to plan ceiling (PLAN_WEEKLY_SESSIONS[plan])
6. Output a Rich panel with:
   - Sessions used so far this week
   - Average sessions/day
   - Projected total by reset
   - Sessions remaining at current pace
   - A warning if projected total exceeds 80% of plan ceiling
   - Days until weekly reset

### Edge cases
- If fewer than 2 days of data: show "not enough data yet, check back tomorrow"
- If no sessions at all: show "no sessions tracked this week"
- Never divide by zero — guard elapsed_days with max(elapsed_days, 0.1)

### Plan values (already in PLAN_WEEKLY_SESSIONS dict)
- pro: 10 sessions/week
- max_5x: 50 sessions/week  
- max_20x: 200 sessions/week

---

## Feature 2: `export` command

### What it does
Exports session history to a CSV file for spreadsheet analysis.

### Command signature
```
claude-burnrate export
claude-burnrate export --days 30
claude-burnrate export --output my_sessions.csv
claude-burnrate export --days 30 --output ~/Desktop/sessions.csv
```

### Options
- `--days` / `-d`: how many days back to export (default: 30)
- `--output` / `-o`: output file path (default: `burnrate_export_YYYYMMDD.csv`)

### CSV columns (in this order)
```
id, label, task_type, started_at, ended_at, duration_hrs, messages, tokens_est, peak_hour, notes, status
```

Where:
- `duration_hrs`: calculated from started_at and ended_at (empty string if session still active)
- `peak_hour`: 1 or 0
- `status`: "active" | "short" | "done"
  - active = no ended_at
  - short = duration < 4h (left more than 1h on table)
  - done = duration >= 4h

### Output
- Write the CSV using Python stdlib `csv` module — no pandas
- Print a Rich panel confirming: filename, row count, date range covered
- If no sessions found for the date range: print a warning and exit cleanly

---

## Feature 3: Windows shell hook (PowerShell)

### What it does
Auto-starts a burnrate tracking session when the user opens Claude Code
in their terminal, and optionally prompts to end it when they exit.

### Why Windows / PowerShell
The user is on Windows with VS Code. The hook goes in their PowerShell profile.

### Deliverable
A file: `shell/burnrate_hook.ps1`

This script should:
1. Define a function `Start-ClaudeTracked` that:
   - Runs `claude-burnrate start --label "claude-code" --task coding` silently
   - Then runs the real `claude` command with all passed arguments
   - On exit, runs `claude-burnrate end` and prompts for message count

2. Print instructions at the bottom as a comment block:
```
# SETUP INSTRUCTIONS
# Add this to your PowerShell profile ($PROFILE):
#   . "C:\path\to\shell\burnrate_hook.ps1"
#   Set-Alias claude Start-ClaudeTracked
#
# To find your profile path: echo $PROFILE
# To edit it: notepad $PROFILE
```

### Notes
- Keep it simple — no error handling beyond try/catch on the start command
- The alias replaces `claude` so every `claude` invocation auto-tracks
- Do not auto-end silently — always prompt for message count so data is useful

---

## Implementation order
1. `week` command — pure logic, no new dependencies
2. `export` command — stdlib csv only, no pandas  
3. PowerShell hook — new file in shell/ folder, no changes to cli.py

## Testing each feature after implementation
- `week`: run with 0 sessions, 1 session, 3+ sessions — check all edge cases render correctly
- `export`: run with --days 7, check CSV opens in Excel, verify columns match spec
- Hook: test the ps1 file syntax with `Test-ScriptFileInfo` or just read through it
