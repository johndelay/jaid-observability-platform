#!/usr/bin/env bash
# cc-observability state hook. Register (APPEND) under SessionStart/UserPromptSubmit/PreToolUse/
# PostToolUse/Stop/Notification/SessionEnd as:  cc-state-hook.sh <HookName>
# Writes ~/.cache/cc-dashboard/<session_id>.state.json. Fast, fire-and-forget, always exit 0.
set -u
DIR="$HOME/.cache/cc-dashboard"; mkdir -p "$DIR"
HOOK="${1:-}"
JSON="$(cat 2>/dev/null || true)"
sid="$(printf '%s' "$JSON" | jq -r '.session_id // empty' 2>/dev/null)"
[ -n "$sid" ] || exit 0
cwd="$(printf '%s' "$JSON" | jq -r '.cwd // .workspace.current_dir // empty' 2>/dev/null)"

# Answer-from-phone (E5) needs to know WHICH tmux pane this session lives in. When Claude runs
# inside tmux, $TMUX_PANE is the pane id (e.g. %3) — record session_id -> pane so the responder can
# inject a reply. Refresh every event (cheap; self-heals if the session is moved to another pane).
# No tmux / not in a pane -> skip (this session just isn't answerable-from-phone). 0600: injection target.
if [ -n "${TMUX_PANE:-}" ] && [ -n "${TMUX:-}" ]; then
  tsess="$(tmux display-message -p '#S' 2>/dev/null || true)"
  ttmp="$(mktemp "$DIR/.$sid.tgt.XXXXXX" 2>/dev/null)" || ttmp=""
  if [ -n "$ttmp" ]; then
    jq -n --arg sid "$sid" --arg pane "$TMUX_PANE" --arg sess "$tsess" --arg cwd "$cwd" \
          --arg ts "$(date -u +%FT%TZ)" \
      '{session_id:$sid, tmux_pane:$pane, tmux_session:$sess, cwd:$cwd, updated_at:$ts}' \
      > "$ttmp" 2>/dev/null && chmod 600 "$ttmp" 2>/dev/null && mv -f "$ttmp" "$DIR/$sid.target.json" \
      || rm -f "$ttmp" 2>/dev/null
  fi
fi

bg_running() {  # true if a backgrounded task is still running (don't flash "done")
  local n; n="$(printf '%s' "$JSON" | jq -r '[.background_tasks[]? | select(.status=="running")] | length' 2>/dev/null)"
  [ -n "$n" ] && [ "$n" -gt 0 ] 2>/dev/null
}

case "$HOOK" in
  UserPromptSubmit|PreToolUse|PostToolUse) state="working" ;;
  SessionStart|Notification)               state="waiting" ;;
  Stop)  if bg_running; then state="working"; else state="waiting"; fi ;;
  SessionEnd)                              state="done" ;;
  *) exit 0 ;;
esac

# awaiting_input_since: CLEAR on human-resumed events; SET otherwise (waiting on you)
case "$HOOK" in
  UserPromptSubmit|PreToolUse|PostToolUse|SessionEnd) await="" ;;
  *) await="$(date -u +%FT%TZ)" ;;
esac

ts="$(date -u +%FT%TZ)"
tmp="$(mktemp "$DIR/.$sid.XXXXXX" 2>/dev/null)" || exit 0
jq -n --arg sid "$sid" --arg st "$state" --arg cwd "$cwd" --arg ts "$ts" --arg hook "$HOOK" --arg aw "$await" \
  '{session_id:$sid, state:$st, cwd:$cwd, updated_at:$ts, last_hook:$hook, awaiting_input_since:(if $aw=="" then null else $aw end)}' \
  > "$tmp" 2>/dev/null && mv -f "$tmp" "$DIR/$sid.state.json"
exit 0
