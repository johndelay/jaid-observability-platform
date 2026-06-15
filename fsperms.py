"""Cross-platform file/dir permission hardening.

The app writes a few sensitive files: cc-content.db (RAW transcript text), cc-usage.db (account
emails + the coach narrative), and export/backup bundles. We want them owner-only, on every OS.

Reality per platform:
  • Linux / macOS (POSIX): real mode bits. We make dirs 0700 and files 0600. Belt-and-suspenders, main()
    also sets umask(0o077) so anything created later (e.g. SQLite's -wal/-shm) is owner-only by default.
  • Windows: os.chmod CANNOT restrict "other users" — it only toggles the read-only attribute. Access is
    governed by NTFS ACLs. Files created under the user profile (the default ~/.cache/... location) inherit
    an ACL that grants only the owning user + SYSTEM + Administrators (NOT "Everyone"), so they are not
    world-readable by default. The genuine Windows risk is relocating a sensitive path (CC_CONTENT_DB_PATH /
    CC_DB_PATH / CC_EXPORT_DIR) onto a shared/world-readable location. So on Windows we no-op chmod and rely
    on the profile ACL; warn_if_exposed() is the cross-platform "did this end up readable?" check.
"""
import os
import stat

IS_WINDOWS = os.name == "nt"


def secure_dir(path):
    """Create `path` (and parents) and, on POSIX, restrict it to the owner (0700). Idempotent; also
    tightens a dir that already exists at looser perms. No-op chmod on Windows (relies on the profile ACL)."""
    os.makedirs(path, exist_ok=True)
    if not IS_WINDOWS:
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def secure_file(*paths):
    """On POSIX, chmod each existing path to 0600 (owner read/write only). Accepts several paths so callers
    can pass a DB plus its -wal/-shm sidecars. No-op on Windows / for missing paths."""
    if IS_WINDOWS:
        return
    for p in paths:
        try:
            if os.path.exists(p):
                os.chmod(p, 0o600)
        except OSError:
            pass


def is_exposed(path):
    """True if `path` exists and is group- or other-accessible (POSIX). Always False on Windows (ACL-based;
    not expressible as mode bits)."""
    if IS_WINDOWS or not os.path.exists(path):
        return False
    try:
        mode = os.stat(path).st_mode
        return bool(mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH))
    except OSError:
        return False


def warn_if_exposed(path, label, log=print):
    """Best-effort startup nudge: if a sensitive file is group/other-accessible, tell the operator how to
    fix it. POSIX-only (Windows ACLs aren't mode bits)."""
    if is_exposed(path):
        try:
            m = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            m = 0
        log(f"[security] {label} ({path}) is group/other-accessible (mode {oct(m)}); "
            f"restrict it:  chmod 600 {path}")
