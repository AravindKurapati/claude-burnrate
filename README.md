# claude-budget

> **Stop losing 10-15% of your weekly Claude limit to wasted sessions.**

A lightweight CLI that tracks your Claude 5-hour session windows and 7-day weekly usage so you can make every message count.

---

## The problem this solves

Claude's usage model has two layers:
- **5-hour rolling sessions** - starts when you send your first message, expires 5 hours later whether you use them or not
- **7-day weekly cap** - total usage across all surfaces (claude.ai, Claude Code, Desktop)
- **Peak hours** - since March 2026, sessions drain *faster* during 5am–11am PT on weekdays

If you're careful all week but start sessions you don't finish, or accidentally run heavy work during peak hours, you lose budget you already paid for. This tool surfaces that waste and helps you avoid it.

---

## Install

```bash
pip install claude-budget
```

Or from source:
```bash
git clone https://github.com/AravindKurapati/claude-burnrate
cd claude-burnrate
pip install -e .
```

**Requirements:** Python 3.9+ - no API keys needed, everything is local.

---

## Usage

### Start a session (run before opening Claude)
```bash
claude-budget start --label "RAG pipeline" --task coding
```

Output:
```
🟢 Session Started  2026-04-09 09:00 UTC
Label: RAG pipeline  |  Task: coding
Window: 5h rolling
✓ off-peak - good time for heavy work

Weekly sessions used: ██░░░░░░░░░░░░░░░░░░░░░░░░░░░░  3/50 est. (6%)
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
```
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
| `end` | Close session, log messages + token estimate |
| `status` | Current session + weekly budget at a glance |
| `history` | Session log for last N days (`--project` to filter) |
| `week` | Project end-of-week budget based on burn pace |
| `estimate` | Estimate questions remaining by size and model |
| `export` | Export session history to CSV |
| `advice` | Personalised tips based on your usage patterns |
| `config` | Set plan and timezone |
| `reset` | Wipe history (keeps config) |

---

## Peak hours

Since March 26, 2026, Anthropic burns through your 5-hour session limit faster during weekday peak hours:

- **Peak:** Mon–Fri, 5am–11am PT (1pm–7pm GMT)
- **Off-peak:** Evenings, nights, weekends - same window lasts longer

`claude-budget status` always shows whether you're in peak hours right now, so you can time your heavy sessions accordingly.

---

## How usage is stored

Everything is local - a SQLite database at `~/.claude_budget/usage.db`. No data leaves your machine. Config lives at `~/.claude_budget/config.json`.

Token estimates are manual (you enter them when ending a session) because Anthropic doesn't expose token counts through the claude.ai interface. Even rough estimates are enough to see patterns over time.

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
