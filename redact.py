"""Best-effort secret redaction for the content index (V13 Slice 2B).

Masks the usual secret shapes before raw transcript text is written to cc-content.db, so an indexed file dump
or command output doesn't persist a token verbatim. It is HEURISTIC — labeled best-effort, not a guarantee —
and OFF by default (cfg.content_redact). Searching the surrounding prose still works; only the secret span is
replaced with a [REDACTED:*] marker.
"""
import re

_PATTERNS = [
    # PEM private-key blocks (whole block)
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "[REDACTED:pem]"),
    # JWTs (three base64url segments)
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED:jwt]"),
    # OpenAI / Anthropic style sk-/rk- keys
    (re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{20,}\b"), "[REDACTED:key]"),
    # GitHub tokens (ghp_/gho_/ghu_/ghs_/ghr_)
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED:gh-token]"),
    # Slack tokens
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED:slack]"),
    # AWS access key id
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws-key]"),
    # Authorization headers
    (re.compile(r"(?i)\bauthorization\b\s*[:=]\s*\S+"), "authorization: [REDACTED]"),
    # Bearer tokens
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{10,}"), "Bearer [REDACTED]"),
    # key=value / key: value secrets (mask the value, keep the key name for context)
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?token|client[_-]?secret)"
                r"(\s*[:=]\s*)([^\s,;)\]\"']{6,})"), r"\1\2[REDACTED]"),
    # ${ENV} interpolations that look secret-bearing
    (re.compile(r"\$\{[A-Za-z0-9_]*(?:TOKEN|SECRET|KEY|PASSWORD|PASSWD|APIKEY)[A-Za-z0-9_]*\}"),
     "[REDACTED:env]"),
]


def redact(text):
    """Apply every pattern. Returns the masked text (best-effort)."""
    if not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text
