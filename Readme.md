# claude-budget

> **Stop losing 10-15% of your weekly Claude limit to wasted sessions.**

A lightweight CLI that tracks your Claude 5-hour session windows and 7-day weekly usage so you can make every message count.

---

## The problem this solves

Claude's usage model has two layers:

- **5-hour rolling sessions** - starts when you send your first message, expires 5 hours later whether you use it or not
- **7-day weekly cap** - total usage across all surfaces: claude.ai, Claude Code, and Desktop
- **Peak hours** - since March 2026, sessions drain faster during 5am-11am PT on weekdays

If you're careful all week but start sessions you don't finish, or accidentally run heavy work during peak hours, you lose budget you already paid for. This tool surfaces that waste and helps you avoid it.

---

## Install

```bash
pip install claude-budget
```

Or from source:

```bash
git clone https://github.com/AravindKurapati/claude-budget
cd claude-budget
pip install -e .
```

**Requirements:** Python 3.9+. No API keys needed; everything is local.

---

## Usage

### Start a session

Run this before opening Claude:

```bash
claude-budget start --label "RAG pipeline" --task coding
```

Output:

```text
Session Started  2026-04-09 09:00 UTC
Label: RAG pipeline  |  Task: coding
Window: 5h rolling
OK off-peak - good time for heavy work

Weekly sessions used: ##............................  3/50 est. (6%)
```

### Check status mid-session

```bash
claude-budget status
```

### End a session and log usage

```bash
claude-budget end --messages 45 --tokens 30000
```

### Get personalised waste-reduction advice

```bash
claude-budget advice
```

Output example:

```text
Short sessions: You left ~3.2h unused across 2 sessions. Front-load heavier
tasks so each 5h window is fully used.

Peak hours: 4/7 sessions were during peak (5am-11am PT). Shifting heavy
sessions to evenings gives you the same window but it drains slower.
```

### View history

```bash
claude-budget history --days 7
```

### Configure your plan

```bash
claude-budget config --plan max_5x --tz 3   # ET = PT+3
claude-budget config --plan pro              # or max_20x
claude-budget config --show
```

---

## All commands

| Command | What it does |
|---------|-------------|
| `start` | Begin tracking a new session (`--project` to tag) |
| `end` | Close session, log messages and token estimate |
| `status` | Current session and weekly budget at a glance |
| `history` | Session log for last N days (`--project` to filter) |
| `dashboard` | Visual terminal charts for recent usage (`--project` to filter) |
| `projects` | Summarize sessions, messages, and tokens by project |
| `doctor` | Check config, stale sessions, sync freshness, and data quality |
| `forecast` | Estimate when current pace reaches 80% and 100% of weekly budget |
| `simulate` | Test hypothetical usage pace against a plan |
| `assumptions` | View or tune local forecasting assumptions |
| `review` | Retrospective summary for recent sessions |
| `week` | Project end-of-week budget based on burn pace |
| `estimate` | Estimate questions remaining by size and model |
| `plan` | Pick better start times around active windows and peak hours |
| `sync` | Sync manual usage numbers from Claude's Settings > Usage page |
| `optimize` | Schedule remaining sessions before weekly reset |
| `export` | Export session history to CSV |
| `advice` | Personalised tips based on your usage patterns |
| `config` | Set plan and timezone |
| `reset` | Wipe history (keeps config) |

---

## Visual summaries

```bash
claude-budget dashboard
claude-budget dashboard --days 14
claude-budget dashboard --project burnrate-build
claude-budget projects
```

`dashboard` charts sessions, messages, peak/off-peak usage, short sessions, and active windows right in the terminal. `projects` rolls up usage by project tag so multi-session builds are easier to review.

---

## Forecast and review

```bash
claude-budget doctor
claude-budget forecast
claude-budget simulate --sessions-per-day 3 --plan pro
claude-budget review --days 14
```

`doctor` flags setup and data-quality problems, such as stale sync data or missing message counts. `forecast` estimates when your current pace will hit your warning threshold and 100% of your weekly budget. `simulate` models a hypothetical pace without changing your history. `review` gives a compact retrospective of recent sessions, including short-session waste, peak-hour usage, message pace, and the main project.

---

## Configurable assumptions

Forecasts and estimates are local and configurable. Hiring managers, reviewers, or power users can tune the model without editing code:

```bash
claude-budget assumptions
claude-budget assumptions --set peak_penalty=0.65
claude-budget assumptions --set default_msg_rate=18
claude-budget assumptions --set weekly_sessions.pro=12
claude-budget assumptions --load examples/heavy_coder_assumptions.json
claude-budget assumptions --reset
```

The configurable assumptions are stored in `~/.claude_budget/config.json`:

```json
{
  "assumptions": {
    "session_hours": 5,
    "peak_penalty": 0.75,
    "default_msg_rate": 10,
    "short_session_threshold_hours": 4,
    "fresh_sync_hours": 2,
    "weekly_warning_threshold": 0.8,
    "weekly_sessions": {
      "pro": 10,
      "max_5x": 50,
      "max_20x": 200
    }
  }
}
```

These values feed `estimate`, `forecast`, `simulate`, `doctor`, `review`, `dashboard`, `projects`, `plan`, `optimize`, `week`, `status`, and `advice`.

Example assumption profiles live in `examples/` so reviewers can quickly test Pro, heavy-coding, and weekend-builder usage patterns.

---

## Peak hours

Since March 26, 2026, Anthropic burns through your 5-hour session limit faster during weekday peak hours:

- **Peak:** Mon-Fri, 5am-11am PT (1pm-7pm GMT)
- **Off-peak:** Evenings, nights, weekends. Same window, slower drain.

`claude-budget status` always shows whether you're in peak hours right now, so you can time your heavy sessions accordingly.

---

## How usage is stored

Everything is local: a SQLite database at `~/.claude_budget/usage.db`. No data leaves your machine. Config lives at `~/.claude_budget/config.json`.

Token estimates are manual because Anthropic doesn't expose token counts through the claude.ai interface. Even rough estimates are enough to see patterns over time.

---

## Tips to reduce waste

From the `advice` command, and from building this tool:

1. **Don't end sessions early** - a 5h window is already burned the moment you start it. Use `/clear` within Claude to reset context without starting a new session.
2. **Front-load heavy tasks** - open with your most token-intensive work, not quick questions.
3. **Avoid peak hours for big jobs** - same 5h window, but it drains slower off-peak.
4. **Batch your messages** - Claude counts messages, not just tokens. One well-constructed message with 5 questions is more efficient than 5 separate messages.
5. **Use sub-agents for research** - Claude Code sub-agents do research without bloating your main context window.

---

PRs welcome. If you've found other patterns that reduce waste, open an issue.

---

## License

MIT
