# SPEC.md - Features to implement

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
- Never divide by zero - guard elapsed_days with max(elapsed_days, 0.1)

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
- Write the CSV using Python stdlib `csv` module - no pandas
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
- Keep it simple - no error handling beyond try/catch on the start command
- The alias replaces `claude` so every `claude` invocation auto-tracks
- Do not auto-end silently - always prompt for message count so data is useful

---

## Feature 4: `estimate` command

Add @app.command("estimate") to cli.py.

### What it does
Estimates questions remaining in the current session window, broken down
by message size (small / medium / large) and by model (Sonnet / Opus).
Shows a 3x2 grid: 3 sizes × 2 models.

### Command signature
    claude-burnrate estimate
    claude-burnrate estimate --size small
    claude-burnrate estimate --size large

### Options
- `--size` / `-s`: filter to one size row only (default: show all three)

### Size definitions and cost multipliers
These are relative cost units, not real tokens. Opus costs ~5x Sonnet
at the same task size.

Size   | What it means                               | Sonnet cost | Opus cost
-------|---------------------------------------------|-------------|----------
small  | Quick question, yes/no, clarification       | 1x          | 5x
medium | Explain concept, ~50 lines code, write para | 3x          | 15x
large  | Full file review, agentic task, deep research| 8x         | 40x

### Calculation steps

Step 1 - time remaining
    If active session: SESSION_HOURS - elapsed_hours
    If no active session: SESSION_HOURS (full window)

Step 2 - historical message rate
    avg_msgs_per_hr = sum(messages) / sum(duration_hrs)
    across all completed sessions in last 7 days
    Fallback if no history: 10 msg/hr
    Note in output which one is being used

Step 3 - peak hour penalty
    if _is_peak(now): effective_time = time_remaining * 0.75
    else: effective_time = time_remaining
    Show this adjustment as a visible line when it applies

Step 4 - base capacity
    base_capacity = effective_time * avg_msgs_per_hr

Step 5 - estimates per cell
    questions = floor(base_capacity / cost_multiplier)
    Floor at 0. Never negative.

### Output format
Rich panel with three sections:

Section 1 - context line:
    "Active session: 3.2h remaining" OR "No active session - full 5h available"
    "Your rate: 14.3 msg/hr (7-day history)" OR "Using default: 10 msg/hr (no history yet)"
    If peak: " Peak hours - effective time reduced to X.Xh (0.75x penalty)"

Section 2 - estimates table (Rich Table):
    Size     What it means                      Sonnet    Opus
    ------   --------------------------------   -------   ------
    Small    Quick question / clarification     ~142      ~28
    Medium   Concept / code review / para       ~47       ~9
    Large    File review / agentic / research   ~17       ~3

Section 3 - one-line caveat:
    "Estimates based on your usage history. Actual limits depend on
    message complexity, features used, and Anthropic's capacity management."

### Edge cases
- No active session: use full SESSION_HOURS, note in output
- No message history: use 10 msg/hr fallback, note in output
- --size filter: show only that row in the table, all other output unchanged
- All values are 0: show "Session window likely exhausted - start a fresh session"

### Tests to write in test_cli.py
- test_estimate_no_history - fallback rate used, full window, all 6 cells present
- test_estimate_with_active_session - time_remaining < SESSION_HOURS
- test_estimate_peak_penalty - mock _is_peak to return True, verify Opus/Sonnet
  values are lower than off-peak equivalents
- test_estimate_size_filter - --size large shows only large row

---

## Feature 5: project tagging + project history filter

### What it does
Lets users tag sessions with a project name so they can track total
effort (messages + token estimates) across a multi-session build.

### Two parts

Part A - add --project option to `start` command
    claude-burnrate start --label "auth refactor" --task coding --project burnrate-build

- Add `project` TEXT column to the sessions table (nullable, default NULL)
- Migration: on _db() startup, run:
    ALTER TABLE sessions ADD COLUMN project TEXT DEFAULT NULL
  Wrap in try/except - this is a no-op if the column already exists
- Add --project / -p option to start_session()
- Store it in the DB with the session

Part B - add --project filter to `history` command
    claude-burnrate history --project burnrate-build
    claude-burnrate history --project burnrate-build --days 30

- Add --project / -p option to history()
- If provided: filter sessions WHERE project = ? in the query
- At the bottom of the table, add a summary line:
    "Project total: X sessions, Y messages, ~Z tokens est."
- If no sessions match: print yellow warning "No sessions found for project '<name>'"

### Tests to write in test_cli.py
- test_project_tag_stored - start with --project, verify column in DB
- test_project_history_filter - 3 sessions, 2 tagged same project, filter returns 2
- test_project_history_summary_line - verify totals line appears
- test_project_history_no_match - warning shown for unknown project name
- test_db_migration - verify ALTER TABLE is safe to run twice (no crash)

---

## Implementation order
1. Feature 4 (estimate) - no schema changes, read-only
2. Feature 5 (project tagging) - schema migration first, then commands

Show me Feature 4 output before starting Feature 5.
Do not commit either feature - show me first.