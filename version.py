"""Single source of truth for the app version + runtime detection (V13 Slice 1).

VERSION is surfaced on GET /health, in the UI footer, and compared against the update-check manifest (Slice 6).
Bump it per release. runtime() distinguishes a Docker container from a native (pipx/venv) install — it drives
the content-firewall bind (native binds loopback directly; Docker relies on the compose loopback publish) and
the update-check's upgrade verb (docker compose pull vs pipx upgrade).
"""
import os

VERSION = "0.14.0"


def runtime():
    """'docker' when running inside a container, else 'native'. Override with CC_RUNTIME=docker|native."""
    r = os.environ.get("CC_RUNTIME", "").strip().lower()
    if r in ("docker", "native"):
        return r
    return "docker" if os.path.exists("/.dockerenv") else "native"


def _parse(v):
    """Loose semver → comparable int tuple ('0.13.0' → (0,13,0); ignores pre-release suffixes)."""
    out = []
    for p in str(v).split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out)


def is_newer(latest, current):
    """True if `latest` is a newer version than `current`."""
    try:
        return _parse(latest) > _parse(current)
    except (ValueError, TypeError):
        return False


def upgrade_verb(rt=None):
    """The per-runtime upgrade command shown in the update banner."""
    rt = rt or runtime()
    return "docker compose pull && docker compose up -d" if rt == "docker" else "pipx upgrade cc-observability"
