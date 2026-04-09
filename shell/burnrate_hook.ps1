# Resolve the real claude executable before any alias overrides it
$realClaude = (Get-Command claude -CommandType Application -ErrorAction SilentlyContinue).Source

if (-not $realClaude) {
    Write-Error "claude-burnrate: could not find claude executable on PATH"
    return
}

function Start-ClaudeTracked {
    # Auto-track Claude Code sessions with burnrate

    # Start tracking (silently — don't block if burnrate isn't installed)
    try {
        claude-burnrate start --label "claude-code" --task coding | Out-Null
    } catch {
        Write-Warning "claude-burnrate: could not start session tracking"
    }

    # Run the real claude executable with all passed arguments
    & $realClaude @args

    # Prompt for message count so the data is useful
    $messages = Read-Host "How many messages did you send?"

    # End the session with the message count
    claude-burnrate end --messages $messages
}

# ─────────────────────────────────────────────────────────────────────────────
# SETUP INSTRUCTIONS
# Add this to your PowerShell profile ($PROFILE):
#   . "C:\path\to\shell\burnrate_hook.ps1"
#   Set-Alias claude Start-ClaudeTracked
#
# To find your profile path: echo $PROFILE
# To edit it: notepad $PROFILE
# ─────────────────────────────────────────────────────────────────────────────
