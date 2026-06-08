"""Craft Score (Phase 7) — a rate-normalized, honor-system score rewarding SKILLED, RESPONSIBLE Claude
Code use rather than raw spend.

Design notes:
- **Pure functions over pre-computed aggregates** (no DB) so the formula is trivially unit-testable and the
  scoring policy lives in one auditable place. server.py gathers the aggregates from store.py and calls score().
- **Rate-normalized** — every sub-score is a ratio, never a volume, so a light user and a heavy user compete
  fairly (you can't win by burning more tokens).
- **HONESTY** (same discipline as the cost estimate): each sub-score is a heuristic over *observable proxies*
  (cache hits, compaction triggers, context-zone time, skill/subagent use). It is NOT a judgment of work
  quality — quality is unobservable. The UI says so.
- **Local-only, zero egress** — this never leaves the box (project principle #1).

Three sub-scores, each 0–100 (or None when there's no signal), then a weight-renormalized composite.
"""

# Equal weights by default; tune here. A dim that has no signal (None) is dropped and the rest renormalize.
WEIGHTS = {"efficiency": 1.0, "hygiene": 1.0, "craft": 1.0}

# Craft saturation: advanced-tool uses per 100 messages at which the Craft dim approaches 100. A gentle
# saturating curve (1 - e^-x) so the first few skills/subagents move the needle and it can't be farmed.
_CRAFT_HALFLIFE = 2.5


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def efficiency_score(cache_ratio):
    """Efficiency = cache-read ratio mapped to 0–100. None when there's no token data."""
    if cache_ratio is None:
        return None
    return round(_clamp(cache_ratio * 100), 1)


def hygiene_score(compaction_manual=0, compaction_auto=0, healthy_pct=None, tool_errors=0, messages=0):
    """Hygiene = the average of whichever of these signals have data:
      - compaction discipline: manual / (manual + auto)  (compacting before the wall is the win)
      - healthy context-zone time: % of samples below the drift onset (Sharp+Good)
      - cleanliness: 1 - (tool_errors / messages), i.e. a low tool-error rate
    None only when NONE of the three has any signal."""
    parts = []
    tot = (compaction_manual or 0) + (compaction_auto or 0)
    if tot:
        parts.append(_clamp(compaction_manual / tot * 100))
    if healthy_pct is not None:
        parts.append(_clamp(healthy_pct))
    if messages:
        err_rate = min(1.0, (tool_errors or 0) / messages)
        parts.append(_clamp((1 - err_rate) * 100))
    if not parts:
        return None
    return round(sum(parts) / len(parts), 1)


def craft_dim_score(skill_use=0, subagent_spawn=0, messages=0):
    """Craft = adoption of leverage beyond plain chat (skills + subagents), rate-normalized per 100 messages
    through a saturating curve. None when there are no messages to normalize against."""
    if not messages:
        return None
    import math
    per100 = (skill_use + subagent_spawn) / messages * 100
    return round(_clamp(100 * (1 - math.exp(-per100 / _CRAFT_HALFLIFE))), 1)


def composite(eff, hyg, craft, weights=WEIGHTS):
    """Weighted average over the non-None dims (weights renormalize). None when nothing scored."""
    parts = [("efficiency", eff), ("hygiene", hyg), ("craft", craft)]
    num = sum(weights[k] * v for k, v in parts if v is not None)
    den = sum(weights[k] for k, v in parts if v is not None)
    return round(num / den, 1) if den else None


def grade(s):
    if s is None:
        return "—"
    return "A" if s >= 90 else "B" if s >= 80 else "C" if s >= 70 else "D" if s >= 60 else "F"


def score(agg):
    """agg keys (all optional, default 0/None):
       cache_ratio, messages, compaction_manual, compaction_auto, healthy_pct, tool_errors,
       skill_use, subagent_spawn.
    Returns the full breakdown: {score, grade, dims:{efficiency,hygiene,craft}} where each dim carries its
    score + the raw signals behind it (the UI composes the human-readable 'why')."""
    g = agg.get
    eff = efficiency_score(g("cache_ratio"))
    hyg = hygiene_score(g("compaction_manual", 0), g("compaction_auto", 0),
                        g("healthy_pct"), g("tool_errors", 0), g("messages", 0))
    cr = craft_dim_score(g("skill_use", 0), g("subagent_spawn", 0), g("messages", 0))
    comp = composite(eff, hyg, cr)
    return {
        "score": comp,
        "grade": grade(comp),
        "dims": {
            "efficiency": {"score": eff, "cache_ratio": g("cache_ratio")},
            "hygiene": {"score": hyg,
                        "compaction_manual": g("compaction_manual", 0),
                        "compaction_auto": g("compaction_auto", 0),
                        "healthy_pct": g("healthy_pct"),
                        "tool_errors": g("tool_errors", 0),
                        "messages": g("messages", 0)},
            "craft": {"score": cr,
                      "skill_use": g("skill_use", 0),
                      "subagent_spawn": g("subagent_spawn", 0),
                      "messages": g("messages", 0)},
        },
    }
