// Real-browser E2E smoke (Chromium via Playwright) against a LIVE dashboard.
// Complements tests/ui_smoke.js: that runs the page's render() in a *stubbed* DOM (no browser); this loads
// the ACTUAL page in real Chromium against a running server, so it catches the integration/runtime bugs the
// stub can't — uncaught exceptions, broken fetch/SSE wiring, a drill-in that never leaves "loading…"
// (the esc() regression), and CSS/layout breakage (via the screenshot artifact). Exits non-zero on failure.
//
//   CC_URL=http://localhost:8099 node tests/e2e_playwright.js
//
// Skips cleanly (exit 0) if `playwright` isn't installed or the dashboard is unreachable, so verify.sh stays
// green on hosts without the browser toolchain. Install: `npm install && npx playwright install chromium`.

let chromium;
try { ({ chromium } = require('playwright')); }
catch { console.log('  SKIP (playwright not installed — run `npm install && npx playwright install chromium` in repo root)'); process.exit(0); }

const fs = require('fs');
const path = require('path');
const URL = process.env.CC_URL || 'http://localhost:8099';

const results = [];
async function check(name, fn) {
  try { await fn(); results.push([true, name]); }
  catch (e) { results.push([false, name + '  →  ' + (e && e.message ? e.message : e)]); }
}

// Console errors we deliberately ignore: optional PWA assets the bare server may 404, and the auto favicon.
// These are real-but-irrelevant for a render smoke; we still hard-fail on any UNCAUGHT page exception.
const BENIGN = [/sw\.js/, /manifest\.webmanifest/, /favicon/, /service ?worker/i];

(async () => {
  const browser = await chromium.launch({ headless: true });
  // phone-ish viewport — this is a mobile "fuel gauge", so test the surface it's actually used on
  const ctx = await browser.newContext({ viewport: { width: 420, height: 900 } });
  const page = await ctx.newPage();

  const pageErrors = [];
  const consoleErrors = [];
  page.on('pageerror', e => pageErrors.push(String(e)));
  page.on('console', m => {
    if (m.type() !== 'error') return;
    const where = (m.location() && m.location().url) || '';
    const txt = m.text();
    if (BENIGN.some(r => r.test(txt) || r.test(where))) return;
    consoleErrors.push(txt + (where ? '  @ ' + where : ''));
  });

  // If the UI is PIN-gated, log in first (ctx.request shares cookies with the browser context).
  // The PIN comes from the env (verify.sh exports it from .env) — never hard-coded.
  const PIN = process.env.CC_ACCESS_PIN || process.env.CC_PIN || '';
  if (PIN) { try { await ctx.request.post(URL + '/login', { form: { pin: PIN } }); } catch {} }

  let reachable = true;
  try {
    const resp = await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 8000 });
    if (!resp || !resp.ok()) reachable = false;
  } catch { reachable = false; }

  if (!reachable) {
    console.log('  SKIP (dashboard not reachable at ' + URL + ' — start the container or set CC_URL)');
    await browser.close();
    process.exit(0);
  }

  // PIN gate with no/invalid PIN available → the app redirects to /login; can't exercise the UI. Skip cleanly.
  if (/\/login$/.test(page.url())) {
    console.log('  SKIP (dashboard is PIN-gated and no valid CC_ACCESS_PIN available to the harness)');
    await browser.close();
    process.exit(0);
  }

  await check('page title is the JAID Observability Platform', async () => {
    const t = await page.title();
    if (!/JAID Observability Platform/i.test(t)) throw new Error('unexpected title: ' + JSON.stringify(t));
  });

  await check('fleet renders (hero + content) within 8s', async () => {
    // #app fills from the SSE/state feed; accept either a session card or the "All clear" hero.
    // NB: waitForFunction signature is (fn, arg, options) — timeout MUST be the 3rd arg.
    await page.waitForFunction(() => {
      const a = document.getElementById('app');
      return a && a.innerHTML.length > 50 && (a.querySelector('[data-sid]') || a.querySelector('.needs'));
    }, undefined, { timeout: 8000, polling: 100 });
  });

  let sid = null;
  await check('shows session cards OR a clean "all clear" hero', async () => {
    sid = await page.evaluate(() => {
      const el = document.querySelector('[data-sid]');
      return el ? el.getAttribute('data-sid') : null;
    });
    const clear = await page.$('.needs.clear');
    if (!sid && !clear) throw new Error('neither sessions nor all-clear hero present');
  });

  if (sid) {
    // The regression that shipped broken: drill-in hung on "loading…" forever (undefined esc()).
    // A real click must open the overlay AND replace "loading…" with real content (items or a note).
    await check('drill-in opens and resolves past the loading placeholder (esc() regression guard)', async () => {
      // Dispatch the click on the card element itself (bubbles to #app's delegated handler) rather than a
      // coordinate click — the sticky hero `.summary` can overlap a top card and intercept a positional click.
      await page.$eval('#app [data-sid="' + sid + '"]', el => el.click());
      // Assert resolved DOM STRUCTURE, not a substring: activity items render as `.ai` divs; the loading
      // state is a single `.muted` div reading "loading…". (Substring checks for "loading" are unsafe here —
      // this very session's activity text contains the word, which is exactly the bug we were hunting.)
      await page.waitForFunction(() => {
        const d = document.getElementById('detail');
        const s = document.getElementById('dstream');
        if (!d || d.hidden !== false || !s) return false;
        if (s.querySelector('.ai')) return true;                  // real activity items rendered
        const note = s.querySelector('.muted');                   // else a note/empty/error state...
        return note && !note.textContent.includes('loading');     // ...as long as it's not the placeholder
      }, undefined, { timeout: 8000, polling: 100 });
    });

    // Answer-from-phone reply bar: it must EXIST, and its visibility must match the open session's
    // answerable + reply_enabled flags (true only with a tmux target AND a server PIN). PIN-agnostic:
    // on a no-PIN live server every session is reply_enabled:false, so we assert the bar stays hidden.
    await check('reply bar visibility matches the session answerable + reply_enabled flags', async () => {
      const v = await page.evaluate((id) => {
        const card = (window._cards || {})[id] || {};
        const bar = document.getElementById('dreply');
        return { exists: !!bar, hidden: bar ? bar.hidden : null,
                 expect: !!(card.answerable && card.reply_enabled) };
      }, sid);
      if (!v.exists) throw new Error('#dreply element missing');
      if (v.hidden === v.expect) throw new Error(`reply bar hidden=${v.hidden} but expected visible=${v.expect}`);
    });

    await check('drill-in shows a resume command (claude --resume <id>)', async () => {
      const ok = await page.evaluate(() => {
        const d = document.getElementById('dstats');
        return !!d && /claude --resume [0-9a-f-]{8}/.test(d.innerHTML) && d.innerHTML.includes('data-copy');
      });
      if (!ok) throw new Error('resume command + copy affordance missing from drill-in');
    });

    await check('drill-in exposes an Archive (hide) action', async () => {
      const has = await page.evaluate(() => { const d = document.getElementById('dstats'); return !!d && d.innerHTML.includes('data-action='); });
      if (!has) throw new Error('Archive/hide action missing from drill-in');
    });

    await check('drill-in shows a context-health (dumb-zone) banner', async () => {
      const has = await page.evaluate(() => { const d = document.getElementById('dstats'); return !!d && /class="zone z-(sharp|good|drift|danger|auto)"/.test(d.innerHTML); });
      if (!has) throw new Error('dumb-zone banner missing from drill-in');
    });

    await check('/history returns a points array for the open session', async () => {
      const ok = await page.evaluate(async (id) => {
        const r = await fetch('/history?id=' + encodeURIComponent(id)); if (!r.ok) return false;
        const d = await r.json(); return Array.isArray(d.points);
      }, sid);
      if (!ok) throw new Error('/history did not return a points array');
    });

    await check('/session-events returns a counts object for the open session', async () => {
      const ok = await page.evaluate(async (id) => {
        const r = await fetch('/session-events?id=' + encodeURIComponent(id)); if (!r.ok) return false;
        const d = await r.json(); return d && typeof d.counts === 'object' && Array.isArray(d.items);
      }, sid);
      if (!ok) throw new Error('/session-events did not return {counts, items}');
    });
  } else {
    results.push([true, 'drill-in skipped (idle fleet — no sessions to open)']);
  }

  // Phase 1 Foundation: scene carousel + nav bar + hamburger menu must be present and functional.
  await check('nav bar + scene dots render (one dot per visible scene)', async () => {
    const v = await page.evaluate(() => {
      const nb = document.getElementById('navbar');
      const dots = document.querySelectorAll('#navdots i').length;
      return { nb: !!nb, dots };
    });
    if (!v.nb) throw new Error('#navbar missing');
    if (v.dots < 2) throw new Error('expected ≥2 scene dots (Triage + Help), got ' + v.dots);
  });

  await check('nav: ► button does not shift horizontally when the scene label changes width', async () => {
    // #navlabel sits left of the dots + ► — if it resizes per scene it shoves ► out from under the cursor.
    // A fixed min-width + centered label keeps ►'s x-position constant. Measure it on a short label (Triage)
    // vs the longest (Efficiency) and assert the button hasn't moved.
    const measure = async (sceneIdx) => page.evaluate((i) => {
      window.goScene && window.goScene(i);
      const b = document.getElementById('navnext').getBoundingClientRect();
      return { left: Math.round(b.left), label: (document.getElementById('navlabel') || {}).textContent || '' };
    }, sceneIdx);
    const triage = await measure(0);                 // "TRIAGE …" (short)
    const eff = await measure(6);                     // "EFFICIENCY …" (longest)
    if (triage.label === eff.label) throw new Error('scene label did not change between scenes 0 and 6 — test invalid');
    if (Math.abs(triage.left - eff.left) > 1)
      throw new Error(`► button moved ${Math.abs(triage.left - eff.left)}px when label changed ("${triage.label}" left=${triage.left} vs "${eff.label}" left=${eff.left}) — layout shift not fixed`);
    await page.evaluate(() => window.goScene && window.goScene(0)); // restore
  });

  await check('carousel transport: Cost + History + Trophy + Craft + Efficiency + Fleet + Archive + Maintenance + About + Help scenes reachable & populated', async () => {
    // scene 1 = Cost, 2 = History, 3 = Trophy, 4 = Craft, 5 = Coach, 6 = Efficiency, 7 = MCP, 8 = Fleet, 9 = Archive, 10 = Maintenance, 11 = About, 12 = Help
    await page.evaluate(() => window.goScene && window.goScene(1));
    await page.waitForFunction(() => {
      const t = document.getElementById('track'), cost = document.getElementById('scene-cost');
      return t && /translateX\(-(?!0px)/.test(t.style.transform) && cost && /Cost &amp; Accounts|Cost & Accounts/.test(cost.innerHTML);
    }, undefined, { timeout: 4000, polling: 100 });
    await page.evaluate(() => window.goScene && window.goScene(2));
    await page.waitForFunction(() => {
      const hist = document.getElementById('scene-history');
      return hist && /History/.test(hist.innerHTML) && !/Loading history/.test(hist.innerHTML);
    }, undefined, { timeout: 4000, polling: 100 });
    await page.evaluate(() => window.goScene && window.goScene(3));
    await page.waitForFunction(() => {
      const tr = document.getElementById('scene-trophy');
      return tr && /Trophy Room/.test(tr.innerHTML) && !/Loading trophy/.test(tr.innerHTML);
    }, undefined, { timeout: 4000, polling: 100 });
    await page.evaluate(() => window.goScene && window.goScene(4));
    await page.waitForFunction(() => {
      const cr = document.getElementById('scene-craft');
      return cr && /Craft Score/.test(cr.innerHTML) && !/Loading Craft/.test(cr.innerHTML);
    }, undefined, { timeout: 4000, polling: 100 });
    await page.evaluate(() => window.goScene && window.goScene(5));
    await page.waitForFunction(() => {
      const co = document.getElementById('scene-coach');
      return co && /Coach/.test(co.innerHTML) && !/Loading coach/.test(co.innerHTML);
    }, undefined, { timeout: 4000, polling: 100 });
    await page.evaluate(() => window.goScene && window.goScene(6));
    await page.waitForFunction(() => {
      const eff = document.getElementById('scene-efficiency');
      return eff && /Efficiency/.test(eff.innerHTML) && !/Loading efficiency/.test(eff.innerHTML);
    }, undefined, { timeout: 4000, polling: 100 });
    await page.evaluate(() => window.goScene && window.goScene(7));
    await page.waitForFunction(() => {
      const mcp = document.getElementById('scene-mcp');
      return mcp && /MCP tax/.test(mcp.innerHTML) && !/Loading MCP/.test(mcp.innerHTML);
    }, undefined, { timeout: 4000, polling: 100 });
    await page.evaluate(() => window.goScene && window.goScene(8));
    await page.waitForFunction(() => {
      const fleet = document.getElementById('scene-fleet');
      return fleet && /Fleet/.test(fleet.innerHTML) && !/Loading fleet/.test(fleet.innerHTML);
    }, undefined, { timeout: 4000, polling: 100 });
    await page.evaluate(() => window.goScene && window.goScene(9));
    await page.waitForFunction(() => {
      const arch = document.getElementById('scene-archive');
      return arch && /Archive/.test(arch.innerHTML);
    }, undefined, { timeout: 4000, polling: 100 });
    await page.evaluate(() => window.goScene && window.goScene(10));
    await page.waitForFunction(() => {
      const mt = document.getElementById('scene-maint');
      return mt && /Maintenance/.test(mt.innerHTML) && /Storage/.test(mt.innerHTML) && !/Loading maintenance/.test(mt.innerHTML);
    }, undefined, { timeout: 4000, polling: 100 });
    await page.evaluate(() => window.goScene && window.goScene(11));
    await page.waitForFunction(() => {
      const ab = document.getElementById('scene-about');
      return ab && /About/.test(ab.innerHTML) && !/Loading/.test(ab.innerHTML);
    }, undefined, { timeout: 4000, polling: 100 });
    await page.evaluate(() => window.goScene && window.goScene(12));
    await page.waitForFunction(() => {
      const help = document.getElementById('scene-help');
      return help && help.textContent.includes('context window');
    }, undefined, { timeout: 4000, polling: 100 });
    await page.evaluate(() => window.goScene && window.goScene(0)); // back to Triage for the screenshot
  });

  await check('🔍 Search scene: the content firewall walls raw content on the LAN port', async () => {
    // Search is opt-in OFF (not in the carousel by default), but the scene element exists. Probing the LIVE
    // :8099 (the LAN port) must report local:false → the UI renders the local-only wall, never content.
    await page.evaluate(() => window.pollSearchMeta && window.pollSearchMeta());
    await page.waitForFunction(() => {
      const s = document.getElementById('scene-search');
      return s && /Local-only/.test(s.innerHTML) && /localhost:/.test(s.innerHTML);
    }, undefined, { timeout: 4000, polling: 100 });
    // and it must NOT have rendered a search box (no content surface on the LAN port)
    const hasBox = await page.evaluate(() => /id="search-q"/.test(document.getElementById('scene-search').innerHTML));
    if (hasBox) throw new Error('search box rendered on the LAN port — firewall leak');
  });

  await check('🧩 MCP "?" opens the help modal; ✕ and Escape close it', async () => {
    await page.evaluate(() => window.goScene && window.goScene(7));
    await page.waitForFunction(() => {
      const mcp = document.getElementById('scene-mcp');
      return mcp && /MCP tax/.test(mcp.innerHTML) && !/Loading MCP/.test(mcp.innerHTML);
    }, undefined, { timeout: 4000, polling: 100 });
    // click the ? affordance → modal becomes visible with the populate + auto-run content
    await page.$eval('#scene-mcp [data-mcphelp]', el => el.click());
    await page.waitForFunction(() => {
      const m = document.getElementById('mcphelp');
      return m && !m.hidden && /mcp_probe\.py/.test(m.innerHTML) && /SessionStart/.test(m.innerHTML);
    }, undefined, { timeout: 3000, polling: 100 });
    // ✕ closes
    await page.$eval('#mcphelpclose', el => el.click());
    await page.waitForFunction(() => { const m = document.getElementById('mcphelp'); return m && m.hidden; },
      undefined, { timeout: 3000, polling: 100 });
    // re-open, then Escape closes
    await page.$eval('#scene-mcp [data-mcphelp]', el => el.click());
    await page.waitForFunction(() => { const m = document.getElementById('mcphelp'); return m && !m.hidden; },
      undefined, { timeout: 3000, polling: 100 });
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => { const m = document.getElementById('mcphelp'); return m && m.hidden; },
      undefined, { timeout: 3000, polling: 100 });
    await page.evaluate(() => window.goScene && window.goScene(0)); // back to Triage
  });

  await check('Trophy scene renders all-time totals + a streak (or an honest empty state)', async () => {
    await page.evaluate(() => window.goScene && window.goScene(3));
    await page.waitForFunction(() => {
      const tr = document.getElementById('scene-trophy'); if (!tr) return false;
      const h = tr.innerHTML;
      const populated = /all-time spend/.test(h) && /Streak/.test(h);
      const empty = /No history yet|Couldn’t build/.test(h);
      return populated || empty;
    }, undefined, { timeout: 5000, polling: 100 });
    await page.evaluate(() => window.goScene && window.goScene(0)); // back to Triage
  });

  await check('Craft scene renders a score breakdown (or an honest unscored state)', async () => {
    await page.evaluate(() => window.goScene && window.goScene(4));
    await page.waitForFunction(() => {
      const cr = document.getElementById('scene-craft'); if (!cr) return false;
      const h = cr.innerHTML;
      // either a scored breakdown (composite + the 3 dims) or the honest "not enough signal" / "no data" state
      const scored = /\/100/.test(h) && /Efficiency/.test(h) && /Hygiene/.test(h) && /Trophy case/.test(h);
      const unscored = /Not enough signal|No data yet/.test(h);
      return scored || unscored;
    }, undefined, { timeout: 5000, polling: 100 });
    await page.evaluate(() => window.goScene && window.goScene(0)); // back to Triage
  });

  await check('Coach scene renders the Claude Code handoff + content-free honesty banner', async () => {
    await page.evaluate(() => window.goScene && window.goScene(5));
    await page.waitForFunction(() => {
      const co = document.getElementById('scene-coach'); if (!co) return false;
      const h = co.innerHTML;
      // the handoff + Autopsy section + the always-on honesty banner (or the honest no-data state)
      const populated = /Coach me with Claude Code/.test(h) && /Session Autopsy/.test(h);
      const empty = /No data yet/.test(h);
      return (populated || empty) && /content-free/.test(h);
    }, undefined, { timeout: 5000, polling: 100 });
    await page.evaluate(() => window.goScene && window.goScene(0)); // back to Triage
  });

  await check('Efficiency scene renders savings opportunities (or an honest empty state)', async () => {
    await page.evaluate(() => window.goScene && window.goScene(6));
    await page.waitForFunction(() => {
      const e = document.getElementById('scene-efficiency'); if (!e) return false;
      const h = e.innerHTML;
      // either a savings card / policy list, the "already right-sized" note, or the honest no-data state
      const populated = /What-if downgrade policies|already looks right-sized/.test(h);
      const empty = /No data yet|Couldn’t compute/.test(h);
      return (populated || empty) && /opportunities, not judgments/.test(h);   // the honesty framing must always show
    }, undefined, { timeout: 5000, polling: 100 });
    await page.evaluate(() => window.goScene && window.goScene(0)); // back to Triage
  });

  await check('screenshot-safe by default: no email address rendered in the Cost scene', async () => {
    const leak = await page.evaluate(() => {
      const c = document.getElementById('scene-cost');
      return c ? c.innerHTML.includes('@') : false;   // masked accounts are codenames; an @ means a leaked email
    });
    if (leak) throw new Error('an email (@) is rendered while masked — screenshot-safe default is broken');
  });

  await check('account chips: filter then clear (or hidden when ≤1 account)', async () => {
    const st = await page.evaluate(() => {
      const a = document.getElementById('accounts');
      return { hidden: a ? a.hidden : true, chips: a ? a.querySelectorAll('[data-acct]').length : 0 };
    });
    if (st.hidden) return;                       // ≤1 account on this host — nothing to filter, fine
    if (st.chips < 2) throw new Error('account strip shown but has <2 chips (need All + ≥1 account)');
    // click the first real account chip (filter), then "All" (clear) — must not throw
    await page.evaluate(() => { const r = [...document.querySelectorAll('#accounts [data-acct]')].find(e => e.getAttribute('data-acct')); if (r) r.click(); });
    await page.waitForTimeout(150);
    await page.evaluate(() => { const all = document.querySelector('#accounts [data-acct=""]'); if (all) all.click(); });
    await page.waitForFunction(() => { const all = document.querySelector('#accounts [data-acct=""]'); return all && all.classList.contains('on'); }, undefined, { timeout: 3000 });
  });

  await check('hamburger opens the menu, ✕ closes it', async () => {
    // a prior test may have opened the full-screen drill-in overlay (which intentionally covers the
    // header); close it first so the hamburger is reachable.
    await page.evaluate(() => window.closeDetail && window.closeDetail());
    await page.waitForFunction(() => { const d = document.getElementById('detail'); return d && d.hidden === true; }, undefined, { timeout: 3000 });
    await page.click('#menubtn');
    await page.waitForFunction(() => { const m = document.getElementById('menu'); return m && m.hidden === false; }, undefined, { timeout: 3000 });
    const sortRows = await page.evaluate(() => document.querySelectorAll('#menusort [data-sort]').length);
    if (sortRows < 4) throw new Error('menu sort rows missing (got ' + sortRows + ', want ≥4)');
    await page.click('#menuclose');
    await page.waitForFunction(() => { const m = document.getElementById('menu'); return m && m.hidden === true; }, undefined, { timeout: 3000 });
  });

  // Screenshot artifact (gitignored) — eyes-on confirmation of real layout/CSS, fixed name so it overwrites.
  let shot = '';
  await check('saves a full-page screenshot artifact', async () => {
    const dir = path.join(__dirname, 'artifacts');
    fs.mkdirSync(dir, { recursive: true });
    shot = path.join(dir, 'dashboard.png');
    await page.screenshot({ path: shot, fullPage: true });
    if (!fs.existsSync(shot) || fs.statSync(shot).size < 1000) throw new Error('screenshot not written');
  });

  await check('no uncaught page exceptions', async () => {
    if (pageErrors.length) throw new Error(pageErrors.join('  |  '));
  });
  await check('no unexpected console errors', async () => {
    if (consoleErrors.length) throw new Error(consoleErrors.join('  |  '));
  });

  await browser.close();

  let failed = 0;
  for (const [ok, n] of results) { console.log((ok ? '  PASS ' : '  FAIL ') + n); if (!ok) failed++; }
  if (shot) console.log('  artifact: tests/artifacts/dashboard.png');
  console.log(`playwright_e2e: ${results.length - failed}/${results.length} passed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('  FAIL harness error: ' + (e && e.stack ? e.stack.split('\n')[0] : e)); process.exit(1); });
