#!/usr/bin/env bash
# cc-observability statusline.
# (1) Captures Claude Code's Status JSON (authoritative context-window size/%, cost, model, rate_limits)
#     into ~/.cache/cc-dashboard/<session_id>.status.json (the dashboard reads it).
# (2) Prints a status line showing this session's FRIENDLY NAME (same name+color the dashboard shows for
#     this session_id) so you can match "which terminal am I in?" to a card on the web page.
# Never fails the status line.
set -u
DIR="$HOME/.cache/cc-dashboard"; mkdir -p "$DIR"
JSON="$(cat)"

CC_JSON="$JSON" CC_DIR="$DIR" python3 - <<'PY' 2>/dev/null || printf 'cc-observability'
import os, json, time, tempfile

# --- identity: MUST stay byte-for-byte in sync with web/index.html ident() ---
ADJ = ["brave","calm","swift","keen","wise","bold","sly","spry","lush","nova","zen","jade","ember","rune","fox"]
NOUN = ["otter","falcon","lynx","koi","yak","wren","ibex","moth","fern","comet","puma","crane","heron","sage","wolf"]
def ident(sid):
    h = 2166136261
    for ch in (sid or ""):
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF        # FNV-1a 32-bit (matches JS Math.imul + >>>0)
    name = ADJ[h % len(ADJ)] + "-" + NOUN[(h // 16) % len(NOUN)]
    return name, h % 360
def hsl_rgb(h, s, l):
    c = (1 - abs(2*l - 1)) * s
    x = c * (1 - abs((h/60) % 2 - 1)); m = l - c/2
    r,g,b = [(c,x,0),(x,c,0),(0,c,x),(0,x,c),(x,0,c),(c,0,x)][int(h//60) % 6]
    return int((r+m)*255), int((g+m)*255), int((b+m)*255)

try:
    d = json.loads(os.environ.get("CC_JSON") or "{}")
except Exception:
    d = {}
sid = d.get("session_id") or ""

# which Claude account is this host logged into, and is Extra Usage (overage past the plan rate-limit)
# enabled? Read host-side here so the OAuth tokens in ~/.claude.json never need to enter the dashboard
# container; the watcher reads only the email + the extra-usage flag. ~188K → ~2ms json.load, negligible.
def _oauth():
    try:
        with open(os.path.expanduser("~/.claude.json")) as _f:
            return json.load(_f).get("oauthAccount") or {}
    except Exception:
        return {}
_oa = _oauth()

# capture side-file for the dashboard (best-effort)
if sid:
    m = d.get("model"); model = m.get("display_name") if isinstance(m, dict) else m
    cw = d.get("context_window") or {}; cost = d.get("cost") or {}
    out = {"session_id": sid, "cwd": d.get("cwd"), "model": model,
           "model_id": (m.get("id") if isinstance(m, dict) else None),
           "context_window_size": cw.get("context_window_size"), "used_percentage": cw.get("used_percentage"),
           "total_input_tokens": cw.get("total_input_tokens"), "total_output_tokens": cw.get("total_output_tokens"),
           "cost_usd": cost.get("total_cost_usd"), "exceeds_200k": d.get("exceeds_200k_tokens"),
           "rate_limits": d.get("rate_limits"), "version": d.get("version"),
           "account": _oa.get("emailAddress"), "extra_usage": _oa.get("hasExtraUsageEnabled"),
           "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "source": "statusline"}
    try:
        dirp = os.environ["CC_DIR"]
        fd, tmp = tempfile.mkstemp(dir=dirp, prefix="." + sid + ".")
        with os.fdopen(fd, "w") as fh:
            json.dump(out, fh)
        os.replace(tmp, os.path.join(dirp, sid + ".status.json"))
    except Exception:
        pass

# build the status line (name = cross-reference to the dashboard card)
name, hue = ident(sid)
R, G, B = hsl_rgb(hue, 0.7, 0.55)
cw = d.get("context_window") or {}
mm = d.get("model"); model = (mm.get("display_name") if isinstance(mm, dict) else mm) or ""
parts = ["\033[38;2;{};{};{}m●\033[0m \033[1m{}\033[0m".format(R, G, B, name)]
if model:
    parts.append(model)
# V6.1 — TWO decoupled context signals (mirrors the dashboard). The old V6 colored the % by ABSOLUTE
# tokens, so a 25%-of-1M session (large context, nowhere near the wall) screamed red "⚠ /compact now".
# Now the two risks are separated:
#   (1) COMPACTION proximity = % of window (the hard wall). Owns the alarm RED + the "/compact" cue —
#       it nags only as you near the wall, NOT because absolute context is large.
#   (2) DUMB ZONE = ABSOLUTE accumulated tokens (Context Rot; floor 120k = dashboard ZONE_TOK.drift). A
#       QUIET, dim "🧠 long context" note — never red, never "now" — informs without alarming.
# Dumb zone is a RANGE (mirrors dashboard ZONE_TOK + zone()): 🟡 onset → 🟠 drift → 🔴 deep, keyed on
# ABSOLUTE accumulated tokens. Colors match the dashboard --yellow/--orange/--red. This is the Context-Rot
# heads-up, separate from compaction proximity below; gradual, not a wall (see docs/COMPONENTS.md Advisory).
DZ_GOOD, DZ_DRIFT, DZ_DANGER = 50_000, 120_000, 200_000
DZ_YELLOW, DZ_ORANGE, DZ_RED = (245, 208, 32), (255, 140, 43), (255, 51, 102)
def dzband(t):                          # dumb-zone band by absolute tokens → (rgb, label) or None
    if t >= DZ_DANGER: return DZ_RED,    "🧠 deep context"
    if t >= DZ_DRIFT:  return DZ_ORANGE, "🧠 drifting"
    if t >= DZ_GOOD:   return DZ_YELLOW, "🧠 long context"
    return None
def pband(p):                           # compaction proximity, by % of window
    if p >= 85: return (248, 81, 73),  "🔴", "⚠ /compact now"
    if p >= 60: return (210, 153, 34), "🟡", "consider /compact"
    return         (63, 185, 80),  "🟢", None

pct = cw.get("used_percentage")
if pct is not None:
    (pr, pg, pb), emoji, cue = pband(pct)
    parts.append("\033[38;2;{};{};{}m{} {}%\033[0m".format(pr, pg, pb, emoji, pct))
    if cue:
        parts.append("\033[38;2;{};{};{}m{}\033[0m".format(pr, pg, pb, cue))
    # dumb-zone note: keyed on ABSOLUTE tokens, escalating 🟡→🟠→🔴 across the range (a heads-up that grows
    # with what you carry — distinct from the compaction % above; the 🧠 prefix keeps them apart).
    size = cw.get("context_window_size") or 0
    ctx_tok = int(round(pct / 100.0 * size)) if size else 0
    dz = dzband(ctx_tok)
    if dz:
        (dr, dg, db), label = dz
        parts.append("\033[38;2;{};{};{}m{}\033[0m".format(dr, dg, db, label))
print(" · ".join(parts))
PY
