# Content-index privacy

Everything in JAID Observability Platform is content-free (token counts + timestamps) **except one opt-in feature:
transcript search** (`cc-content.db`). That index holds the **raw text** of your sessions — your prose, file
dumps, and command output — which can include secrets that scrolled past (a token in a `cat .env`, an API
response). This doc is how we keep that risk bounded, and what you can turn up if you want more.

## What protects you (in order of how much it helps)

1. **Opt-in.** Nothing is indexed until you click *Enable content search*. *Delete index* wipes it.
2. **Local-only.** The index is served only on the loopback content port — never the LAN/your phone.
3. **14-day auto-purge (default).** Indexed content older than the retention window is deleted automatically.
   Since the index is rebuildable from your transcripts, a tight default is cheap and shrinks a leak's blast
   radius to "the last two weeks." Change it in the 🔐 Privacy controls (7 / 14 / 30 / 90 days, or *Off*).
   *Off* keeps content until you Delete index — larger exposure; use it deliberately.
4. **Best-effort secret redaction (optional, default OFF).** Masks common secret shapes (API/AWS keys,
   bearer/Authorization, `password=`, PEM blocks, JWTs, `${TOKEN}`) before text is written. Heuristic — a
   reducer, not a guarantee. Applies to newly-indexed content.
5. **Conversation-only scope (optional).** Excludes `tool_result` (file dumps / command output) — the biggest
   secret source — at the cost of not being able to search command output. Default is *Everything* (max recall).

## Encryption at rest

To keep search working, the engine must read plaintext, so the app does **not** encrypt the index file itself
(column encryption would break the index). The right tool is **whole-disk / volume encryption**, which is
transparent to the app:

- **macOS:** FileVault — one toggle (Secure Enclave auto-unlocks at login). Recommended.
- **Windows:** BitLocker — one toggle on Pro (TPM auto-unlock). Recommended.
- **Linux:** LUKS — strongest, but a **bigger commitment**: it's set up at install (converting later means
  backup→reformat→restore), and a headless server needs a boot passphrase or TPM2 auto-unlock. Worth it for a
  laptop; a deliberate decision for an always-on server.

App-managed file-level encryption (SQLCipher, with an unlock-on-start step) is a planned **opt-in** for a
later release. Until then: opt-in + short retention + (optionally) redaction, plus your host's disk encryption.

## TL;DR

Enable it if it's useful; leave the 14-day default on; turn on redaction and/or conversation-only if your
sessions touch sensitive systems; and encrypt your disk. Anything you *export* to share is a separate path and
is always encrypted before it leaves the machine (see `DATA_EXPORT.md`).
