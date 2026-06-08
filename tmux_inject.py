#!/usr/bin/env python3
"""
cc-observability — tmux injection (the write path for answer-from-phone)
========================================================================
Inject a reply into a running Claude Code session by driving its tmux pane with
`tmux send-keys`. Stdlib only. This is the ONE place that turns a phone reply into
keystrokes in your shell, so it is deliberately small and conservative:

  - list-form subprocess (NO shell) → no command-injection surface
  - `-l` literal text → arbitrary reply text is never reinterpreted as key names
    (a reply containing the word "Enter" or "C-c" must NOT be a control key)
  - Enter is ALWAYS a separate send-keys call (the load-bearing trick from CCR;
    bundling it with the text doesn't submit reliably)
  - target is validated to a pane-id (%N) or session[:win[.pane]] shape and
    liveness-checked before we touch it → a stale/closed pane fails cleanly

Security: injecting here is RCE into the user's shell. The network gate lives in
server.py (/reply requires CC_ACCESS_PIN; /outbox requires CC_INGEST_TOKEN). This
module assumes its caller is already authenticated and only guards correctness.
"""
import json
import os
import re
import subprocess

STATE_DIR = os.path.expanduser(os.environ.get("CC_STATE_DIR", "~/.cache/cc-dashboard"))
TMUX_TIMEOUT = float(os.environ.get("CC_TMUX_TIMEOUT", "3.0"))

# A tmux target we are willing to address: a pane id (%12), or a
# session name optionally with :window and .pane (cc-build, cc-build:1, cc-build:1.0).
# Session names here are restricted to a safe charset (no shell metachars, no whitespace).
_TARGET_RE = re.compile(r"^(%[0-9]+|[A-Za-z0-9_.-]+(:[0-9]+(\.[0-9]+)?)?)$")


class InjectError(Exception):
    """Reply could not be delivered. `reason` is a short machine code; str() is human-readable."""
    def __init__(self, reason, detail=""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def valid_target(target):
    return bool(target) and bool(_TARGET_RE.match(target))


def _tmux(args):
    """Run a tmux command (list-form, no shell). Returns CompletedProcess; never raises on nonzero."""
    return subprocess.run(
        ["tmux", *args],
        capture_output=True, text=True, timeout=TMUX_TIMEOUT, check=False,
    )


def pane_alive(target):
    """True iff `target` resolves to a live pane right now."""
    if not valid_target(target):
        return False
    # display-message -p against the target prints the pane id and exits 0 only if it exists.
    r = _tmux(["display-message", "-p", "-t", target, "#{pane_id}"])
    return r.returncode == 0 and r.stdout.strip().startswith("%")


def target_path(session_id, state_dir=None):
    return os.path.join(state_dir or STATE_DIR, f"{session_id}.target.json")


def resolve_target(session_id, state_dir=None):
    """Look up the tmux pane recorded for a Claude session_id (written by the SessionStart hook).
    Returns the pane id (e.g. '%3') or None if unknown / file missing / malformed."""
    if not session_id:
        return None
    try:
        with open(target_path(session_id, state_dir)) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    t = d.get("tmux_pane") or d.get("tmux_target")
    return t if valid_target(t) else None


def inject_text(target, text, enter=True):
    """Send `text` as a literal reply into the tmux pane `target`, then (optionally) Enter.
    Raises InjectError on a bad/dead target. The reply is treated as a single submission;
    embedded newlines are flattened to spaces (multi-line is a future enhancement)."""
    if not valid_target(target):
        raise InjectError("bad_target", str(target))
    if not pane_alive(target):
        raise InjectError("stale_target", target)
    payload = (text or "").replace("\r", " ").replace("\n", " ")
    # 1) clear whatever is on the input line, 2) type the literal reply, 3) submit (separate call).
    for args in (["send-keys", "-t", target, "C-u"],
                 ["send-keys", "-t", target, "-l", payload]):
        r = _tmux(args)
        if r.returncode != 0:
            raise InjectError("send_failed", (r.stderr or "").strip()[:200])
    if enter:
        r = _tmux(["send-keys", "-t", target, "Enter"])
        if r.returncode != 0:
            raise InjectError("send_failed", (r.stderr or "").strip()[:200])
    return {"ok": True, "target": target, "chars": len(payload)}


def deliver(session_id, text, state_dir=None, enter=True):
    """Resolve a Claude session_id to its pane and inject. Raises InjectError if no target known."""
    target = resolve_target(session_id, state_dir)
    if not target:
        raise InjectError("no_target", session_id)
    return inject_text(target, text, enter=enter)


if __name__ == "__main__":
    # Manual smoke: python3 tmux_inject.py <target> <text...>
    import sys
    if len(sys.argv) < 3:
        print("usage: tmux_inject.py <pane-or-session-target> <text...>", file=sys.stderr)
        raise SystemExit(2)
    print(inject_text(sys.argv[1], " ".join(sys.argv[2:])))
