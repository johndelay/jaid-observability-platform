#!/usr/bin/env python3
"""
cc-observability — cost engine (ccusage model, stdlib only)
===========================================================
Per-token USD pricing for Claude models, used to turn transcript `usage` objects into a
dollar cost for HISTORY/aggregation (the live statusline feed gives `cost.total_cost_usd`
directly; this is for sessions without it + per-project/day rollups).

Four distinct rates per model — input / output / cache-creation / cache-read — because
cache-create ≈ input×1.25 and cache-read ≈ input×0.1; pricing cache at the input rate is wrong.
Numbers per published vendor rate cards.

Model resolution: exact id → normalize (`.`/`@`→`-`, strip a trailing `-YYYYMMDD` date and any
`[1m]` marker) → longest-prefix match. A `<synthetic>` / unknown / missing model costs $0.

200k / long-context tiering: VERIFIED 2026-06-05 against platform.claude.com (models overview +
migration guide) — NO current model has an above-200k premium. Opus 4.6/4.7/4.8 each price the full
1M-token window at the flat standard rate ("1M context window at standard API pricing with no
long-context premium"); Sonnet 4.6 ($3/$15) and Haiku 4.5 ($1/$5) are flat too, as are the listed
legacy models. So the earlier "~5% low = unmodeled 1M above-200k tier" guess was WRONG — there is no
tier to model. The `tiered()` machinery is retained ONLY in case a FUTURE/legacy model reintroduces a
premium (it would carry `*_above` keys + `tier_at`); no entry below populates it, and none should
without an authoritative above-200k number from the pricing page.
"""
import re

# rate = USD per token. Optional tier: add input_above/cache_create_above/cache_read_above/output_above
# + tier_at (token threshold) to price prompt tokens beyond the threshold at the higher rate.
_R = lambda i, o, cc, cr: {"input": i, "output": o, "cache_create": cc, "cache_read": cr}

# Canonical (already-normalized) model id → rates. Longest-prefix match handles dated/variant ids.
PRICES = {
    # Fable 5 / Mythos 5 — flagship tier ($10 / $50 per M in/out; GA 2026-06-09). 1M window, flat (no
    # above-200k tier). Cache rates use the standard multipliers every entry here applies: write(5m) =
    # input×1.25, read = input×0.1. Source: platform.claude.com models overview (fetched 2026-06-12).
    "claude-fable-5":         _R(10e-6, 50e-6, 12.5e-6, 1.0e-6),
    "claude-mythos-5":        _R(10e-6, 50e-6, 12.5e-6, 1.0e-6),   # limited availability (Project Glasswing)
    "claude-mythos-preview":  _R(10e-6, 50e-6, 12.5e-6, 1.0e-6),   # research preview; same Mythos-family rate (price not separately published)
    # Opus 4.5–4.8 (current pricing: $5 / $25 per M in/out)
    "claude-opus-4-8": _R(5e-6, 25e-6, 6.25e-6, 0.5e-6),
    "claude-opus-4-7": _R(5e-6, 25e-6, 6.25e-6, 0.5e-6),
    "claude-opus-4-6": _R(5e-6, 25e-6, 6.25e-6, 0.5e-6),
    "claude-opus-4-5": _R(5e-6, 25e-6, 6.25e-6, 0.5e-6),
    # Legacy/deprecated Opus 4.x ($15 / $75 per M)
    "claude-opus-4-1": _R(15e-6, 75e-6, 18.75e-6, 1.5e-6),   # deprecated (retires 2026-08-05)
    "claude-opus-4-0": _R(15e-6, 75e-6, 18.75e-6, 1.5e-6),   # alias of the dated Opus 4 (deprecated)
    "claude-opus-4":   _R(15e-6, 75e-6, 18.75e-6, 1.5e-6),   # dated id (claude-opus-4-YYYYMMDD) strips to this
    # Sonnet ($3 / $15 per M) — flat, no above-200k tier (verified 2026-06-05)
    "claude-sonnet-4-6": _R(3e-6, 15e-6, 3.75e-6, 0.3e-6),
    "claude-sonnet-4-5": _R(3e-6, 15e-6, 3.75e-6, 0.3e-6),
    "claude-sonnet-4-0": _R(3e-6, 15e-6, 3.75e-6, 0.3e-6),   # alias of the dated Sonnet 4 (deprecated)
    "claude-sonnet-4":   _R(3e-6, 15e-6, 3.75e-6, 0.3e-6),
    # Haiku ($1 / $5 per M)
    "claude-haiku-4-5": _R(1e-6, 5e-6, 1.25e-6, 0.1e-6),
}

# Coarse family detection (substring of the normalized id) + the CURRENT id to price an unknown member of
# that family against. So a brand-new version (or a Bedrock/Vertex-prefixed id like `anthropic.claude-opus-…`)
# prices at the right family rate instead of $0 — and never mis-matches a stale legacy rate. Order matters:
# flagship families are checked first. A truly unknown family → None ($0).
_FAMILIES = ("fable", "mythos", "opus", "sonnet", "haiku")
_FAMILY_DEFAULT = {
    "fable":  "claude-fable-5",
    "mythos": "claude-mythos-5",
    "opus":   "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku":  "claude-haiku-4-5",
}

_DATE_SUFFIX = re.compile(r"-\d{8}$")


def normalize(model):
    """exact id → comparable canonical form: lowercase, `.`/`@`→`-`, drop `[1m]`, strip a trailing date."""
    if not model:
        return ""
    m = str(model).lower().strip()
    m = m.replace("[1m]", "").replace("@", "-").replace(".", "-")
    m = _DATE_SUFFIX.sub("", m)
    return m


def family_of(model):
    """Coarse family of a model id: fable | mythos | opus | sonnet | haiku | "" (unknown/synthetic/None).
    Substring match on the normalized id, so Bedrock/Vertex-prefixed ids classify too."""
    m = normalize(model)
    if not m or "synthetic" in m:
        return ""
    for fam in _FAMILIES:
        if fam in m:
            return fam
    return ""


def rate_for(model):
    """Rate dict for a model id: exact id (after normalize) → its family's CURRENT rate → None ($0).
    The family fallback means a brand-new version (or a Bedrock/Vertex-style id) prices at the right
    family rate instead of $0 — and never mis-matches a stale legacy rate. Unknown family/synthetic → $0."""
    m = normalize(model)
    if not m or "synthetic" in m:
        return None
    if m in PRICES:
        return PRICES[m]
    fam = family_of(m)
    return PRICES[_FAMILY_DEFAULT[fam]] if fam in _FAMILY_DEFAULT else None


def tiered(tokens, base, above, tier_at=200_000):
    """Price `tokens` with a 200k tier: tokens up to tier_at at `base`, the rest at `above`."""
    if tokens <= tier_at or above is None:
        return tokens * base
    return tier_at * base + (tokens - tier_at) * above


def _priced(tokens, r, kind, prompt_tokens):
    """Cost for one token bucket, applying the model's 200k tier if it defines one.
    `prompt_tokens` (input+cache) is the size used to decide which side of the tier we're on."""
    tokens = tokens or 0
    base = r[kind]
    above = r.get(kind + "_above")
    if above is None:
        return tokens * base
    # tier on the total prompt size, not the bucket alone (ccusage tiers per request)
    tier_at = r.get("tier_at", 200_000)
    return tiered(tokens, base, above, tier_at) if prompt_tokens > tier_at else tokens * base


def line_cost(usage, model):
    """USD cost of one transcript `usage` object for `model`. Unknown/missing model → $0."""
    r = rate_for(model)
    if not r or not isinstance(usage, dict):
        return 0.0
    it = int(usage.get("input_tokens", 0) or 0)
    cc = int(usage.get("cache_creation_input_tokens", 0) or 0)
    cr = int(usage.get("cache_read_input_tokens", 0) or 0)
    ot = int(usage.get("output_tokens", 0) or 0)
    prompt = it + cc + cr
    return (_priced(it, r, "input", prompt)
            + _priced(cc, r, "cache_create", prompt)
            + _priced(cr, r, "cache_read", prompt)
            + _priced(ot, r, "output", prompt))


def reprice(model, input_tokens=0, output_tokens=0, cache_read=0, cache_creation=0):
    """Cost of a set of stored token counts under `model`'s rates (V11 what-if repricing). Pure arithmetic on
    already-stored counts — no quality claim, just rate arbitrage. Unknown/synthetic model → $0."""
    return line_cost({"input_tokens": input_tokens, "output_tokens": output_tokens,
                      "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_creation}, model)


if __name__ == "__main__":
    # quick sanity: opus-4-8 line
    u = {"input_tokens": 2, "cache_creation_input_tokens": 4287, "cache_read_input_tokens": 271700, "output_tokens": 415}
    print("opus-4-8 line:", round(line_cost(u, "claude-opus-4-8"), 6), "USD")
    print("dated id resolves:", rate_for("claude-opus-4-8-20260115") is not None)
    print("synthetic -> $0:", line_cost(u, "<synthetic>"))
