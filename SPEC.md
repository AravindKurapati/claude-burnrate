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

## Feature 4: `estimate` command

### What it does
Estimates how many questions you have left in the current session window,
broken down by message size (small / medium / large).

This is an approximation — Anthropic does not expose token counts on claude.ai.
The estimate is based on: session time remaining, your historical message rate,
message size multipliers, and a peak-hour drain penalty.

### Command signature
```
claude-burnrate estimate
claude-burnrate estimate --size small
claude-burnrate estimate --size medium
claude-burnrate estimate --size large
```

### Options
- `--size` / `-s`: filter output to one size tier (default: show all three)

### Message size definitions
These are the token cost tiers. Show these definitions in the output so users
understand what "small" means without needing to check the docs.

| Size   | What it means                                              | Relative cost |
|--------|------------------------------------------------------------|---------------|
| small  | Quick question, yes/no, short clarification (<50 words)    | 1x            |
| medium | Explain a concept, review ~50 lines of code, write a para  | 3x            |
| large  | Full file review, long doc, agentic task, deep research    | 8x            |

### Core calculation

Step 1: get time remaining in session
```
active session → SESSION_HOURS - elapsed_hours = time_remaining_hrs
no active session → SESSION_HOURS (full window available)
```

Step 2: get historical messages/hour from the last 7 days of completed sessions
```
avg_msgs_per_hr = total_messages / total_duration_hrs
fallback if no history: use 10 messages/hour as a conservative default
```

Step 3: apply peak hour penalty
```
if _is_peak(now): effective_time = time_remaining_hrs * 0.75
else:             effective_time = time_remaining_hrs
```
The 0.75 factor reflects that sessions drain ~25-33% faster during peak hours
(Anthropic confirmed faster drain in March 2026 but did not publish exact multiplier).
Show this adjustment in the output when it applies.

Step 4: calculate base capacity
```
base_capacity = effective_time * avg_msgs_per_hr
```

Step 5: apply size multiplier
```
questions_remaining_small  = base_capacity / 1
questions_remaining_medium = base_capacity / 3
questions_remaining_large  = base_capacity / 8
```

Round all results to nearest integer. Floor at 0 — never show negative.

### Output format
Show a Rich panel with three sections:

Section 1 — Session context:
- Time remaining in window (or "no active session — full 5h window available")
- Avg messages/hour from your history (or "using default: 10 msg/hr — no history yet")
- Peak hour warning if applicable

Section 2 — Estimates table (Rich Table inside the panel):
```
Size     What it means                    Est. questions remaining
------   ------------------------------   -----------------------
Small    Quick question / clarification   ~142
Medium   Concept / code review / para     ~47
Large    File review / agentic / research ~17
```

Section 3 — One-line caveat at the bottom:
"These are estimates based on your usage history. Actual limits depend on
message complexity, features used, and Anthropic's capacity management."

### Edge cases
- No active session: use full SESSION_HOURS as time_remaining, note this in output
- No message history: use fallback of 10 msg/hr, note this in output
- Peak hours: show the 0.75x adjustment as a visible line ("⚡ Peak hour penalty applied: effective time reduced to X.Xh")
- All sizes result in 0: show "session window likely exhausted — start a fresh session"

### Where this fits in the CLI
Add `estimate` as a subcommand in cli.py alongside the others.
It does not require an active session — it works without one (uses full window).
It reads from the DB for history but does not write anything.

### Testing
- Run with no sessions in DB → should show fallback rate, full window
- Run during peak hours (mock by temporarily changing PEAK_START_PT) → should show penalty
- Run with an active session that has 2h remaining → verify numbers scale correctly
- Run with --size large → should show only the large row, not all three

---

## Implementation order
1. `week` command — pure logic, no new dependencies
2. `export` command — stdlib csv only, no pandas
3. PowerShell hook — new file in shell/ folder, no changes to cli.py
4. `estimate` command — read-only, no new dependencies

## Testing each feature after implementation
- `week`: run with 0 sessions, 1 session, 3+ sessions — check all edge cases render correctly
- `export`: run with --days 7, check CSV opens in Excel, verify columns match spec
- Hook: read through the ps1 file to verify alias and prompt logic
- `estimate`: run with no history, with history, during peak hours, with --size filter
