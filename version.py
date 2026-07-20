"""Single source of truth for the app version + runtime detection (V13 Slice 1).

VERSION is surfaced on GET /health, in the UI footer, and compared against the update-check manifest (Slice 6).
Bump it per release. runtime() distinguishes a Docker container from a native (pipx/venv) install — it drives
the content-firewall bind (native binds loopback directly; Docker relies on the compose loopback publish) and
the update-check's upgrade verb (docker compose pull vs pipx upgrade).
"""
import os

VERSION = "0.15.0"

# Build stamp: "v<VERSION>" when the image was built from exactly the tagged release on a clean tree, else a
# short commit sha (+"-dirty"). Baked in as CC_BUILD at container-build time by scripts/rebuild.sh. Exists because
# VERSION alone cannot tell "this IS 0.14.0" from "this is 0.14.0 plus nine unreleased commits" — the running
# container reported a clean 0.14.0 for days while serving post-tag code, and nothing surfaced the gap.
# VERSION itself stays a bare semver so is_newer()/the update check are unaffected.
BUILD = os.environ.get("CC_BUILD", "").strip()


def build():
    """The baked-in `git describe` string, or None when the image was built without one."""
    return BUILD or None


def is_release():
    """True when the running code is exactly the tagged VERSION, False when it is ahead of/dirty from the
    tag, and **None when there is no build stamp to judge by**. None is deliberate: an unstamped build must
    not be reported as a clean release — that is precisely the false-confidence this stamp exists to remove."""
    if not BUILD:
        return None
    return BUILD.lstrip("v") == VERSION


def display():
    """Human-facing version string: '0.14.0' on a release, '0.14.0+02fab5f' on anything past the tag."""
    if not BUILD or is_release():
        return VERSION
    suffix = BUILD.lstrip("v")
    if suffix.startswith(VERSION + "-"):
        suffix = suffix[len(VERSION) + 1:]
    return "%s+%s" % (VERSION, suffix)


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
