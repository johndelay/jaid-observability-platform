"""Local awards (Phase 8) — collection medals that reward REPEATED responsible/skilled Claude Code use.

Design (locked in ROADMAP V7):
- **Pure, declarative registry + evaluator** over the SAME aggregates as the Craft Score (craft.py). Adding
  a medal = append one dict; no engine surgery. Mirrors the scene/sort registries in the UI.
- **Tiered collection medals** — Bronze → Silver → Gold → Platinum at ascending thresholds. Two metric
  kinds: 'count' (cumulative — repeated good behavior) and 'rate' (a sustained ratio, gated by a minimum
  sample so a 1-message fluke can't earn Platinum).
- **Honor-system, local-only, zero egress.** Anti-cheat is deliberately abandoned (public code +
  user-controlled Claude = unwinnable) — any edition/serial flavor is charm, NOT security.
- **Original names/art only** — no third-party IP; behavior metrics only, never content.

evaluate(agg) returns {medals:[...], level:{...}} where agg has the craft.py keys:
  cache_ratio, messages, compaction_manual, compaction_auto, healthy_pct, tool_errors, skill_use, subagent_spawn.
"""

TIERS = ("Bronze", "Silver", "Gold", "Platinum")
TIER_ICON = {"Bronze": "🥉", "Silver": "🥈", "Gold": "🥇", "Platinum": "💎"}


def _disc(a):
    m, au = a.get("compaction_manual", 0) or 0, a.get("compaction_auto", 0) or 0
    tot = m + au
    return (m / tot * 100) if tot else None


def _clean(a):
    n = a.get("messages", 0) or 0
    return (1 - min(1.0, (a.get("tool_errors", 0) or 0) / n)) * 100 if n else None


# Each medal: id, name, icon, dim, kind, unit, thresholds (Bronze..Platinum, ascending),
# value(agg)->number|None (None = locked / not enough data), min(agg)->bool gate, desc.
REGISTRY = [
    {"id": "cache_whisperer", "name": "Cache Whisperer", "icon": "🫧", "dim": "Efficiency", "kind": "rate",
     "unit": "% cache", "thresholds": [70, 85, 95, 98],
     "value": lambda a: (a.get("cache_ratio") * 100) if a.get("cache_ratio") is not None else None,
     "gate": lambda a: (a.get("messages", 0) or 0) >= 100,
     "desc": "High cache-read ratio — well-structured prompts/context."},
    {"id": "context_surgeon", "name": "Context Surgeon", "icon": "🔪", "dim": "Hygiene", "kind": "rate",
     "unit": "% manual", "thresholds": [50, 75, 90, 100], "value": _disc,
     "gate": lambda a: ((a.get("compaction_manual", 0) or 0) + (a.get("compaction_auto", 0) or 0)) >= 5,
     "desc": "Compacting manually before the wall, not letting auto-compact fire."},
    {"id": "wall_dodger", "name": "Wall Dodger", "icon": "🧱", "dim": "Hygiene", "kind": "count",
     "unit": "manual compactions", "thresholds": [5, 25, 100, 250],
     "value": lambda a: a.get("compaction_manual", 0) or 0, "gate": lambda a: True,
     "desc": "Total manual compactions — every one is a wall dodged."},
    {"id": "zen_mind", "name": "Zen Mind", "icon": "🧘", "dim": "Hygiene", "kind": "rate",
     "unit": "% healthy zone", "thresholds": [40, 60, 80, 95],
     "value": lambda a: a.get("healthy_pct"), "gate": lambda a: (a.get("messages", 0) or 0) >= 100,
     "desc": "Share of time spent in the 🟢/🟡 context-health zones."},
    {"id": "clean_run", "name": "Clean Run", "icon": "✨", "dim": "Hygiene", "kind": "rate",
     "unit": "% clean", "thresholds": [90, 95, 98, 99], "value": _clean,
     "gate": lambda a: (a.get("messages", 0) or 0) >= 100,
     "desc": "Low tool-error rate across your messages."},
    {"id": "delegator", "name": "Delegator", "icon": "🤝", "dim": "Craft", "kind": "count",
     "unit": "subagents", "thresholds": [5, 25, 100, 500],
     "value": lambda a: a.get("subagent_spawn", 0) or 0, "gate": lambda a: True,
     "desc": "Subagents spawned — leverage through delegation."},
    {"id": "skill_master", "name": "Skill Master", "icon": "🎛️", "dim": "Craft", "kind": "count",
     "unit": "skills", "thresholds": [5, 25, 100, 500],
     "value": lambda a: a.get("skill_use", 0) or 0, "gate": lambda a: True,
     "desc": "Skills invoked — using the tools beyond plain chat."},
]


def _earned_tier(value, thresholds):
    """Highest tier index whose threshold is met (-1 = none yet)."""
    idx = -1
    if value is not None:
        for i, t in enumerate(thresholds):
            if value >= t:
                idx = i
    return idx


def evaluate(agg):
    agg = agg or {}
    medals, points, earned = [], 0, 0
    for m in REGISTRY:
        gated = not m["gate"](agg)
        value = None if gated else m["value"](agg)
        idx = _earned_tier(value, m["thresholds"])
        tier = TIERS[idx] if idx >= 0 else None
        nxt = m["thresholds"][idx + 1] if idx + 1 < len(TIERS) else None
        # progress 0..1 toward the next tier (from the current tier's floor); 1.0 at Platinum
        if value is None:
            progress = 0.0
        elif nxt is None:
            progress = 1.0
        else:
            floor = m["thresholds"][idx] if idx >= 0 else 0
            progress = max(0.0, min(1.0, (value - floor) / (nxt - floor))) if nxt > floor else 0.0
        if idx >= 0:
            points += idx + 1
            earned += 1
        medals.append({
            "id": m["id"], "name": m["name"], "icon": m["icon"], "dim": m["dim"], "kind": m["kind"],
            "unit": m["unit"], "desc": m["desc"], "thresholds": m["thresholds"],
            "value": round(value, 1) if isinstance(value, float) else value,
            "tier": tier, "tier_idx": idx, "tier_icon": TIER_ICON.get(tier),
            "next_threshold": nxt, "progress": round(progress, 3), "locked": gated,
        })
    max_points = len(REGISTRY) * len(TIERS)
    return {"medals": medals,
            "level": {"points": points, "max_points": max_points, "earned": earned, "total": len(REGISTRY)}}
