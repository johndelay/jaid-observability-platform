// UI smoke test — runs web/index.html's <script> in a stubbed DOM (node stdlib `vm`, no deps)
// and exercises the render paths against realistic data. Catches the class of bug that hung the
// drill-in on "loading…": an undefined helper (e.g. esc) → ReferenceError swallowed at runtime.
// Exit non-zero on any failure so verify.sh / CI fails loudly.
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const idx = process.argv[2] ? path.resolve(process.argv[2]) : path.join(__dirname, '..', 'web', 'index.html');
const html = fs.readFileSync(idx, 'utf8');
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.error('FAIL: no <script> block in index.html'); process.exit(1); }
const code = m[1];

// ---- minimal DOM/browser stubs ----
function elem() {
  return {
    innerHTML: '', textContent: '', hidden: false, value: '', placeholder: '', disabled: false,
    style: {}, dataset: {}, scrollHeight: 0, scrollTop: 0, clientHeight: 0,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {}, setAttribute() {}, getAttribute() { return null; },
    appendChild() {}, closest() { return null; }, play() { return Promise.resolve(); },
    focus() {}, set onclick(_) {}, get onclick() { return null; },
  };
}
const els = {};
const document = {
  getElementById(id) { return els[id] || (els[id] = elem()); },
  addEventListener() {}, querySelector() { return elem(); }, createElement() { return elem(); },
  get visibilityState() { return 'visible'; }, body: elem(),
};
function AudioCtx() {
  return { createOscillator: () => ({ connect() {}, start() {}, stop() {}, type: '', frequency: {} }),
           createGain: () => ({ connect() {}, gain: {} }), destination: {}, resume() {}, state: 'running', currentTime: 0 };
}
class EventSource { constructor() { this.onopen = this.onmessage = this.onerror = null; } close() {} }
const navigator = {}; // no serviceWorker / wakeLock → exercises the graceful fallbacks
const win = { AudioContext: AudioCtx };
const sandbox = {
  document, navigator, window: win, EventSource, AudioContext: AudioCtx,
  fetch: async (url) => ({ ok: true, json: async () => String(url).includes('/cost')
    ? { enabled: true, burn: { cost_per_hour: 1.23, tokens_per_min: 999 },
        daily: [{ day: '2026-06-04', cost_usd: 4.56 }], projects: [], total_usd: 4.56 }
    : String(url).includes('/hosts')
    ? { hosts: [{ host: 'my-desktop', status: 'green', age_secs: 3, sessions: 2, source: 'local' },
                { host: 'my-laptop', status: 'red', age_secs: 240, sessions: 0, source: 'beacon' }],
        rejected: [{ ip: '192.0.2.42', count: 7, age_secs: 5 }] }
    : { items: [] } }),
  setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
  console, location: { reload() {}, origin: 'http://localhost:8099' }, Date,
};
sandbox.globalThis = sandbox;
const ctx = vm.createContext(sandbox);

const results = [];
function check(name, fn) {
  try { fn(); results.push([true, name]); }
  catch (e) { results.push([false, name + '  →  ' + (e && e.stack ? e.stack.split('\n')[0] : e)]); }
}

// 1) the page script loads (top-level connect()/lock()/SW-register must not throw with stubs)
check('index.html <script> loads without throwing', () => {
  vm.runInContext(code, ctx, { filename: 'web/index.html' });
});

const STATE = [
  { session_id: 'abc12345-aaaa', host: 'my-desktop', project: 'my-project', model: 'Opus 4.8', pct: 73,
    window: 1000000, authoritative: true, tokens_until_compact: 200000, compact_at: 950000, cost_usd: 1.23,
    state: 'waiting', eff_state: 'waiting', needs_me: true, awaiting_input_since: '2026-06-04T20:00:00Z',
    answerable: true, reply_enabled: true, eta_secs: 1320, growth_tpm: 3949,
    last_msg: { kind: 'ask', text: 'Go live now?', options: ['Activate <now>', 'Leave dormant'] },
    subagent_running: 1, sidechain_recent: 1, age_secs: 5, stale: false },
  { session_id: 'def67890-bbbb', host: 'my-laptop', project: 'work-project', model: 'Sonnet 4.6', pct: 40,
    window: 200000, authoritative: false, tokens_until_compact: 80000, compact_at: 190000, cost_usd: null,
    state: 'working', eff_state: 'working', needs_me: false, answerable: false, reply_enabled: true,
    subagent_running: 0, sidechain_recent: 0, age_secs: 3, stale: false },
];
const ACTIVITY = { items: [
  { kind: 'text', text: 'hello <b>world</b> & co', ts: 't', side: false },
  { kind: 'thinking', text: 'planning the <approach>', ts: 't', side: false },
  { kind: 'tool', name: 'Bash', summary: 'echo hi > /tmp/x', ts: 't', side: true },
  { kind: 'ask', question: 'Ship it <now>?', options: ['Yes & deploy', 'No <wait>'], ts: 't', side: false },
  { kind: 'result', text: 'ok & done', error: false, ts: 't', side: false },
] };

// 2) render() the fleet (hero + rows) — must not throw and must produce output
check('render(state) produces output (hero + rows)', () => {
  if (typeof ctx.render !== 'function') throw new Error('render() not defined');
  ctx.render(STATE);
  const out = document.getElementById('app').innerHTML;
  if (!out || out.length < 50) throw new Error('app empty after render');
  if (!out.includes('Needs you')) throw new Error('hero "Needs you" missing for a needs_me session');
});

// 3) render() with empty list (calm state)
check('render([]) handles empty fleet', () => { ctx.render([]); });

// 3b) hero card shows the "what's being asked" preview (so you see it without drilling in)
check('hero card shows last_msg ask preview', () => {
  ctx.render(STATE);
  const out = document.getElementById('app').innerHTML;
  if (!out.includes('Go live now?')) throw new Error('ask preview missing from hero card');
});

// 3c) time-to-compact ETA renders on a card (⏱ marker via dur())
check('hero card shows time-to-compact ETA', () => {
  ctx.render(STATE);
  if (!document.getElementById('app').innerHTML.includes('⏱')) throw new Error('ETA marker missing');
});

// 3d) burn + today cost chips render from window._cost
check('summary shows burn + today chips from /cost', () => {
  win._cost = { burn: { cost_per_hour: 1.23 }, daily: [{ day: '2026-06-04', cost_usd: 4.56 }] };
  ctx.render(STATE);
  const out = document.getElementById('app').innerHTML;
  if (!out.includes('/h')) throw new Error('burn chip missing');
  if (!out.includes('today')) throw new Error('today chip missing');
  win._cost = null;
});

// 3d2) rate-limit gauges, single account: worst-WITHIN-account; generic "plan limits" label; crit at >=85
check('rate gauges (single account): worst-within-account + "plan limits" label', () => {
  ctx.setReveal(true);   // these checks assert real account short-names; masking is the DEFAULT now (tested separately)
  const now = Math.floor(Date.now() / 1000);
  const oneAcct = [   // no `account` field -> both fold into one "(unknown)" account group
    { ...STATE[0], rate_limits: { five_hour: { used_percentage: 30, resets_at: now + 7200 },
                                  seven_day: { used_percentage: 12, resets_at: now + 3 * 86400 } } },
    { ...STATE[1], rate_limits: { five_hour: { used_percentage: 88.0, resets_at: now + 3600 },
                                  seven_day: { used_percentage: 4, resets_at: now + 3 * 86400 } } },
  ];
  ctx.render(oneAcct); ctx.renderCost(oneAcct);
  const out = document.getElementById('scene-cost').innerHTML;
  if (!out.includes('ratelimits')) throw new Error('per-account ratelimits section missing');
  if (!out.includes('plan limits')) throw new Error('single account should use the generic "plan limits" label');
  if (!out.includes('88%')) throw new Error('5h should show the worst-within-account (88%), not 30%');
  if (out.includes('30%')) throw new Error('5h wrongly showing the lower (30%) reading');
  if (!out.includes('12%')) throw new Error('7d worst (12%) missing');
  if (!out.includes('↺')) throw new Error('reset countdown glyph missing');
  if (!/chip crit"><b>88%/.test(out)) throw new Error('5h gauge at 88% missing crit class');
});

// 3d3) MULTI-account: one labeled row per account, each its OWN value — never blended across accounts
check('rate gauges split per account (no cross-account blending)', () => {
  const now = Math.floor(Date.now() / 1000);
  const twoAccts = [
    { ...STATE[0], account: 'me@gmail.com',  rate_limits: { five_hour: { used_percentage: 8,  resets_at: now + 7200 },
                                                            seven_day: { used_percentage: 2,  resets_at: now + 3 * 86400 } } },
    { ...STATE[1], account: 'work@corp.com', rate_limits: { five_hour: { used_percentage: 88, resets_at: now + 3600 },
                                                            seven_day: { used_percentage: 12, resets_at: now + 3 * 86400 } } },
  ];
  ctx.render(twoAccts); ctx.renderCost(twoAccts);
  const out = document.getElementById('scene-cost').innerHTML;   // Cost scene = per-account rate rows
  const triage = document.getElementById('app').innerHTML;        // Triage = session cards
  if (!out.includes('>me</span>') || !out.includes('>work</span>')) throw new Error('per-account short labels (me / work) missing');
  if (out.includes('plan limits')) throw new Error('multi-account must label by account, not the generic label');
  if (!out.includes('8%') || !out.includes('88%')) throw new Error('each account must show its OWN 5h value (8% and 88%), not a blended max');
  if (!/chip crit"><b>88%/.test(out)) throw new Error('the work-account 88% gauge missing crit class');
  // multi-account also tags the session cards (sub-line) with the short account name — stays on Triage
  if (!triage.includes(' · me') && !triage.includes(' · work')) throw new Error('session cards not tagged with account in multi-account mode');
});

// 3d4) no rate gauges when no session reports rate_limits AND no cost-by-account
check('per-account section hidden when no rate_limits and no cost', () => {
  win._cost = null;
  ctx.render(STATE); ctx.renderCost(STATE);   // STATE sessions have no rate_limits and no account
  const out = document.getElementById('scene-cost').innerHTML;
  if (out.includes('ratelimits') || out.includes('plan limits')) throw new Error('per-account section rendered with no data');
});

// 3d5) per-account COST: today's spend chip from /cost.by_account, merged with the rate rows;
//      a cost-only account (spend but no live session/rate) still gets a row.
check('per-account cost: today spend chips merge with rate rows', () => {
  const now = Math.floor(Date.now() / 1000);
  win._cost = { by_account: [
    { account: 'me@gmail.com',   cost_usd: 4.20, messages: 9 },
    { account: 'work@corp.com',  cost_usd: 1.50, messages: 3 },
    { account: 'costonly@x.com', cost_usd: 7.00, messages: 2 },   // spend but no live session below
  ] };
  const two = [
    { ...STATE[0], account: 'me@gmail.com',  rate_limits: { five_hour: { used_percentage: 8,  resets_at: now + 7200 },
                                                            seven_day: { used_percentage: 2,  resets_at: now + 3 * 86400 } } },
    { ...STATE[1], account: 'work@corp.com', rate_limits: { five_hour: { used_percentage: 50, resets_at: now + 3600 },
                                                            seven_day: { used_percentage: 9,  resets_at: now + 3 * 86400 } } },
  ];
  ctx.render(two); ctx.renderCost(two);
  const out = document.getElementById('scene-cost').innerHTML;
  if (!out.includes('$4.20')) throw new Error("me account's today-spend chip missing");
  if (!out.includes('$1.50')) throw new Error("work account's today-spend chip missing");
  if (!out.includes('$7.00')) throw new Error('cost-only account (no live session) should still get a row with its spend');
  if (!out.includes('>me</span>') || !out.includes('>work</span>')) throw new Error('per-account labels missing');
  win._cost = null;
});

// 3d6) approximate (host-login) accounts are marked with a ~; exact (per-session hook) ones are not
check('approximate vs exact account marking (~)', () => {
  const now = Math.floor(Date.now() / 1000);
  win._cost = null;
  const sess = [
    { ...STATE[0], account: 'exact@x.com', account_approx: false,
      rate_limits: { five_hour: { used_percentage: 5, resets_at: now + 3600 }, seven_day: { used_percentage: 1, resets_at: now + 3 * 86400 } } },
    { ...STATE[1], account: 'guess@y.com', account_approx: true,
      rate_limits: { five_hour: { used_percentage: 9, resets_at: now + 3600 }, seven_day: { used_percentage: 2, resets_at: now + 3 * 86400 } } },
  ];
  ctx.render(sess); ctx.renderCost(sess);
  const out = document.getElementById('scene-cost').innerHTML;
  if (!out.includes('~guess')) throw new Error('approximate (host-login) account should be ~-prefixed');
  if (out.includes('~exact')) throw new Error('exact (per-session hook) account must NOT be ~-prefixed');
});

// 3d7) Extra Usage proximity = binding window %, with enabled (→ extra usage) vs disabled (→ plan limit) semantics
check('Extra Usage proximity metric (binding %, enabled/blocked semantics)', () => {
  const now = Math.floor(Date.now() / 1000);
  win._cost = null;
  const sess = [
    // Extra Usage ENABLED, 5h binds at 72% → "→ extra usage" (warn)
    { ...STATE[0], account: 'max@me.com', account_approx: false, extra_usage: true,
      rate_limits: { five_hour: { used_percentage: 72, resets_at: now + 3600 }, seven_day: { used_percentage: 20, resets_at: now + 3 * 86400 } } },
    // Extra Usage DISABLED, 7d binds at 90% → "→ plan limit" (crit)
    { ...STATE[1], account: 'pro@me.com', account_approx: false, extra_usage: false,
      rate_limits: { five_hour: { used_percentage: 40, resets_at: now + 3600 }, seven_day: { used_percentage: 90, resets_at: now + 3 * 86400 } } },
  ];
  ctx.render(sess); ctx.renderCost(sess);
  const out = document.getElementById('scene-cost').innerHTML;
  if (!out.includes('→ extra usage')) throw new Error('Extra-Usage-ON account should read "→ extra usage"');
  if (!out.includes('>72%')) throw new Error('binding window % (72, the 5h) missing for the enabled account');
  if (!out.includes('→ plan limit')) throw new Error('Extra-Usage-OFF account should read "→ plan limit"');
  if (!/chip crit"><b>90%/.test(out)) throw new Error('binding 90% (the 7d) should be crit');
});

// 3d7b) Extra Usage chip shows the burn ETA (⏱) for the binding window when /cost provides rate_eta
check('Extra Usage chip shows burn ETA when available', () => {
  const now = Math.floor(Date.now() / 1000);
  win._cost = { rate_eta: { 'max@me.com': { five_hour: { eta_secs: 8640, slope_pct_per_hr: 12.5 }, seven_day: null } } };
  const s1 = [{ ...STATE[0], account: 'max@me.com', account_approx: false, extra_usage: true,
    rate_limits: { five_hour: { used_percentage: 60, resets_at: now + 3600 }, seven_day: { used_percentage: 10, resets_at: now + 3 * 86400 } } }];
  ctx.render(s1); ctx.renderCost(s1);
  const out = document.getElementById('scene-cost').innerHTML;
  if (!out.includes('→ extra usage')) throw new Error('Extra Usage chip missing');
  if (!out.includes('⏱')) throw new Error('burn ETA (⏱) missing on the Extra Usage chip (5h binds → five_hour eta)');
  win._cost = null;
});

// 3d7c) overage chip: shows the Extra-Usage overage $ when /cost reports it for the account
check('overage chip shows when /cost reports Extra-Usage overage', () => {
  const now = Math.floor(Date.now() / 1000);
  win._cost = { overage: { 'max@me.com': 3.10 } };
  const s2 = [{ ...STATE[0], account: 'max@me.com', account_approx: false, extra_usage: true,
    rate_limits: { five_hour: { used_percentage: 100, resets_at: now + 600 }, seven_day: { used_percentage: 30, resets_at: now + 3 * 86400 } } }];
  ctx.render(s2); ctx.renderCost(s2);
  const out = document.getElementById('scene-cost').innerHTML;
  if (!out.includes('overage today')) throw new Error('overage chip missing');
  if (!out.includes('$3.10')) throw new Error('overage amount ($3.10) missing');
  win._cost = null;
});

// 3d8) Extra Usage metric ABSENT when the account has no rate_limits (Enterprise/Console/Team)
check('Extra Usage metric hidden for accounts without rate_limits', () => {
  win._cost = { by_account: [{ account: 'ent@corp.com', cost_usd: 5.0, messages: 4 }] };
  const s3 = [{ ...STATE[0], account: 'ent@corp.com', account_approx: false }];   // no rate_limits
  ctx.render(s3); ctx.renderCost(s3);
  const out = document.getElementById('scene-cost').innerHTML;
  if (out.includes('→ extra usage') || out.includes('→ plan')) throw new Error('no rate_limits → Extra Usage metric must not render');
  if (!out.includes('$5.00')) throw new Error('an Enterprise account with no rate_limits should still show its cost');
  win._cost = null;
});

// 3d9) screenshot-safe masking: identifiers are masked to neutral codenames BY DEFAULT; reveal un-masks
check('masking: emails/hosts/projects masked by default, reveal un-masks', () => {
  ctx.setReveal(false);
  const now = Math.floor(Date.now() / 1000);
  win._cost = null;
  const sess = [{ ...STATE[0], account: 'jane.doe@acme.com', host: 'jd-secret-01', project: 'TopSecret',
    rate_limits: { five_hour: { used_percentage: 10, resets_at: now + 3600 }, seven_day: { used_percentage: 2, resets_at: now + 3 * 86400 } } }];
  ctx.render(sess); ctx.renderCost(sess);
  const triage = document.getElementById('app').innerHTML, cost = document.getElementById('scene-cost').innerHTML;
  if (triage.includes('jane') || cost.includes('jane') || cost.includes('@')) throw new Error('account/email leaked while masked');
  if (triage.includes('jd-secret-01')) throw new Error('hostname leaked while masked');
  if (triage.includes('TopSecret')) throw new Error('project name leaked while masked');
  if (!cost.includes('Account ')) throw new Error('masked account codename ("Account XX") missing');
  if (!triage.includes('Host ') || !triage.includes('Project ')) throw new Error('masked host/project codenames missing');
  ctx.setReveal(true);
  ctx.render(sess); ctx.renderCost(sess);
  if (!document.getElementById('scene-cost').innerHTML.includes('jane')) throw new Error('reveal did not un-mask the real account');
  if (!document.getElementById('app').innerHTML.includes('jd-secret-01')) throw new Error('reveal did not un-mask the real host');
  ctx.setReveal(false);   // restore screenshot-safe default for subsequent checks
});

// 3e) fleet reporting-health strip (P4): chips per host + a red auth-rejection chip; status dot classed
check('renderHosts renders host chips + rejection warning', () => {
  if (typeof ctx.renderHosts !== 'function') throw new Error('renderHosts() not defined');
  ctx.setReveal(true);   // structure test asserts the real host/IP; masking is verified separately (3d9)
  ctx.renderHosts({
    hosts: [{ host: 'my-desktop', status: 'green', age_secs: 3, sessions: 2, source: 'local' },
            { host: 'my-laptop', status: 'red', age_secs: 240, sessions: 0, source: 'beacon' }],
    rejected: [{ ip: '192.0.2.42', count: 7, age_secs: 5 }],
  });
  const out = document.getElementById('fleet').innerHTML;
  // alert-only strip: the RED host (my-laptop) shows; the GREEN host (my-desktop) does NOT clutter it
  if (!out.includes('my-laptop')) throw new Error('problem (red) host chip missing');
  if (out.includes('my-desktop')) throw new Error('green host should not appear in the alert-only strip');
  if (!out.includes('class="red"') && !out.includes("class='red'")) throw new Error('red status dot missing for a not-reporting host');
  if (!out.includes('7 rejected')) throw new Error('auth-rejection warning missing');
  if (!out.includes('192.0.2.42')) throw new Error('rejected source IP missing');
  ctx.setReveal(false);
});

// 3e2) account top-bar chips: render when >1 account, with an "All" chip + per-account chips
check('account chips render (All + per-account) when >1 account', () => {
  ctx.setReveal(true);
  win._cost = { by_account: [{ account: 'a@x.com', cost_usd: 1 }, { account: 'b@y.com', cost_usd: 2 }] };
  ctx.render([{ ...STATE[0], account: 'a@x.com' }, { ...STATE[1], account: 'b@y.com' }]);
  const out = document.getElementById('accounts').innerHTML;
  if (document.getElementById('accounts').hidden) throw new Error('account strip hidden with 2 accounts');
  if (!out.includes('data-acct=""')) throw new Error('"All" chip missing');
  if (!out.includes('data-acct="a@x.com"') || !out.includes('data-acct="b@y.com"')) throw new Error('per-account chips missing');
  win._cost = null; ctx.setReveal(false);
});

// 3e3) account strip hidden when ≤1 account (nothing to filter)
check('account chips hidden when ≤1 account', () => {
  win._cost = null;
  ctx.render([{ ...STATE[0], account: 'solo@x.com' }]);
  if (!document.getElementById('accounts').hidden) throw new Error('account strip should hide with a single account');
});

// 3f) renderHosts with nothing known -> strip hidden, no throw
check('renderHosts hides the strip when empty', () => {
  ctx.renderHosts({ hosts: [], rejected: [] });
  if (!document.getElementById('fleet').hidden) throw new Error('fleet strip should be hidden when empty');
  ctx.renderHosts({});      // tolerate a malformed/empty payload
});

// 4) renderStream() — the path that was broken (undefined esc). Must not throw + must escape HTML.
check('renderStream(activity) renders + escapes HTML', () => {
  if (typeof ctx.renderStream !== 'function') throw new Error('renderStream() not defined');
  ctx.renderStream(ACTIVITY);
  const out = document.getElementById('dstream').innerHTML;
  if (!out || out.length < 20) throw new Error('dstream empty after renderStream');
  if (out.includes('<b>world</b>')) throw new Error('HTML not escaped (XSS / render bug)');
  if (!out.includes('&lt;b&gt;')) throw new Error('escaping did not run');
});

// 4b) renderStream renders an AskUserQuestion as a readable question + options (escaped)
check('renderStream renders ask question + options (escaped)', () => {
  ctx.renderStream(ACTIVITY);
  const out = document.getElementById('dstream').innerHTML;
  if (!out.includes('Ship it')) throw new Error('ask question not rendered');
  if (!out.includes('Yes &amp; deploy')) throw new Error('ask option not rendered/escaped');
  if (out.includes('<now>')) throw new Error('ask text not HTML-escaped');
  if (!out.includes('&lt;now&gt;')) throw new Error('ask escaping did not run');
});

// 5) renderStream note + empty
check('renderStream({note}) and empty items', () => {
  ctx.renderStream({ note: 'waiting for activity from host' });
  ctx.renderStream({ items: [] });
});

// 6) openDetail() wires up without throwing
check('openDetail(sid) does not throw', () => {
  if (typeof ctx.openDetail !== 'function') throw new Error('openDetail() not defined');
  ctx.openDetail('abc12345-aaaa');
});

// 7) answer-from-phone reply bar: shown for an answerable+PIN session, hidden otherwise
check('reply bar visibility tracks answerable + reply_enabled', () => {
  ctx.render(STATE);                       // populate window._cards
  ctx.openDetail('abc12345-aaaa');         // answerable:true, reply_enabled:true
  if (document.getElementById('dreply').hidden) throw new Error('reply bar hidden for an answerable session');
  ctx.openDetail('def67890-bbbb');         // answerable:false
  if (!document.getElementById('dreply').hidden) throw new Error('reply bar shown for a non-answerable session');
});

// 8) sendReply() does not throw (empty input → early return; stub fetch path)
check('sendReply() handles empty + stub fetch without throwing', () => {
  if (typeof ctx.sendReply !== 'function') throw new Error('sendReply() not defined');
  ctx.openDetail('abc12345-aaaa');
  document.getElementById('rtext').value = '';
  return ctx.sendReply();                  // empty → returns before fetch; must not throw
});

// 9) scene carousel (Phase 1): goScene() switches scenes + updates the nav label (registry path).
//    Also proves the new module loaded without throwing in a sandbox that has no localStorage/window events.
check('goScene switches scenes + updates nav label', () => {
  if (typeof ctx.goScene !== 'function') throw new Error('goScene() not defined (scene module failed to load)');
  ctx.goScene(1);   // → Cost (scene 2 of 4)
  const lbl = document.getElementById('navlabel').textContent;
  if (!/2\/4|Cost/.test(lbl)) throw new Error('nav label did not reflect scene 2: ' + JSON.stringify(lbl));
  ctx.goScene(0);
  const lbl0 = document.getElementById('navlabel').textContent;
  if (!/1\/4|Triage/.test(lbl0)) throw new Error('nav label did not return to Triage: ' + JSON.stringify(lbl0));
});

// 9b) Fleet scene (Phase 2): per-host cards + a rejection card
check('renderFleetScene renders per-host cards + rejection', () => {
  if (typeof ctx.renderFleetScene !== 'function') throw new Error('renderFleetScene() not defined');
  ctx.setReveal(true);   // structure test asserts the real host; masking is verified separately (3d9)
  ctx.renderFleetScene({ hosts: [{ host: 'my-desktop', status: 'green', age_secs: 3, sessions: 2, source: 'local' }],
                         rejected: [{ ip: '192.0.2.42', count: 7, age_secs: 5 }] });
  const out = document.getElementById('scene-fleet').innerHTML;
  if (!out.includes('my-desktop')) throw new Error('host card missing');
  if (!out.includes('hdot green')) throw new Error('host status dot missing');
  if (!out.includes('Rejected')) throw new Error('rejection card missing');
  ctx.setReveal(false);
});

// 9c) Fleet scene empty state must not throw
check('renderFleetScene handles no hosts', () => { ctx.renderFleetScene({ hosts: [], rejected: [] }); });

// 9d) session sort: default mode = working-on-top, then Context % (fullest first)
check('sortSessions: working-on-top then by context %', () => {
  if (typeof ctx.sortSessions !== 'function') throw new Error('sortSessions() not defined');
  const arr = [
    { session_id: 'a', eff_state: 'idle',    pct: 90, age_secs: 10 },
    { session_id: 'b', eff_state: 'working', pct: 20, age_secs: 5 },
    { session_id: 'c', eff_state: 'idle',    pct: 50, age_secs: 8 },
  ];
  const out = ctx.sortSessions(arr).map(s => s.session_id).join('');
  if (out !== 'bac') throw new Error('expected working(b) first, then ctx% desc (a90,c50) → "bac", got "' + out + '"');
});

// 9e) drill-in resume hint: shows `claude --resume <id>` + a copy affordance
check('drill-in shows a resume command + copy affordance', () => {
  ctx.render(STATE); ctx.openDetail('abc12345-aaaa');
  const out = document.getElementById('dstats').innerHTML;
  if (!out.includes('claude --resume abc12345-aaaa')) throw new Error('resume command missing');
  if (!out.includes('data-copy')) throw new Error('copy affordance missing');
});

// 9f) feature toggle: show-idle off hides idle sessions (and notes the hidden count); on shows them
check('feature toggle: show-idle hides/shows idle sessions', () => {
  if (typeof ctx.setFeat !== 'function') throw new Error('setFeat() not defined');
  const list = [{ ...STATE[1], session_id: 'w', eff_state: 'working', needs_me: false },
                { ...STATE[1], session_id: 'i', eff_state: 'idle',    needs_me: false }];
  ctx.setFeat('showIdle', false); ctx.render(list);
  let out = document.getElementById('app').innerHTML;
  if (!out.includes('data-sid="w"')) throw new Error('working session should show with show-idle off');
  if (out.includes('data-sid="i"')) throw new Error('idle session should be hidden with show-idle off');
  if (!out.includes('idle hidden')) throw new Error('hidden-count note missing');
  ctx.setFeat('showIdle', true); ctx.render(list);
  if (!document.getElementById('app').innerHTML.includes('data-sid="i"')) throw new Error('idle session should show with show-idle on');
});

// 9g) archive: hidden sessions are filtered out of Triage and listed in the Archive scene
check('archive: hidden filtered from Triage, shown in Archive', () => {
  ctx.setFeat('showIdle', true);
  const list = [{ ...STATE[1], session_id: 'vis', needs_me: false, hidden: false },
                { ...STATE[1], session_id: 'hid', needs_me: false, hidden: true }];
  ctx.render(list);
  const triage = document.getElementById('app').innerHTML;
  if (!triage.includes('data-sid="vis"')) throw new Error('visible session missing from Triage');
  if (triage.includes('data-sid="hid"')) throw new Error('hidden session must not appear in Triage');
  const arch = document.getElementById('scene-archive').innerHTML;
  if (!arch.includes('data-unhide="hid"')) throw new Error('hidden session missing from Archive scene');
  if (arch.includes('data-unhide="vis"')) throw new Error('visible session must not appear in Archive');
});

// 9h) setHidden optimistically moves a session Triage → Archive without a server round-trip
check('setHidden optimistically moves session Triage → Archive', () => {
  if (typeof ctx.setHidden !== 'function') throw new Error('setHidden() not defined');
  const list = [{ ...STATE[1], session_id: 'x', needs_me: false, hidden: false }];
  ctx.render(list);
  ctx.setHidden('x', true);
  if (document.getElementById('app').innerHTML.includes('data-sid="x"')) throw new Error('session should leave Triage after setHidden(true)');
  if (!document.getElementById('scene-archive').innerHTML.includes('data-unhide="x"')) throw new Error('session should appear in Archive after setHidden(true)');
});

// 9i) dumb-zone v1: zone() maps ABSOLUTE context_tokens (+ a proximity-to-compact override) to zones
check('zone() maps absolute context_tokens to the right zone', () => {
  if (typeof ctx.zone !== 'function') throw new Error('zone() not defined');
  const z = (tok, ptc) => ctx.zone({ context_tokens: tok, pct_to_compact: ptc }).key;
  if (z(10000, 5)   !== 'sharp')  throw new Error('10k → sharp');
  if (z(60000, 20)  !== 'good')   throw new Error('60k → good');
  if (z(150000, 40) !== 'drift')  throw new Error('150k → drift');
  if (z(250000, 30) !== 'danger') throw new Error('250k → danger (absolute, even at low % of a 1M window)');
  if (z(40000, 90)  !== 'danger') throw new Error('ptc 90 → danger (wall override on a small window)');
  if (z(40000, 100) !== 'auto')   throw new Error('ptc 100 → auto-compact');
});

// 9j) drill-in shows the dumb-zone banner + advice; summary chip counts drifting+ sessions
check('dumb-zone banner in drill-in + summary chip', () => {
  ctx.setFeat('dumbzone', true);
  ctx.render([{ ...STATE[1], session_id: 'dz', context_tokens: 150000, pct_to_compact: 60, needs_me: false },
              { ...STATE[1], session_id: 'sh', context_tokens: 10000,  pct_to_compact: 5,  needs_me: false }]);
  const app = document.getElementById('app').innerHTML;
  if (!app.includes('dumb zone')) throw new Error('summary "dumb zone" chip missing when a session is drifting');
  ctx.openDetail('dz');
  const d = document.getElementById('dstats').innerHTML;
  if (!d.includes('zone z-drift')) throw new Error('drill-in zone banner (drift) missing');
  if (!d.includes('Drifting')) throw new Error('zone label/advice missing in drill-in');
});

// 9j2) conflation fix: a low-% / high-abs-token tile DECOUPLES the two signals — the % keeps its
// COMPACTION color (NOT red at 23%) and the SOFT inline 🧠 dumb-zone pip appears. Guards the reported bug
// (red "23%" that looked like "about to compact" on a 1M window).
check('low-% high-abs-token: % not compaction-red + soft 🧠 pip', () => {
  ctx.setFeat('dumbzone', true);
  const cl = ctx.clarity({ context_tokens: 250000 });
  if (cl.fill !== 1) throw new Error('250k abs tokens should fill the clarity meter');
  if (/red/i.test(cl.col)) throw new Error('clarity hue must never be alarm-red');
  ctx.render([{ ...STATE[1], session_id: 'deep', eff_state: 'working', needs_me: false, pct: 23, context_tokens: 250000, pct_to_compact: 23 }]);
  const app = document.getElementById('app').innerHTML;
  if (!app.includes('dz-pip')) throw new Error('inline 🧠 dumb-zone pip missing on the tile');
  if (app.includes('class="pct" style="color:var(--red)"')) throw new Error('tile % rendered compaction-red at 23% (the conflation bug)');
});

// 9k) dumbzone toggle OFF → no zone chip/banner (falls back to the plain number)
check('dumb-zone toggle off hides the zone UI', () => {
  ctx.setFeat('dumbzone', false);
  ctx.render([{ ...STATE[1], session_id: 'dz', context_tokens: 150000, pct_to_compact: 60, needs_me: false }]);
  if (document.getElementById('app').innerHTML.includes('dumb zone')) throw new Error('dumb-zone chip shown while toggled off');
  ctx.openDetail('dz');
  if (document.getElementById('dstats').innerHTML.includes('class="zone')) throw new Error('zone banner shown while toggled off');
  ctx.setFeat('dumbzone', true);   // restore default
});

// 9l) History scene (Phase 4): all-time totals + daily bar chart (SVG) + top projects, from /cost
check('renderHistory: all-time totals + daily bars + top projects', () => {
  if (typeof ctx.renderHistory !== 'function') throw new Error('renderHistory() not defined');
  win._cost = {
    total_usd: 1200.00,
    daily: [{ day: '2026-06-06', cost_usd: 42.79, messages: 236 }, { day: '2026-06-05', cost_usd: 148.43, messages: 906 },
            { day: '2026-06-04', cost_usd: 176.9, messages: 1002 }],
    projects: [{ project: 'my-project', cost_usd: 491.9, messages: 3300 }, { project: 'project-b', cost_usd: 168.6, messages: 832 }],
    burn: { cost_per_hour: 11.81 },
  };
  ctx.renderHistory();
  const out = document.getElementById('scene-history').innerHTML;
  if (!out.includes('all-time spend')) throw new Error('all-time spend chip missing');
  if (!out.includes('$1200')) throw new Error('all-time total missing');
  if (!out.includes('<svg')) throw new Error('daily bar chart (SVG) missing');
  if (!out.includes('Top projects')) throw new Error('top projects section missing');
  win._cost = null;
});

// 9l2) Trophy Room scene (V10 Slice 1): all-time totals + streak + cache savings + spend breakdown from /report
check('renderTrophy: all-time totals + streak + cache savings + breakdown', () => {
  if (typeof ctx.renderTrophy !== 'function') throw new Error('renderTrophy() not defined');
  win._report = {
    enabled: true,
    all_time: { cost_usd: 1200.00, messages: 12000, sessions: 300, tokens: 1500000000 },
    streak: { current: 6, longest: 23, active_days: 41 },
    cache_savings_usd: 942.17,
    breakdown: {
      input: { tokens: 120000, cost_usd: 0.6 }, output: { tokens: 80000, cost_usd: 2.0 },
      cache_read: { tokens: 1200000000, cost_usd: 600.0 }, cache_creation: { tokens: 50000000, cost_usd: 312.5 },
    },
    daily: [{ day: '2026-06-06', cost_usd: 42.79, messages: 236, tokens: 9000000 },
            { day: '2026-06-01', cost_usd: 12.0, messages: 80, tokens: 3000000 }],
    hourly: [{ dow: 1, hour: 9, messages: 40, cost_usd: 3.1 }, { dow: 4, hour: 22, messages: 90, cost_usd: 7.7 }],
    zone_trend: [{ day: '2026-06-04', healthy_pct: 55.0 }, { day: '2026-06-05', healthy_pct: 60.0 }, { day: '2026-06-06', healthy_pct: 72.0 }],
    model_mix_daily: [{ day: '2026-06-05', opus: 10, sonnet: 5 }, { day: '2026-06-06', opus: 8, sonnet: 4, haiku: 2 }],
  };
  ctx.renderTrophy();
  const out = document.getElementById('scene-trophy').innerHTML;
  if (!out.includes('Trophy Room')) throw new Error('trophy title missing');
  if (!out.includes('all-time spend')) throw new Error('all-time spend chip missing');
  if (!out.includes('$1200')) throw new Error('all-time total missing');
  if (!out.includes('1.5B')) throw new Error('token vanity number (fmtTok) missing/incorrect');
  if (!out.includes('Streak')) throw new Error('streak card missing');
  if (!out.includes('6 days')) throw new Error('current streak missing');
  if (!out.includes('$942')) throw new Error('cache savings missing');
  if (!out.includes('Where the spend goes')) throw new Error('breakdown card missing');
  if (!out.includes('Activity · last year')) throw new Error('calendar heatmap card missing');
  if (!out.includes('<svg')) throw new Error('heatmap SVG missing');
  if (!out.includes('data-metric="tokens"')) throw new Error('metric toggle buttons missing');
  if (!out.includes('When you code')) throw new Error('time-of-day rhythm card missing');
  // the rhythm grid is a FULL 24×7 matrix → 168 cells regardless of how sparse the data is
  const rects = (out.match(/<rect/g) || []).length;
  if (rects < 168) throw new Error('rhythm grid should render a full 24×7=168-cell matrix, got ' + rects + ' rects total');
  if (!out.includes('Context hygiene')) throw new Error('dumb-zone hygiene-trend card missing');
  if (!out.includes('72%')) throw new Error('latest healthy-zone % missing');
  if (!out.includes('▲ 15pt')) throw new Error('hygiene trend delta vs avg wrong (72 vs avg 57.5 → Math.round(14.5)=+15pt)');
  if (!out.includes('Model mix')) throw new Error('model-mix-over-time card missing');
  if (!out.includes('mxleg')) throw new Error('model-mix legend missing');
  win._report = null;
});

// 9l2c) svgStacked (Slice 3): normalized 100% stacked bars; segments sum per column
check('svgStacked renders proportional stacked segments', () => {
  if (typeof ctx.svgStacked !== 'function') throw new Error('svgStacked() not defined');
  const out = ctx.svgStacked([{ day: 'd', opus: 3, sonnet: 1 }], { keys: ['opus', 'sonnet', 'haiku'], colors: { opus: '#f00', sonnet: '#0f0', haiku: '#00f' } });
  if (!out.includes('<svg') || (out.match(/<rect/g) || []).length !== 2) throw new Error('expected 2 segments (opus+sonnet; haiku=0 skipped)');
  if (!out.includes('opus: 75%')) throw new Error('opus share should be 3/4 = 75%');
});

// 9l2d) Efficiency scene (V11 Slice 1): what-if savings, the right-sized note, and the honesty framing
check('renderEfficiency: savings opportunities + honesty framing + empty states', () => {
  if (typeof ctx.renderEfficiency !== 'function') throw new Error('renderEfficiency() not defined');
  win._eff = { enabled: true, estimate: true, savings: { days: 30, considered: 1200, policies: [
    { id: 'all_opus_sonnet', label: 'All Opus → Sonnet', saved_usd: 84.5, turns: 900 },
    { id: 'short_opus_sonnet', label: 'Short Opus turns → Sonnet', saved_usd: 12.0, turns: 120 },
    { id: 'trivial_haiku', label: 'Trivial turns → Haiku', saved_usd: 3.0, turns: 40 },
  ] } };
  ctx.renderEfficiency();
  let out = document.getElementById('scene-efficiency').innerHTML;
  if (!out.includes('What-if downgrade policies')) throw new Error('policy list missing');
  if (!out.includes('$84.5')) throw new Error('top savings figure missing');
  if (!out.includes('opportunities, not judgments')) throw new Error('honesty framing missing (must always show)');
  // all-zero turns → the "already right-sized" honest note, not a fake $0 opportunity
  win._eff = { enabled: true, estimate: true, savings: { days: 30, considered: 5, policies: [
    { id: 'all_opus_sonnet', label: 'All Opus → Sonnet', saved_usd: 0, turns: 0 }] } };
  ctx.renderEfficiency();
  out = document.getElementById('scene-efficiency').innerHTML;
  if (!out.includes('already looks right-sized')) throw new Error('right-sized note missing for zero opportunities');
  win._eff = { enabled: false }; ctx.renderEfficiency();
  if (!document.getElementById('scene-efficiency').innerHTML.includes('No data yet')) throw new Error('empty state missing');
  win._eff = null; ctx.renderEfficiency();   // null payload must be safe
});

// 9l2e) Efficiency ROI card (V11 Slice 2): API-equiv value vs plan price; set-price button when unset
check('renderEfficiency: subscription ROI — multiple when priced, set-price button when not', () => {
  win._eff = { enabled: true, estimate: true,
    savings: { days: 30, considered: 10, policies: [{ id: 'all_opus_sonnet', label: 'All Opus → Sonnet', saved_usd: 5, turns: 3 }] },
    roi: [{ account: 'priced@x.com', value_usd: 220.0, plan_price: 100, multiple: 2.2 },
          { account: 'unpriced@y.com', value_usd: 40.0, plan_price: null, multiple: null }],
    caps: [{ account: 'priced@x.com', windows: { five_hour: { avg: 27.0, peak: 92.0, cap_hits: 0, samples: 40 },
                                                 seven_day: { avg: 55.0, peak: 100.0, cap_hits: 2, samples: 30 } } }] };
  ctx.renderEfficiency();
  const out = document.getElementById('scene-efficiency').innerHTML;
  if (!out.includes('Subscription ROI')) throw new Error('ROI card missing');
  if (!out.includes('2.2×')) throw new Error('ROI multiple missing for a priced account');
  if (!out.includes('data-price="unpriced@y.com"')) throw new Error('set-price button missing for an unpriced account');
  if (!out.includes('Cap headroom')) throw new Error('cap-headroom card missing');
  if (!out.includes('5h cap') || !out.includes('peak 92%')) throw new Error('5h cap row missing');
  if (!out.includes('2 hits')) throw new Error('7d cap_hits count missing/incorrect');
  win._eff = null;
});

// 9l2f) Efficiency reducible-spend card (V11 Slice 4): Danger-band $ + auto-compaction count + "may be reducible" framing
check('renderEfficiency: reducible-spend card leads with Danger-band spend, never says "wasted"', () => {
  win._eff = { enabled: true, estimate: true,
    savings: { days: 30, considered: 10, policies: [] },
    reducible: { days: 30, considered: 100, total_usd: 15.0, at_risk_usd: 8.0, at_risk_pct: 53.3,
                 by_band: { sharp: 1.0, good: 2.0, drift: 4.0, danger: 8.0 }, auto_compactions: 3, estimate: true } };
  ctx.renderEfficiency();
  let out = document.getElementById('scene-efficiency').innerHTML;
  if (!out.includes('may be reducible')) throw new Error('reducible-spend card/heading missing');
  if (!out.includes('$8')) throw new Error('Danger-band at-risk figure missing');
  if (!out.includes('53.3%')) throw new Error('at-risk percent missing');
  if (!out.includes('Danger zone')) throw new Error('Danger-zone framing missing');
  if (!out.includes('auto-compaction')) throw new Error('auto-compaction count missing');
  const txt = out.replace(/<[^>]+>/g, '');   // strip markup so "<b>not</b> wasted" reads as "not wasted"
  if (/\bwasted\b/i.test(txt.replace(/not\s+wasted/ig, ''))) throw new Error('must NOT call spend "wasted" (only "not wasted")');
  // total_usd 0 → card omitted entirely (no division, no empty bars)
  win._eff = { enabled: true, estimate: true, savings: { days: 30, considered: 0, policies: [] },
    reducible: { days: 30, considered: 0, total_usd: 0, at_risk_usd: 0, at_risk_pct: 0,
                 by_band: { sharp: 0, good: 0, drift: 0, danger: 0 }, auto_compactions: 0, estimate: true } };
  ctx.renderEfficiency();
  out = document.getElementById('scene-efficiency').innerHTML;
  if (out.includes('may be reducible')) throw new Error('reducible card must be hidden when total is 0');
  win._eff = null;
});

// 9l2b) svgHeatmap + calendarCells (Slice 2): grid of rects; calendar builds 7 rows over ~53 week-cols
check('svgHeatmap renders a colored grid + calendarCells lays out a year', () => {
  if (typeof ctx.svgHeatmap !== 'function') throw new Error('svgHeatmap() not defined');
  if (typeof ctx.calendarCells !== 'function') throw new Error('calendarCells() not defined');
  const hm = ctx.svgHeatmap([{ x: 0, y: 0, value: 5, tip: 'a' }, { x: 1, y: 2, value: 0, tip: 'b' }], { cols: 2, rows: 3 });
  if (!hm.includes('<svg') || !hm.includes('<rect')) throw new Error('no svg/rect output');
  const cal = ctx.calendarCells([{ day: '2026-06-06', cost_usd: 3, messages: 9, tokens: 100 }], 'messages');
  if (cal.cols < 50 || cal.cols > 54) throw new Error('expected ~53 week columns, got ' + cal.cols);
  if (!cal.cells.length || cal.cells.some(c => c.y < 0 || c.y > 6)) throw new Error('calendar rows must be 0..6 (day-of-week)');
});

// 9l3) Trophy Room honest empty state must not throw
check('renderTrophy handles the empty / not-enabled state', () => {
  win._report = { enabled: false }; ctx.renderTrophy();
  if (!document.getElementById('scene-trophy').innerHTML.includes('No history yet')) throw new Error('empty-state copy missing');
  win._report = null; ctx.renderTrophy();   // null payload must also be safe
});

// 9m) trend() arrows vs a baseline (V2)
check('trend() arrows: up / down / flat vs baseline', () => {
  if (typeof ctx.trend !== 'function') throw new Error('trend() not defined');
  if (ctx.trend(120, 100).dir !== 'up') throw new Error('120 vs 100 → up');
  if (ctx.trend(80, 100).dir !== 'down') throw new Error('80 vs 100 → down');
  if (ctx.trend(101, 100).dir !== 'flat') throw new Error('101 vs 100 → flat (<5%)');
  if (ctx.trend(50, 0).dir !== 'flat') throw new Error('zero baseline → flat');
});

// 9n) context-trajectory sparkline (Phase 4b): svgSpark draws a line over zone bands; <2 pts → nothing
check('svgSpark renders a trajectory line over zone bands', () => {
  if (typeof ctx.svgSpark !== 'function') throw new Error('svgSpark() not defined');
  const out = ctx.svgSpark([{ ts: 1, ctx: 10000 }, { ts: 2, ctx: 130000 }, { ts: 3, ctx: 260000 }]);
  if (!out.includes('<svg')) throw new Error('no svg');
  if (!out.includes('<path')) throw new Error('no trajectory line path');
  if (ctx.svgSpark([{ ts: 1, ctx: 1 }]) !== '') throw new Error('a single point should render nothing');
});

// 9o2) History "Craft signals" card from the event rollup in /cost (Phase 6b)
check('History Craft-signals card (compaction discipline %)', () => {
  win._cost = { total_usd: 100, daily: [{ day: '2026-06-06', cost_usd: 10, messages: 5 }],
    projects: [{ project: 'P', cost_usd: 10, messages: 5 }],
    events: { compaction: 5, compaction_manual: 4, compaction_auto: 1, skill_use: 3, subagent_spawn: 2, tool_error: 7 } };
  ctx.renderHistory();
  const out = document.getElementById('scene-history').innerHTML;
  if (!out.includes('Craft signals')) throw new Error('craft card missing');
  if (!out.includes('80% manual')) throw new Error('compaction discipline % missing/incorrect (4/5 → 80%)');
  if (!out.includes('Skills used')) throw new Error('skills row missing');
  win._cost = null;
});

// 9p) Craft Score scene (Phase 7): big score + grade + 3 dim bars from window._craft
check('renderCraft: composite score, grade + 3 dimension bars', () => {
  if (typeof ctx.renderCraft !== 'function') throw new Error('renderCraft() not defined');
  win._craft = {
    enabled: true, score: 89.2, grade: 'B',
    dims: {
      efficiency: { score: 92.0, cache_ratio: 0.92 },
      hygiene: { score: 92.0, compaction_manual: 9, compaction_auto: 1, healthy_pct: 88.0, tool_errors: 4, messages: 200 },
      craft: { score: 83.5, skill_use: 6, subagent_spawn: 3, messages: 200 },
    },
    zone_time: { healthy_pct: 88.0 }, model_mix: {}, accounts: ['a1'],
    medals: [
      { id: 'context_surgeon', name: 'Context Surgeon', icon: '🔪', dim: 'Hygiene', kind: 'rate',
        unit: '% manual', desc: 'manual compaction', thresholds: [50, 75, 90, 100], value: 100, tier: 'Platinum',
        tier_idx: 3, tier_icon: '💎', next_threshold: null, progress: 1.0, locked: false },
      { id: 'zen_mind', name: 'Zen Mind', icon: '🧘', dim: 'Hygiene', kind: 'rate', unit: '% healthy',
        desc: 'healthy zone time', thresholds: [40, 60, 80, 95], value: null, tier: null, tier_idx: -1,
        tier_icon: null, next_threshold: 40, progress: 0, locked: true },
    ],
    level: { points: 4, max_points: 8, earned: 1, total: 2 },
  };
  ctx.renderCraft();
  const out = document.getElementById('scene-craft').innerHTML;
  if (!out.includes('Craft Score')) throw new Error('title missing');
  if (!out.includes('89.2')) throw new Error('composite score missing');
  if (!out.includes('GRADE B')) throw new Error('grade missing');
  if (!out.includes('Efficiency') || !out.includes('Hygiene') || !out.includes('Craft')) throw new Error('a dimension card is missing');
  if (!out.includes('92% of input from cache')) throw new Error('efficiency signal missing');
  if (!out.includes('90% manual compaction')) throw new Error('hygiene compaction signal missing (9/10 → 90%)');
  if (!out.includes('6 skills · 3 subagents')) throw new Error('craft signal missing');
  // Phase 8 trophy case
  if (!out.includes('Trophy case')) throw new Error('trophy case missing');
  if (!out.includes('Context Surgeon') || !out.includes('Platinum')) throw new Error('earned medal missing');
  if (!out.includes('Zen Mind') || !out.includes('locked')) throw new Error('locked medal state missing');
  if (!out.includes('4<span') && !out.includes('>4<')) { /* points header */ if (!out.includes('pts')) throw new Error('level points header missing'); }
  win._craft = null;
});

// 9p2) renderCraft handles the no-/insufficient-data states without throwing
check('renderCraft: graceful empty + unscored states', () => {
  win._craft = { enabled: false };
  ctx.renderCraft();
  if (!document.getElementById('scene-craft').innerHTML.includes('No data yet')) throw new Error('empty state missing');
  win._craft = { enabled: true, score: null };
  ctx.renderCraft();
  if (!document.getElementById('scene-craft').innerHTML.includes('Not enough signal')) throw new Error('unscored state missing');
  win._craft = null;
});

// 9p4) Track 2: renderCraft "Recent form" — last-7d vs 30-day baseline with point deltas
check('renderCraft: recent-form self-comparison (deltas vs baseline)', () => {
  win._craft = { enabled: true, score: 63, grade: 'D',
    dims: { efficiency: { score: 98, cache_ratio: 0.98 }, hygiene: { score: 74, compaction_manual: 9, compaction_auto: 1, healthy_pct: 70, tool_errors: 1, messages: 100 }, craft: { score: 18, skill_use: 1, subagent_spawn: 1, messages: 100 } },
    zone_time: { healthy_pct: 70 }, model_mix: {}, accounts: ['a1'],
    compare: {
      w7: { score: 66, dims: { efficiency: { score: 98 }, hygiene: { score: 74 }, craft: { score: 26 } } },
      w30: { score: 63, dims: { efficiency: { score: 98 }, hygiene: { score: 75 }, craft: { score: 18 } } },
    } };
  ctx.renderCraft();
  const out = document.getElementById('scene-craft').innerHTML;
  if (!out.includes('Recent form')) throw new Error('recent-form card missing');
  if (!out.includes('▲ 8')) throw new Error('craft up-delta (26 vs 18 → ▲ 8) missing');
  if (!out.includes('▼ 1')) throw new Error('hygiene down-delta (74 vs 75 → ▼ 1) missing');
  if (!out.includes('vs 30-day')) throw new Error('baseline framing missing');
  win._craft = null;
});

check('renderCraft: recent form absent when no compare data', () => {
  win._craft = { enabled: true, score: 50, grade: 'F',
    dims: { efficiency: { score: 50 }, hygiene: { score: 50 }, craft: { score: 50 } },
    zone_time: {}, model_mix: {}, accounts: [] };   // no compare key
  ctx.renderCraft();
  if (document.getElementById('scene-craft').innerHTML.includes('Recent form')) throw new Error('recent form should be absent without compare data');
  win._craft = null;
});

// 9p5) Track 2b: renderCraft "trajectory" — daily score sparkline + personal best / new-high badge
check('renderCraft: trajectory sparkline + personal best', () => {
  const base = { enabled: true, score: 60, grade: 'D',
    dims: { efficiency: { score: 98 }, hygiene: { score: 74 }, craft: { score: 18 } },
    zone_time: {}, model_mix: {}, accounts: ['a1'] };
  win._craft = Object.assign({}, base, {
    series: [{ day: '2026-06-03', score: 76 }, { day: '2026-06-04', score: 68 }, { day: '2026-06-06', score: 60 }],
    best: { day: '2026-06-03', score: 76 }, new_high: false });
  ctx.renderCraft();
  let out = document.getElementById('scene-craft').innerHTML;
  if (!out.includes('Your trajectory')) throw new Error('trajectory card missing');
  if (!out.includes('<svg')) throw new Error('score sparkline (SVG) missing');
  if (!out.includes('Personal best') || !out.includes('76')) throw new Error('personal best missing');
  // new-high badge fires when the latest day is the peak
  win._craft = Object.assign({}, base, {
    series: [{ day: '2026-06-03', score: 60 }, { day: '2026-06-06', score: 80 }],
    best: { day: '2026-06-06', score: 80 }, new_high: true });
  ctx.renderCraft();
  out = document.getElementById('scene-craft').innerHTML;
  if (!out.includes('New high score')) throw new Error('new-high badge missing');
  // <2 points → no card
  win._craft = Object.assign({}, base, { series: [{ day: '2026-06-06', score: 60 }] });
  ctx.renderCraft();
  if (document.getElementById('scene-craft').innerHTML.includes('Your trajectory')) throw new Error('single-point series should render no trajectory');
  win._craft = null;
});

// 9p3) dumb-zone v2: renderDrift fills the Help card with per-band error rates + the knee verdict
check('renderDrift: knee case shows the band + warning', () => {
  if (typeof ctx.renderDrift !== 'function') throw new Error('renderDrift() not defined');
  win._drift = { enabled: true, overall: { messages: 200, errors: 21, baseline_rate: 0.01,
      knee_band: 'danger', knee_tokens: 200000, bands: [
        { band: 'sharp', floor: 0, messages: 100, errors: 1, rate: 0.01 },
        { band: 'good', floor: 50000, messages: 0, errors: 0, rate: null },
        { band: 'drift', floor: 120000, messages: 0, errors: 0, rate: null },
        { band: 'danger', floor: 200000, messages: 100, errors: 20, rate: 0.2 }] },
    by_model: { 'claude-opus-4-8': { knee_band: 'danger', knee_tokens: 200000 } } };
  ctx.renderDrift();
  const out = document.getElementById('drift-card').innerHTML;
  if (!out.includes('Measured from')) throw new Error('data header missing');
  if (!out.includes('20.0%')) throw new Error('danger band rate missing');
  if (!out.includes('climb in the <b>danger</b>')) throw new Error('knee warning missing');
  if (!out.includes('claude-opus-4-8')) throw new Error('per-model line missing');
  win._drift = null;
});

check('renderDrift: no-knee case states it honestly', () => {
  win._drift = { enabled: true, overall: { messages: 200, errors: 20, baseline_rate: 0.1, knee_band: null,
      knee_tokens: null, bands: [
        { band: 'sharp', floor: 0, messages: 100, errors: 12, rate: 0.12 },
        { band: 'good', floor: 50000, messages: 0, errors: 0, rate: null },
        { band: 'drift', floor: 120000, messages: 0, errors: 0, rate: null },
        { band: 'danger', floor: 200000, messages: 100, errors: 8, rate: 0.08 }] }, by_model: {} };
  ctx.renderDrift();
  const out = document.getElementById('drift-card').innerHTML;
  if (!out.includes('No upward knee')) throw new Error('honest no-knee message missing');
  if (!out.includes('Correlational')) throw new Error('causation caveat missing');
  // empty/insufficient data → renders nothing (no crash)
  win._drift = { enabled: true, overall: { messages: 0, errors: 0, bands: [] } };
  ctx.renderDrift();
  if (document.getElementById('drift-card').innerHTML !== '') throw new Error('empty data should render nothing');
  win._drift = null;
});

// 9o) event chips (Phase 6): compaction manual/auto split + skills/subagents/errors
check('evChips formats event counts', () => {
  if (typeof ctx.evChips !== 'function') throw new Error('evChips() not defined');
  const out = ctx.evChips({ compaction: 3, compaction_manual: 1, compaction_auto: 2, skill_use: 2, subagent_spawn: 4, tool_error: 5 });
  if (!out.includes('3 compact')) throw new Error('compaction total missing');
  if (!out.includes('1 manual · 2 auto')) throw new Error('manual/auto split missing');
  if (!out.includes('2 skills')) throw new Error('skills missing');
  if (!out.includes('4 subagents')) throw new Error('subagents missing');
  if (!out.includes('5 errors')) throw new Error('errors missing');
  if (ctx.evChips(null) !== '') throw new Error('null → empty string');
});

// 9m) mcpChips (MCP tax Slice 1): observed MCP usage, busiest server first, error chip flagged, empty → ''
check('mcpChips renders observed MCP usage sorted busiest-first with error flag', () => {
  if (typeof ctx.mcpChips !== 'function') throw new Error('mcpChips() not defined');
  if (ctx.mcpChips({}) !== '') throw new Error('empty MCP map → empty string');
  if (ctx.mcpChips(null) !== '') throw new Error('null → empty string');
  const out = ctx.mcpChips({
    filesystem: { tools: ['read_file'], calls: 2, errors: 0 },
    github: { tools: ['create_issue', 'list_prs'], calls: 5, errors: 1 },
  });
  if (!out.includes('2 servers')) throw new Error('server count missing');
  if (!out.includes('7 calls')) throw new Error('total call count missing');
  if (out.indexOf('github') > out.indexOf('filesystem')) throw new Error('not sorted busiest-first');
  if (!out.includes('class="mcpchip err"')) throw new Error('error server not flagged with err class');
  if (!out.includes('⚠1')) throw new Error('error count badge missing');
  if (!out.includes('2 tools')) throw new Error('multi-tool count missing');
});

// 9n) renderMcp (MCP tax Slice 2): per-host configured-vs-used, dead-weight, no-live-session caveat, setup card
check('renderMcp renders configured/used/dead servers + setup card + empty state', () => {
  if (typeof ctx.renderMcp !== 'function') throw new Error('renderMcp() not defined');
  // empty → no-reports message + setup card (always offer the probe)
  win._mcp = { enabled: true, hosts: [] }; ctx.renderMcp();
  let out = document.getElementById('scene-mcp').innerHTML;
  if (!out.includes('No MCP reports yet')) throw new Error('empty state missing');
  if (!out.includes('Set up MCP tracking')) throw new Error('setup card missing in empty state');
  if (!out.includes('mcp_probe.py')) throw new Error('probe command missing');
  // populated: over-time usage (events) — a used+live server and a dead-weight (never-called-in-30d) one
  win._mcp = { enabled: true, window_days: 30, hosts: [
    { host: 'my-desktop', ts: 1, age_secs: 120, configured: 2, used: 1, dead: 1, window_days: 30,
      servers: [ { name: 'github', status: 'connected', calls: 4, last_ts: (Date.now() / 1000) - 3600, used: true, live: true },
                 { name: 'jira', status: 'failed', calls: 0, last_ts: null, used: false, live: false } ] },
  ] };
  ctx.renderMcp();
  out = document.getElementById('scene-mcp').innerHTML;
  const txt = out.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');   // strip markup + collapse so "<b>1</b> used" → "1 used"
  if (!txt.includes('1 used') || !txt.includes('1 dead weight')) throw new Error('per-host summary missing');
  if (!out.includes('(last 30d)')) throw new Error('over-time window label missing');
  if (!out.includes('4 calls')) throw new Error('used-server call count missing');
  if (!out.includes('●live')) throw new Error('live badge missing on currently-active server');
  if (!out.includes('unused · 30d')) throw new Error('over-time dead-weight marker missing');
  if (!out.includes('Set up MCP tracking')) throw new Error('setup card missing when populated');
  win._mcp = null;
});

// 9o) renderMcp context-floor card (MCP tax Slice 3): real-billed prefix, Sharp-zone framing, honesty caveats
check('renderMcp shows the context-floor card (real billed, not MCP-only, no-secrets caveat)', () => {
  win._mcp = { enabled: true, estimate: false, hosts: [],
    floor: { median: 37500, p90: 60000, max: 90000, sessions: 12 } };
  ctx.renderMcp();
  const out = document.getElementById('scene-mcp').innerHTML;
  if (!out.includes('context floor')) throw new Error('floor card heading missing');
  if (!out.includes('37.5k')) throw new Error('median floor (fmtTok) missing');
  if (!out.includes('Sharp zone')) throw new Error('Sharp-zone framing missing');
  if (!out.includes('real billed tokens')) throw new Error('billed-not-estimate honesty missing');
  if (!out.includes('not MCP-only')) throw new Error('MCP-only honesty caveat missing');
  if (!out.includes('refuses to read')) throw new Error('no-secrets caveat missing');
  win._mcp = null;
});

// 9p) 🧩 MCP scene "?" help: header affordance present, body builds the populate + auto-run sections,
// and open/close toggles the modal without throwing (regression: the copy bug taught us to wire+test interactions)
check('renderMcp shows a ? help button and mcpHelpBody builds populate + OS auto-run sections', () => {
  win._mcp = { enabled: true, hosts: [] }; ctx.renderMcp();
  const scene = document.getElementById('scene-mcp').innerHTML;
  if (!scene.includes('data-mcphelp')) throw new Error('? help button missing from MCP scene header');
  if (typeof ctx.mcpHelpBody !== 'function') throw new Error('mcpHelpBody() not defined');
  const body = ctx.mcpHelpBody();
  if (!body.includes('mcp_probe.py')) throw new Error('help: probe command missing');
  if (!body.includes('http://localhost:8099')) throw new Error('help: live dashboard URL not injected into commands');
  // OS-native auto-run prompt covers all three platforms + SessionStart, and stays secret-safe
  if (!/systemd/.test(body) || !/launchd/.test(body) || !/schtasks/.test(body)) throw new Error('help: auto-run prompt missing an OS scheduler (linux/mac/windows)');
  if (!body.includes('SessionStart')) throw new Error('help: SessionStart hook guidance missing');
  if (!/Names \+ status only/.test(body)) throw new Error('help: no-secrets honesty line missing');
  // the copyable prompt must survive esc()-into-attribute: no raw double-quotes or backticks in the payload
  const m = body.match(/data-copy="([^"]*)"/g) || [];
  if (!m.length) throw new Error('help: no copyable blocks found');
  if (/[`]/.test(body.replace(/<[^>]*>/g, ''))) throw new Error('help: backtick in copy payload would break the template literal');
  if (typeof ctx.openMcpHelp !== 'function' || typeof ctx.closeMcpHelp !== 'function') throw new Error('open/closeMcpHelp not defined');
  ctx.openMcpHelp(); ctx.closeMcpHelp();   // must not throw in the stubbed DOM
  win._mcp = null;
});

// 9m) 🧠 Coach scene (V8 Slice A): Claude Code handoff + content-free bundle + honesty framing + empty/null
check('renderCoach: Claude Code handoff + bundle + honesty banner + empty/null states', () => {
  if (typeof ctx.renderCoach !== 'function') throw new Error('renderCoach() not defined');
  win._coach = { enabled: true, engine: 'handoff', narrative: null, estimate: true,
    bundle: { score: 71.2, grade: 'C', healthy_pct: 42.0, cache_ratio: 0.9, compaction_manual: 6,
              compaction_auto: 1, skill_use: 2, subagent_spawn: 5, messages: 800 },
    recent_sessions: [{ session_id: 'sid-A', project: 'proj-A', host: 'h1', ts: 1700000000, messages: 320 }],
    handoff: { cmd: '/claude-coach', note: 'Run this in your own Claude Code.' } };
  ctx.renderCoach();
  const out = document.getElementById('scene-coach').innerHTML;
  if (!out.includes('Coach me with Claude Code')) throw new Error('Claude Code handoff card missing');
  if (!out.includes('/claude-coach') || !out.includes('data-copy="/claude-coach"')) throw new Error('copyable /claude-coach cmd missing');
  if (!out.includes('content-free')) throw new Error('content-free honesty banner missing (must always show)');
  if (!out.includes('opportunities, not judgments')) throw new Error('opportunities-not-judgments framing missing');
  if (!out.includes('Session Autopsy')) throw new Error('Autopsy section missing');
  if (!out.includes('data-asid="sid-A"')) throw new Error('recent-session picker chip missing');
  if (!out.includes('71.2')) throw new Error('craft-score bundle figure missing');
  // no narrative yet → a "Generate in-app" button (Slice B) is offered
  if (!out.includes('data-coach-gen') || !out.includes('Generate in-app')) throw new Error('Generate-in-app button missing when no narrative');
  // a Slice-B narrative, when present, renders + is labelled an estimate + offers Regenerate
  win._coach = Object.assign({}, win._coach, { narrative: 'You compact early — nice.\nWatch the Danger zone.', narrative_engine: 'claude', generated_at: '2026-06-06 12:00:00' });
  ctx.renderCoach();
  const n = document.getElementById('scene-coach').innerHTML;
  if (!n.includes('Watch the Danger zone')) throw new Error('narrative not rendered when present');
  if (!n.includes('claude')) throw new Error('narrative_engine label missing');
  if (!n.replace(/<[^>]+>/g, ' ').includes('not a judgment')) throw new Error('narrative honesty caveat missing');
  if (!n.includes('Regenerate')) throw new Error('Regenerate button missing when a narrative exists');
  // pending → disabled "Generating…" button, no generate trigger
  win._coach = Object.assign({}, win._coach, { pending: true });
  ctx.renderCoach();
  const pg = document.getElementById('scene-coach').innerHTML;
  if (!pg.includes('Generating') || pg.includes('data-coach-gen')) throw new Error('pending state should disable the generate button');
  win._coach = { enabled: false }; ctx.renderCoach();
  if (!document.getElementById('scene-coach').innerHTML.includes('No data yet')) throw new Error('empty state missing');
  win._coach = null; ctx.renderCoach();   // null payload must be safe
});

// 9m2) renderAutopsy: zone timeline + model mix + event chips + cost; error/null safe
check('renderAutopsy: report card renders timeline, model mix, events; error-safe', () => {
  if (typeof ctx.renderAutopsy !== 'function') throw new Error('renderAutopsy() not defined');
  ctx.renderAutopsy({ session_id: 'sid-A', cost_usd: 1.05, messages: 5, duration_secs: 180, cache_ratio: 0.9,
    model_mix: { 'claude-opus-4-8': 3, 'claude-sonnet-4-6': 1 },
    zone_series: [{ ts: 1, ctx: 10000 }, { ts: 2, ctx: 60000 }, { ts: 3, ctx: 150000 }, { ts: 4, ctx: 250000 }],
    zone_time: { sharp: 1, good: 1, drift: 1, danger: 1, total: 4, healthy_pct: 50.0 },
    events: { compaction_manual: 1, compaction_auto: 1, skill_use: 1, subagent_spawn: 1, tool_error: 1 },
    prefix_overhead: 38000 });
  const out = document.getElementById('autopsy-body').innerHTML;
  if (!out.includes('opus')) throw new Error('model mix missing');
  if (!out.includes('manual compact')) throw new Error('compaction event chip missing');
  if (!out.includes('auto-compact')) throw new Error('auto-compaction chip missing');
  if (!out.includes('healthy zone')) throw new Error('zone health line missing');
  if (!out.includes('<svg')) throw new Error('zone-timeline sparkline missing');
  ctx.renderAutopsy({ error: true });   // error payload → friendly message, no throw
  if (!document.getElementById('autopsy-body').innerHTML.includes('Couldn’t load')) throw new Error('error state missing');
  ctx.renderAutopsy(null);   // null safe
});

// 9m3) renderSearch (V9): local-only wall / opt-in enable / results — and the XSS+sentinel contract.
check('renderSearch: local-only wall, enable card, results render + escape raw content (XSS)', () => {
  if (typeof ctx.renderSearch !== 'function') throw new Error('renderSearch() not defined');
  // (a) off-host → a local-only wall, the content port, and NO search box / NO content
  win._searchMeta = { local: false, content_port: 8101, enabled: false, stats: {} };
  ctx.renderSearch();
  let out = document.getElementById('scene-search').innerHTML;
  if (!out.includes('Local-only')) throw new Error('local-only wall missing on a remote client');
  if (!out.includes('localhost:8101')) throw new Error('wall must tell the user the content port');
  if (out.includes('id="search-q"')) throw new Error('no search box may render off-host');
  // (b) local but not enabled → the opt-in enable card, no do-not-screenshot banner yet (nothing to protect)
  win._searchMeta = { local: true, enabled: false, content_port: 8101, stats: { docs: 0, sessions: 0 } };
  ctx.renderSearch();
  out = document.getElementById('scene-search').innerHTML;
  if (!out.includes('Enable content search') || !out.includes('data-search-enable')) throw new Error('enable card/button missing');
  if (out.includes('do not screenshot')) throw new Error('do-not-screenshot banner should not show before content exists');
  if (out.includes('id="search-q"')) throw new Error('no search box until enabled');
  // V13 2B: the consent copy must name the real risk (unredacted secrets) + the 14-day auto-delete default
  if (!/unredacted/i.test(out)) throw new Error('consent copy must say content is unredacted by default');
  if (!/secret/i.test(out)) throw new Error('consent copy must warn about secrets');
  if (!/14 days/i.test(out)) throw new Error('consent copy must state the 14-day auto-delete default');
  // (c) enabled + results → banner + search box + stats; raw content is ESCAPED, hit sentinels become <mark>
  win._searchMeta = { local: true, enabled: true, content_port: 8101, stats: { docs: 28348, sessions: 89, last_indexed_ts: 1700000000 },
    embed: { available: true, model: 'nomic-embed-text', embedded: 1200, embeddable: 1200, error: null },
    config: { retention_days: 14, redact: false, scope: 'all' } };
  const A = String.fromCharCode(2), B = String.fromCharCode(3);   // the server's hit sentinels
  win._searchResults = { query: 'foo', results: [{ session_id: 'sid-X', project: 'proj-X', kind: 'assistant',
    ts: 1700000000, snippet: 'before ' + A + 'foo' + B + ' after <script>alert(1)</script> & <b>x</b>' }] };
  ctx.renderSearch();
  out = document.getElementById('scene-search').innerHTML;
  if (!out.includes('do not screenshot')) throw new Error('do-not-screenshot banner must show when enabled');
  if (!out.includes('id="search-q"')) throw new Error('search box missing when enabled');
  if (!out.includes('28,348') && !out.includes('28348')) throw new Error('indexed-blocks stat missing');
  // kind + date filter chips present (and the filter helpers/handlers exist)
  if (!out.includes('data-fkind="user"') || !out.includes('data-fkind="tool_use"')) throw new Error('kind filter chips missing');
  if (!out.includes('data-frange="604800"') || !out.includes('>All<')) throw new Error('date-range filter chips missing');
  if (typeof ctx.toggleKind !== 'function' || typeof ctx.setRange !== 'function') throw new Error('filter toggle handlers missing');
  // keyword ⇄ semantic mode toggle present + handler exists
  if (!out.includes('data-smode="keyword"') || !out.includes('data-smode="semantic"')) throw new Error('mode toggle missing');
  if (typeof ctx.setMode !== 'function') throw new Error('setMode handler missing');
  // V13 2B: privacy controls (retention / redaction / scope) + handler
  if (!out.includes('data-cfg-retention="14"') || !out.includes('data-cfg-retention="0"')) throw new Error('retention chips missing');
  if (!out.includes('data-cfg-redact=') ) throw new Error('redaction toggle missing');
  if (!out.includes('data-cfg-scope="conversation"')) throw new Error('scope toggle missing');
  if (typeof ctx.setContentConfig !== 'function') throw new Error('setContentConfig handler missing');
  // results render into the #search-results sub-element (like autopsy-body), not the scene string
  const res = document.getElementById('search-results').innerHTML;
  if (!res.includes('<mark>foo</mark>')) throw new Error('hit sentinels not converted to <mark>');
  if (res.includes('<script>alert')) throw new Error('raw content NOT escaped — XSS hole');
  if (!res.includes('&lt;script&gt;')) throw new Error('escaping did not run on the snippet');
  // (c2) semantic mode: a score badge renders on results
  ctx.setMode && ctx.setMode('semantic');
  win._searchResults = { query: 'meaning', results: [{ session_id: 'sid-S', project: 'p', kind: 'assistant',
    ts: 1700000000, snippet: 'a relevant block', score: 0.73 }] };
  ctx.renderSearch();
  if (!document.getElementById('scene-search').innerHTML.includes('Semantic')) throw new Error('semantic mode label missing');
  if (!document.getElementById('search-results').innerHTML.includes('73% match')) throw new Error('semantic score badge missing');
  ctx.setMode && ctx.setMode('keyword');   // restore
  // (d) null payload is safe
  win._searchMeta = null; win._searchResults = null; ctx.renderSearch();
  if (!document.getElementById('scene-search').innerHTML.includes('Loading')) throw new Error('null meta should show a loading state');
});

// 9m4) renderConvo (V9 Slice 3): the conversation reader — chat blocks, hit highlighted, query terms marked,
// raw content escaped, load-more buttons gated on has_more.
check('renderConvo: chat blocks render, hit highlighted, query terms marked, content escaped', () => {
  if (typeof ctx.renderConvo !== 'function') throw new Error('renderConvo() not defined');
  if (typeof ctx.openConvo !== 'function') throw new Error('openConvo() not defined');
  win._searchResults = { query: 'needle', results: [] };   // convoTerms() reads the active query
  ctx.renderConvo([
    { pos: 10, kind: 'user', ts: 1700000000, text: 'where is the needle', hit: false },
    { pos: 11, kind: 'assistant', ts: 1700000001, text: 'found the needle in <script>alert(1)</script> & <b>x</b>', hit: true },
    { pos: 12, kind: 'tool_use', ts: 1700000002, text: 'grep needle file.py', hit: false },
  ], true, true);
  const out = document.getElementById('convobody').innerHTML;
  if (!out.includes('cvhit')) throw new Error('hit block not flagged');
  if (!out.includes('<mark>needle</mark>')) throw new Error('query term not highlighted in the reader');
  if (out.includes('<script>alert')) throw new Error('raw conversation content NOT escaped — XSS hole');
  if (!out.includes('&lt;script&gt;')) throw new Error('escaping did not run in the reader');
  if (!out.includes('data-cv-up')) throw new Error('load-earlier button missing when has_more_before');
  if (!out.includes('data-cv-down')) throw new Error('load-later button missing when has_more_after');
  // no-more on both sides → no paging buttons
  ctx.renderConvo([{ pos: 1, kind: 'assistant', text: 'only block', hit: true }], false, false);
  const out2 = document.getElementById('convobody').innerHTML;
  if (out2.includes('data-cv-up') || out2.includes('data-cv-down')) throw new Error('paging buttons shown with nothing more to load');
  ctx.renderConvo([], false, false);   // empty safe
});

// 10) mute toggle persists intent + flips the icon (guards the localStorage-free sandbox path)
check('toggleMute flips state without throwing', () => {
  if (typeof ctx.toggleMute !== 'function') throw new Error('toggleMute() not defined');
  ctx.toggleMute(); ctx.toggleMute();   // off→on→off; must not throw with LS=null
});

// ---- 🧹 Maintenance scene (V13 Slice 5) ----
check('renderMaint: storage card + export/import/checkpoint/prune/vacuum controls + 1Password warning', () => {
  if (typeof ctx.renderMaint !== 'function') throw new Error('renderMaint() not defined');
  win._maint = {
    export_dir: '/data/exports', runtime: 'docker',
    last_export: { path: '/data/exports/cc-obs-export-x.json.gz.enc', bytes: 90123, ts: 1700000000 },
    stats: {
      usage: { sizes: { main: 5000000, wal: 0, total: 5000000 }, rows: { usage: 12000, events: 700 },
               oldest_ts: 1700000000, projected_bytes_per_year: 53000000 },
      content: { sizes: { main: 122000000, wal: 0, total: 122000000 }, rows: { docs: 34000 },
                 oldest_ts: 1700000000, projected_bytes_per_year: 1200000000 },
    },
  };
  ctx.renderMaint();
  const out = document.getElementById('scene-maint').innerHTML;
  if (!/Storage/.test(out)) throw new Error('storage card missing');
  if (!/data-maint-export\b/.test(out) || !/data-maint-import/.test(out)) throw new Error('export/import buttons missing');
  if (!/data-maint-checkpoint/.test(out) || !/data-maint-prune/.test(out) || !/data-maint-vacuum/.test(out)) throw new Error('maintenance action buttons missing');
  if (!/1Password/i.test(out)) throw new Error('passphrase/1Password warning missing');
  if (!/encrypt/i.test(out)) throw new Error('must state exports are encrypted');
  if (!/MB/.test(out)) throw new Error('byte sizes not formatted');
  if (!/download/i.test(out)) throw new Error('download link missing when last_export present');
  for (const fn of ['pollMaint', 'maintExport', 'maintImport', 'maintCheckpoint', 'maintPrune', 'maintVacuum']) {
    if (typeof ctx[fn] !== 'function') throw new Error(fn + ' handler missing');
  }
  // empty/loading state must not throw
  win._maint = null; ctx.renderMaint();
  if (!/Loading/.test(document.getElementById('scene-maint').innerHTML)) throw new Error('loading state missing');
});

// ---- ℹ️ About scene ----
check('renderAbout: disclaimer + MIT license + version rendered; loading state on null', () => {
  if (typeof ctx.renderAbout !== 'function') throw new Error('renderAbout() not defined');
  win._about = { version: '0.13.0', runtime: 'docker' };
  ctx.renderAbout();
  const out = document.getElementById('scene-about').innerHTML;
  if (!/not affiliated/.test(out)) throw new Error('disclaimer phrase "not affiliated" missing');
  if (!/MIT/.test(out)) throw new Error('MIT license text missing');
  if (!/0\.13\.0/.test(out)) throw new Error('version "0.13.0" not rendered');
  // loading state must not throw
  win._about = null; ctx.renderAbout();
  if (!/Loading/.test(document.getElementById('scene-about').innerHTML)) throw new Error('loading state missing');
});

check('renderAbout: "Terms & License" button present in the About scene', () => {
  win._about = { version: '0.13.0', runtime: 'docker' };
  ctx.renderAbout();
  const out = document.getElementById('scene-about').innerHTML;
  if (!/Terms.*License|Terms &amp; License/.test(out)) throw new Error('"Terms & License" button missing from About scene');
  if (!out.includes('data-terms-open')) throw new Error('data-terms-open attribute missing from the Terms button');
  win._about = null;
});

// ---- Suggestions / Reading card ----
check('render(): suggest card renders after the needs section with a Read → link (rel=noopener)', () => {
  ctx.render(STATE);   // STATE has a needs_me session so we exercise the "hot" needs branch
  const out = document.getElementById('app').innerHTML;
  if (!out.includes('class="suggest"')) throw new Error('suggest card (.suggest) missing from Triage render');
  if (!out.includes('Read →')) throw new Error('"Read →" link missing from the suggest card');
  if (!out.includes('rel="noopener noreferrer"')) throw new Error('Read → link missing rel="noopener noreferrer"');
  // card must appear AFTER the needs section
  const needsPos = out.indexOf('class="needs ');
  const suggestPos = out.indexOf('class="suggest"');
  if (suggestPos < needsPos) throw new Error('suggest card appeared before the needs section (wrong render order)');
});

check('render(): suggest card appears even in the "all clear" (no needs_me) state', () => {
  ctx.render([{ ...STATE[1], needs_me: false }]);
  const out = document.getElementById('app').innerHTML;
  if (!out.includes('class="suggest"')) throw new Error('suggest card missing when all-clear');
});

check('suggest: default state has suggest.fetch unchecked (no network call by default)', () => {
  // prefGet('suggest.fetch', false) → false unless explicitly set
  // DOM: the checkbox renders without the checked attribute
  ctx.render(STATE);
  const out = document.getElementById('app').innerHTML;
  if (!out.includes('id="sg-fetch-chk"')) throw new Error('suggest opt-in checkbox missing');
  // The checkbox should NOT render with "checked" in the default state (prefGet returns false for LS=null)
  // We verify no unconditional "checked" attribute on the checkbox element
  const chkMatch = out.match(/id="sg-fetch-chk"[^>]*/);
  if (!chkMatch) throw new Error('could not locate checkbox attributes');
  if (/\bchecked\b/.test(chkMatch[0])) throw new Error('checkbox renders checked by default — would enable network call on load');
});

// ---- suggest: opt-in fetch tests ----

check('suggest: zero-egress invariant — fetch NOT called when suggest.fetch is off (default)', () => {
  // Record whether the gist URL was fetched while suggest.fetch=false (the default)
  let fetchCalled = false;
  const origFetch = sandbox.fetch;
  sandbox.fetch = async (url) => {
    if (String(url).includes('gist.githubusercontent.com')) fetchCalled = true;
    return origFetch(url);
  };
  try {
    ctx.render(STATE);
  } finally {
    sandbox.fetch = origFetch;
  }
  if (fetchCalled) throw new Error('fetch was called to the gist URL when suggest.fetch is off — zero-egress invariant violated');
});

check('suggest: opted-in → _sgFetch() calls fetch with SUGGEST_URL', () => {
  let fetchedUrl = null;
  const origFetch = sandbox.fetch;
  sandbox.fetch = async (url) => {
    fetchedUrl = url;
    return { ok: true, json: async () => ({ version: 1, items: [] }) };
  };
  try {
    ctx._sgSetFetched(null);  // reset in-memory cache via test-hook
    ctx._sgFetch();           // directly call the fetch function as if user opted in
  } finally {
    sandbox.fetch = origFetch;
  }
  if (!fetchedUrl) throw new Error('fetch was never called after _sgFetch()');
  if (!String(fetchedUrl).includes('gist.githubusercontent.com')) {
    throw new Error('fetch was called but not with the gist URL: ' + fetchedUrl);
  }
});

check('suggest: fetched items — esc() applied: XSS bait in title/blurb comes out escaped, not raw', () => {
  // Call the sanitizer directly to verify the returned item has raw strings (esc() is applied at render time)
  const item = ctx._sgSanitizeItem({
    kind: 'article',
    title: '<script>alert(1)</script>',
    blurb: '"><img onerror=alert(1)>',
    url: 'https://thedelay.com',
    tags: ['test']
  });
  if (!item) throw new Error('_sgSanitizeItem returned null for a valid item');
  // Inject the item via test-hook, render, then verify the HTML is escaped
  const origFetched = ctx._sgGetFetched();
  ctx._sgSetFetched([item]);
  ctx.render(STATE);
  const out = document.getElementById('app').innerHTML;
  // The raw strings must NOT appear; esc() should have replaced < > "
  if (out.includes('<script>')) throw new Error('raw <script> tag found in rendered HTML — XSS not escaped');
  if (out.includes('<img')) throw new Error('raw <img> tag found in rendered HTML — XSS not escaped');
  ctx._sgSetFetched(origFetched);
});

check('suggest: item with javascript: url is dropped by _sgSanitizeItem (not rendered as a link)', () => {
  const item = ctx._sgSanitizeItem({
    kind: 'article',
    title: 'Malicious link',
    blurb: 'Should be dropped',
    url: 'javascript:alert(document.cookie)',
    tags: []
  });
  if (!item) return; // item dropped entirely — that's fine and safe
  // If not dropped, the url field must be null (sanitizer cleared it)
  if (item.url !== null) throw new Error('javascript: url was not cleared by sanitizer — got: ' + item.url);
  // Verify it does not render as a link
  const origFetched = ctx._sgGetFetched();
  ctx._sgSetFetched([item]);
  ctx.render(STATE);
  const out = document.getElementById('app').innerHTML;
  if (out.includes('javascript:')) throw new Error('javascript: url leaked into rendered HTML');
  ctx._sgSetFetched(origFetched);
});

check('suggest: malformed/failed fetch → card still renders bundled list, no throw', () => {
  const origFetch = sandbox.fetch;
  sandbox.fetch = async (url) => {
    if (String(url).includes('gist.githubusercontent.com')) throw new Error('network error');
    return origFetch(url);
  };
  ctx._sgSetFetched(null);  // ensure no cached items from earlier tests
  try {
    ctx._sgFetch(); // must not throw synchronously
  } catch (e) {
    sandbox.fetch = origFetch;
    throw new Error('_sgFetch() threw synchronously on fetch failure: ' + e.message);
  }
  sandbox.fetch = origFetch;
  // Card should still render using the bundled list (no crash)
  ctx.render(STATE);
  const out = document.getElementById('app').innerHTML;
  if (!out.includes('class="suggest"')) throw new Error('suggest card missing after fetch failure');
  if (!out.includes('Read →')) throw new Error('Read → link missing from suggest card after fetch failure');
});

// suggest: auto-advance — _sgTick() cycles to the next article (standalone 20s timer; pauses on hover/focus)
check('suggest: _sgTick auto-advances to the next article', () => {
  ctx.setFeat('dumbzone', false);   // no live-tip item → just the bundled articles
  ctx._sgSetFetched(null);          // use the bundled list
  ctx.render(STATE);
  // title is now wrapped in an <a> when the item has a url — tolerate the optional anchor when extracting
  const titleOf = () => (document.getElementById('app').innerHTML.match(/sg-title">(?:<a[^>]*>)?([^<]*)/) || [])[1];
  const before = titleOf();
  ctx._sgTick();                    // advance one
  const after = titleOf();
  if (!before || !after) throw new Error('suggest title not found before/after tick');
  if (before === after) throw new Error('auto-advance (_sgTick) did not change the displayed article');
  ctx.setFeat('dumbzone', true);    // restore default
});

// suggest: the article title is itself a clickable link to the article url (mirrors the Read → link)
check('suggest: article title is a clickable link to the same url as Read →', () => {
  ctx.setFeat('dumbzone', false);   // bundled articles only (all have urls)
  ctx._sgSetFetched(null);
  ctx.render(STATE);
  const html = document.getElementById('app').innerHTML;
  // the title div must contain an anchor opening in a new tab with rel=noopener
  const m = html.match(/sg-title"><a href="([^"]+)"[^>]*target="_blank"[^>]*rel="noopener noreferrer"/);
  if (!m) throw new Error('sg-title is not a clickable new-tab link');
  // and that href must equal the Read → link's href (same destination)
  const read = html.match(/class="sg-read" href="([^"]+)"/);
  if (!read) throw new Error('Read → link missing');
  if (m[1] !== read[1]) throw new Error('title link url does not match Read → link url');
  ctx.setFeat('dumbzone', true);
});

// ---- 🆕 self-update banner (V13 Slice 6) ----
check('renderUpdate: shows banner + verb when newer, hidden otherwise, dismissable', () => {
  if (typeof ctx.renderUpdate !== 'function') throw new Error('renderUpdate() not defined');
  win._updateDismissed = false;
  win._update = { current: '0.13.0', latest: '0.14.0', update_available: true,
                  verb: 'docker compose pull && docker compose up -d' };
  ctx.renderUpdate();
  const bar = document.getElementById('updatebar');
  if (bar.style.display === 'none') throw new Error('banner should show when update available');
  if (!/0\.14\.0/.test(bar.innerHTML) || !/docker compose/.test(bar.innerHTML)) throw new Error('banner missing version/verb');
  // no update → hidden
  win._update = { current: '0.13.0', latest: '0.13.0', update_available: false };
  ctx.renderUpdate();
  if (document.getElementById('updatebar').style.display !== 'none') throw new Error('banner should hide when up-to-date');
});

// ---- report ----
let failed = 0;
for (const [ok, name] of results) { console.log((ok ? '  PASS ' : '  FAIL ') + name); if (!ok) failed++; }
console.log(`ui_smoke: ${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
