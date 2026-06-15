# Add a Mac (Claude Desktop) to your fleet — watcher install

This guide sets up the JAID **watcher** on a Mac so its Claude sessions show up on an
**already-running** JAID dashboard. It is written to be followed **by an AI agent** (Claude Desktop's
agent mode, or Claude Code) — paste the prompt below and let it drive, or follow the steps yourself.

> **You need a dashboard already running somewhere** (see the README for the Docker or native install).
> This guide only adds a *reporting* host. If you want the dashboard itself on this Mac, use the
> `jaid-setup` skill instead.

---

## Paste this to your agent

```
Set up the JAID watcher on this Mac following docs/WATCHER_MACOS.md. It should report into my
existing dashboard. Before you change anything outside a repo checkout, tell me what you're about to
do and wait for a yes. Ask me for: (1) my dashboard's ingest URL (http://<host>:8099/ingest), and
(2) my CC_INGEST_TOKEN if the dashboard requires one. Never print the token, never commit it, never
write it to /tmp.
```

---

## First, what JAID can and cannot see on a Mac

JAID watches **Claude Code transcript JSONL files**. On a Mac, these come from two sources:

| Source | Writes transcripts? | Where |
|--------|--------------------|-------|
| **Claude Desktop → agent mode** ("Cowork" / local agent) | ✅ yes (runs the Code engine) | `~/Library/Application Support/Claude/local-agent-mode-sessions/` |
| **Claude Code CLI** (if you use it in Terminal) | ✅ yes | `~/.claude/projects/` |
| **Claude Desktop → ordinary chat** | ❌ no | — (nothing to watch) |

So a Mac that only runs **Claude Desktop** is a **watcher-only host**. You'll get session
**state, "needs you", context tokens, and the dumb-zone bar**. You will **not** get **cost / burn /
rate-limit gauges** — those come from the Claude Code statusline hook, which Desktop agent mode does
not run. That's expected, not a bug.

---

## The macOS gotchas (why this needs its own guide)

1. **There is no turnkey macOS installer.** `deploy-watcher.sh` is Linux/systemd only. On macOS you
   install a **launchd LaunchAgent** by hand (or the agent writes it for you).
2. **You must set `CC_PROJECTS_DIRS` explicitly.** The watcher auto-includes a *Linux* extra path
   (`~/.config/Claude/local-agent-mode-sessions`). Claude Desktop on macOS stores transcripts under
   **`~/Library/Application Support/Claude/local-agent-mode-sessions`** instead, so the auto-include
   finds nothing. You must point `CC_PROJECTS_DIRS` at the macOS path.
3. **`CC_PROJECTS_DIRS` *replaces* the default**, it does not add to it. To capture **both** Claude
   Code CLI and Claude Desktop agent mode, list **both paths, comma-separated**.
4. **Full Disk Access may be required.** A launchd agent often cannot read
   `~/Library/Application Support/` until you grant Full Disk Access to the interpreter it runs
   (`python3`). Symptom: the watcher runs and beacons "green" but reports **0 sessions** even though
   transcript files exist.
5. **0 sessions when idle is normal.** The watcher only reports sessions touched within
   `CC_ACTIVE_HOURS` (default **6h**). A quiet Mac legitimately shows 0 — it lights up when you next
   use Claude there.

---

## Procedure

### 0. Gather inputs
- **Dashboard ingest URL** — `http://<dashboard-host>:8099/ingest`.
- **Ingest token** — only if the dashboard sets `CC_INGEST_TOKEN`. Ask the user for it.
  **Treat it as a secret: never echo it, never commit it, never write it to `/tmp`.** It goes only
  into the LaunchAgent plist in the user's home directory.
- **A label** for this Mac (`CC_HOST`), e.g. `my-mac`.
- **`python3`** must exist (`python3 --version`). macOS ships it with the Xcode Command Line Tools;
  otherwise `xcode-select --install` or Homebrew.

### 1. Get `watcher.py` onto the Mac
Either clone the repo, or copy the single file. Then confirm it's the **token-aware** build:
```bash
mkdir -p ~/.local/share/cc-observability
# clone, or: scp <somewhere>/watcher.py ~/.local/share/cc-observability/watcher.py
grep -c X-CC-Token ~/.local/share/cc-observability/watcher.py   # must print 2 (0 = stale build → 401s)
```

### 2. Discover which transcript dirs actually exist
```bash
ls -d ~/.claude/projects 2>/dev/null && echo "  -> Claude Code CLI transcripts present"
ls -d ~/Library/Application\ Support/Claude/local-agent-mode-sessions 2>/dev/null \
  && echo "  -> Claude Desktop agent-mode transcripts present"
```
Build `CC_PROJECTS_DIRS` from whichever exist (comma-separated, full paths). Most Macs want **both**.

### 3. Write the LaunchAgent plist
Create `~/Library/LaunchAgents/com.ccobs.watcher.plist`. Substitute the placeholders. If a value
doesn't apply (e.g. no token), omit that key/string pair.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.ccobs.watcher</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/USERNAME/.local/share/cc-observability/watcher.py</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>CC_COLLECTOR_URL</key><string>http://DASHBOARD-HOST:8099/ingest</string>
    <key>CC_HOST</key><string>MY-MAC</string>
    <key>CC_INGEST_TOKEN</key><string>TOKEN-IF-REQUIRED</string>
    <key>CC_PROJECTS_DIRS</key><string>/Users/USERNAME/.claude/projects,/Users/USERNAME/Library/Application Support/Claude/local-agent-mode-sessions</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/ccobs-watcher.err</string>
  <key>StandardOutPath</key><string>/tmp/ccobs-watcher.out</string>
</dict></plist>
```

> **Agent tip:** to set the token safely without it appearing on screen or in shell history, edit the
> plist with `plistlib` and feed the token via stdin rather than as a command argument. Example:
> ```bash
> # token arrives on stdin; the script never echoes it
> printf '%s' "$INGEST_TOKEN" | python3 -c '
> import sys,plistlib,os
> tok=sys.stdin.read().strip()
> P=os.path.expanduser("~/Library/LaunchAgents/com.ccobs.watcher.plist")
> d=plistlib.load(open(P,"rb"))
> d.setdefault("EnvironmentVariables",{})["CC_INGEST_TOKEN"]=tok
> plistlib.dump(d,open(P,"wb"))
> print("token written, length", len(tok))'
> ```

Validate the file: `plutil -lint ~/Library/LaunchAgents/com.ccobs.watcher.plist` → `OK`.

### 4. Grant Full Disk Access (if reading `~/Library/Application Support`)
System Settings → Privacy & Security → **Full Disk Access** → add the interpreter the plist runs
(`/usr/bin/python3`, or your Homebrew `python3`). Skip if you're only watching `~/.claude/projects`.

### 5. Load and check locally
```bash
launchctl unload ~/Library/LaunchAgents/com.ccobs.watcher.plist 2>/dev/null
launchctl load   ~/Library/LaunchAgents/com.ccobs.watcher.plist
launchctl list | grep ccobs            # shows PID and exit code 0
tail -n 5 /tmp/ccobs-watcher.err       # look for: host=MY-MAC roots=[...] -> .../ingest every 10.0s
```
The startup line should list **both** roots you configured. If `roots=[...]` is missing your Cowork
path, fix `CC_PROJECTS_DIRS` and reload.

### 6. Confirm on the dashboard
Open the dashboard UI → **Fleet** scene → the new host should appear **green**. (Or the operator can
check `GET /hosts`.) Remember step 5's note: if the Mac has been idle longer than `CC_ACTIVE_HOURS`
(6h), it correctly shows **0 sessions** — use Claude on the Mac and re-check within the window.

### 7. Report back
Tell the user: the host label, that it's **watcher-only** (state/context yes; cost/rate-limits no),
where the logs are (`/tmp/ccobs-watcher.*`), and how to manage it (below).

---

## Manage / uninstall
```bash
# restart after a config edit
launchctl unload ~/Library/LaunchAgents/com.ccobs.watcher.plist && \
launchctl load   ~/Library/LaunchAgents/com.ccobs.watcher.plist

# uninstall
launchctl unload ~/Library/LaunchAgents/com.ccobs.watcher.plist
rm ~/Library/LaunchAgents/com.ccobs.watcher.plist
rm -rf ~/.local/share/cc-observability
```

## Troubleshooting
| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Host never appears on dashboard | wrong `CC_COLLECTOR_URL`, or network can't reach `:8099` | check the URL; `curl http://DASHBOARD-HOST:8099/health` from the Mac |
| Host appears but the dashboard logs **rejected** POSTs | wrong/missing `CC_INGEST_TOKEN` | set the token to match the dashboard's `.env`, reload |
| `grep -c X-CC-Token watcher.py` prints 0 | stale watcher build | re-copy `watcher.py` from a current checkout |
| Green but **0 sessions** while files exist | Full Disk Access not granted, or `CC_PROJECTS_DIRS` wrong | grant FDA to `python3`; confirm the Cowork path; check the `roots=[...]` startup line |
| Green, 0 sessions, Mac is idle | nothing touched within `CC_ACTIVE_HOURS` | normal — use Claude and re-check within 6h |

## Security notes
- The watcher is **read-only** and makes only LAN POSTs to your own dashboard — no third-party egress.
- The **ingest token** is the one secret here. Keep it out of the terminal, out of git, and out of
  `/tmp`; it belongs only in the plist under the user's home. To rotate it, change it on the dashboard
  `.env` and update every watcher's token to match.
