---
name: ysk-rate
description: Rate a note from Claude Code's built-in "You should know" plugin — was it true, new, consequential, timely, acted on — and record the verdict (GOOD / MEH / BAD + why) to a local ratings file the JAID dashboard reads. Use when the user says "rate that note", "rate the last you-should-know", "was that note any good", "ysk rate", or wants to judge whether a side-agent note ("You should know" / "Heads up") was worth it.
---

# ysk-rate — judge one "You should know" note and record it

Claude Code's built-in **You should know** plugin runs a quiet side agent during long turns and puts a one-line
note above the prompt, tagged **"You should know"** or **"Heads up"**. This skill records whether a note was
worth it, so its value can be weighed against its cost. Everything stays on this machine.

The helper lives next to this file:
```bash
RATE="$(dirname "$(readlink -f ~/.claude/skills/ysk-rate/SKILL.md)")/rate.py"
```

## Procedure

1. **Find the note.** In this order:
   - The user named or pasted a specific note → use that text.
   - This conversation contains a prompt starting `Here is a note offered by a side agent:` (the user pressed
     *Chat in main session*) → the note is its first quoted line, `> <tag> · <note>`. Use the latest one.
   - Otherwise run `python3 "$RATE" latest --session "${CLAUDE_SESSION_ID}"` → the note on screen (this
     session's first), else the newest one the plugin showed. Exit 2 = nothing found: say so and stop.

   **Quote the note back verbatim**, with its tag if known.

2. **Judge five checks** from what this conversation shows — yes/no, one short reason each:
   1. **True** — was the note correct?
   2. **New** — did the user not already know it / hadn't it come up this session?
   3. **Matters** — a real consequence: money, time, wasted work, a wrong result, a live decision?
   4. **Timely** — did it arrive while it could still change something?
   5. **Acted** — did we do something because of it?

   **Verdict:** **GOOD** = true + new + matters · **MEH** = true but not new, or not consequential ·
   **BAD** = wrong, trivia, or too late to matter. Plus **one sentence of why**.

   Show the five checks and the verdict, then ask **one** short question: *"Override any of these?"* The user's
   answer wins. If they've already given their verdict, skip the question.

3. **Record it** — always through the helper (never `echo`/`printf`: quotes in a note would break the line).
   Pass exact JSON on stdin with a quoted heredoc:
   ```bash
   python3 "$RATE" append <<'JSON'
   {"session_id": "${CLAUDE_SESSION_ID}", "tag": "Heads up", "note": "<the note, verbatim>",
    "checks": {"true": true, "new": true, "matters": true, "timely": true, "acted": false},
    "verdict": "GOOD", "why": "<one sentence>"}
   JSON
   ```
   It fills `ts` and `host`, writes one line to `~/.cache/cc-observability/ysk-ratings.jsonl` (mode 600) and
   prints the line. **Show the user that printed line.** Exit 1 = rejected (it says why — fix and retry);
   exit 2 = could not write.

## Notes

- Rating the same note again appends a new line; the newest rating wins.
- The note text is written locally only. Never paste notes or ratings anywhere public — they describe private
  work.
- The JAID dashboard (if running on this machine) picks the line up within a minute as a `ysk_rating` event.
