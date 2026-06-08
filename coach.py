#!/usr/bin/env python3
"""cc-observability — coach narrative prompt builder (V8 Slice B).

Pure, dependency-free helpers shared by the container server (which assembles the content-free metrics
bundle) and the host coach-worker (which feeds that bundle to an engine — the user's own `claude -p` by
default, or a local Ollama — and POSTs the narrative back). Keeping the prompt here means the server and the
worker agree on exactly what gets sent to a model.

INVARIANTS (the whole point):
- CONTENT-FREE: only aggregates go in (token counts, cache ratio, context-zone time, event counts). Never a
  line of the user's conversation or code. The prompt below is built solely from those numbers.
- SCREENSHOT-SAFE: no emails / project names / paths — the bundle carries none, and the system prompt forbids
  inventing identifying detail, so the narrative can't leak one.
- HONEST: the model is told these are heuristics over proxies, NOT a judgement of work quality — opportunities,
  never scolding. The UI labels the result an estimate.
"""
import json

# Coaching persona + the honesty / content-free / screenshot-safe discipline, baked into the system prompt so
# it holds regardless of engine (claude CLI or Ollama).
SYSTEM = (
    "You are a concise, encouraging coach helping a developer get more out of Claude Code. "
    "You are given ONLY content-free usage metrics (token counts, cache ratios, context-zone time, and "
    "counts of compactions / skills / subagents / tool errors) — never their actual conversations, code, "
    "project names, or accounts.\n"
    "Rules:\n"
    "- These metrics are heuristics over observable proxies, NOT a judgement of work quality. Frame everything "
    "as opportunities; never criticise or scold.\n"
    "- Be specific and actionable: 2-4 short tips, each tied to a concrete metric you were given.\n"
    "- Keep it under ~180 words, plain text, no preamble or sign-off.\n"
    "- Context hygiene: time in the Sharp/Good zones is healthy; auto-compactions and long stretches in the "
    "Danger zone (>200k context tokens) suggest compacting earlier. A manual /compact before an auto-compact "
    "is the good habit.\n"
    "- A high cache ratio means well-structured prompts/context (good). Model right-sizing matters (don't run "
    "Opus for trivial turns), but you can't see task difficulty, so suggest gently — never assert a turn was "
    "wrong.\n"
    "- Never invent numbers you weren't given. Never mention specific projects, files, hosts, or accounts."
)


def _trim_bundle(bundle):
    """Keep the prompt lean (dogfood the context discipline): pass the scoring numbers + a compact summary of
    the most-recent session, dropping bulky arrays (e.g. the autopsy's per-turn zone_series)."""
    b = dict(bundle or {})
    ap = b.get("autopsy")
    if isinstance(ap, dict):
        b["autopsy"] = {k: ap.get(k) for k in
                        ("messages", "cost_usd", "duration_secs", "cache_ratio", "healthy_pct",
                         "danger_turns", "model_mix", "events")}
    return b


def build_prompt(bundle):
    """Return {system, user} for the engines. `user` is the content-free metrics bundle as JSON plus a short
    instruction. The worker passes `system` to Ollama directly and prepends it for `claude -p`."""
    payload = json.dumps(_trim_bundle(bundle), sort_keys=True, indent=2)
    user = ("Here are my recent Claude Code metrics (content-free):\n\n" + payload +
            "\n\nCoach me: what am I doing well, and what 2-4 specific things could make me faster or more "
            "efficient? Tie each to a metric above.")
    return {"system": SYSTEM, "user": user}


def flatten(prompt):
    """One combined string for engines that take a single prompt arg (e.g. `claude -p`)."""
    return prompt["system"] + "\n\n---\n\n" + prompt["user"]
