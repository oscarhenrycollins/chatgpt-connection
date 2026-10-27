"use strict";
const $ = id => document.getElementById(id);
const TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'expired', 'needs_review']);
const STATUS = {ready: 'Ready', connected: 'Ready', renewing: 'Renewing', expired: 'Sign in again', cooldown: 'Cooling down', exhausted: 'Limit reached', error: 'Problem'};
let snapshot = null, active = null, busy = false, timer = null, intent = null, before = 0;
let dialogHidden = false, lastRootBack = 0;
const openManage = new Set();
const manageable = () => snapshot?.management?.contract === 'wizard.account-management/1' && snapshot.management.status === 'available';
const allowed = (action, account, credential) => manageable() && (action === 'add' ? snapshot.actions?.add === true : account?.actions?.[action] === true && credential?.managed === true);

const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
const provider = () => snapshot?.provider || (location.pathname.includes('claude') ? 'claude' : 'chatgpt');
const brand = () => provider() === 'claude' ? 'Claude' : 'ChatGPT';
const hermesAccounts = () => (snapshot?.accounts || []).filter(a => a.source !== 'legacy-claude-code');

async function api(path, body) {
  const r = await fetch(path, {cache: 'no-store', ...(body ? {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Wizard-Account-Action': '1'}, body: JSON.stringify(body)} : {})});
  if (!r.ok) {
    const e = new Error('Request failed'); e.status = r.status;
    try { const d = await r.json(); e.detail = typeof d.error === 'string' ? d.error : ''; } catch { e.detail = ''; }
    throw e;
  }
  return r.json();
}
// ChatGPT refuses Codex device codes until the owner turns this on once; say so before the code, not after.
const DEVICE_HINT = 'First, in ChatGPT open Settings → Security and turn on “Device code sign-in for Codex”.';

function usedOf(l) {
  if (typeof l.usedPercent === 'number') return Math.round(l.usedPercent);
  if (typeof l.remainingPercent === 'number') return Math.round(100 - l.remainingPercent);
  return null;
}
function resetText(ts) {
  const ms = new Date(ts) - Date.now();
  if (!Number.isFinite(ms)) return '';
  if (ms <= 0) return 'Resets now';
  const m = Math.round(ms / 60000);
  if (m < 60) return `Resets in ${m} min`;
  const h = Math.round(m / 60);
  if (h < 36) return `Resets in ${h} h`;
  return `Resets in ${Math.round(h / 24)} days`;
}
function grantName(c) {
  const generic = !c.name || /^(openai-codex-oauth|anthropic-oauth|claude-oauth)(-\d+)?$|^device_code$|^claude_code$/.test(c.name);
  return generic ? 'Sign-in ' + String(c.id).slice(0, 4) : c.name;
}

function renderAccount(a) {
  const legacy = a.source === 'legacy-claude-code';
  const li = el('li', 'acct'); li.dataset.accountId = a.id;
  const head = el('div', 'wz-row acct-head');
  // Label each subscription by its signed-in email; the nickname (if different) moves to the second line.
  const nick = a.name && a.name !== a.email && !legacy ? a.name : '';
  const at = a.email ? a.email.lastIndexOf('@') : -1;
  // Readable on a phone: the name before @ is the title, the domain joins the plan line.
  const title = at > 0 ? a.email.slice(0, at) : (a.email || a.name || (legacy ? 'Claude Code sign-in' : 'Subscription'));
  const domain = at > 0 ? a.email.slice(at + 1) : '';
  const main = el('span', 'wz-main');
  const tb = el('b', null, title); tb.title = a.email || title;
  main.append(tb, el('small', null, [domain, a.plan, nick].filter(Boolean).join(' · ')));
  const ok = ['ready', 'connected'].includes(a.status);
  head.append(el('span', 'wz-tile', title.trim().charAt(0).toUpperCase() || '?'), main, el('span', 'pill ' + (ok ? 'ok' : 'warn'), STATUS[a.status] || a.status || 'Unknown'));
  li.append(head);

  const limits = a.usage?.limits || [];
  if (limits.length) {
    const box = el('div', 'meters');
    for (const l of limits) {
      const u = usedOf(l), row = el('div', 'meter'), top = el('div', 'meter-top');
      top.append(el('span', null, l.label), el('span', 'meter-val', u == null ? '—' : `${u}% used`));
      const bar = el('div', 'bar'), fill = el('i');
      fill.style.width = Math.max(0, Math.min(100, u || 0)) + '%';
      if (u >= 90) bar.classList.add('hot');
      bar.setAttribute('role', 'meter'); bar.setAttribute('aria-label', `${title} ${l.label}`);
      bar.setAttribute('aria-valuemin', '0'); bar.setAttribute('aria-valuemax', '100'); bar.setAttribute('aria-valuenow', String(u ?? 0));
      bar.append(fill); row.append(top, bar);
      if (l.resetsAt) row.append(el('small', 'meter-reset', resetText(l.resetsAt)));
      box.append(row);
    }
    li.append(box);
  } else li.append(el('p', 'muted', 'No usage yet'));

  if (legacy) {
    // The original Claude Code sign-in (the one the agent's Claude Code provider uses) renews on its own page.
    if (!['ready', 'connected', 'renewing'].includes(a.status)) {
      const again = el('a', 'link', 'Sign in again'); again.href = 'claude-code/'; again.dataset.legacySignin = '1';
      li.append(again);
    }
    return li;
  }
  const creds = a.credentials || [];
  if (creds.length) {
    const det = el('details', 'manage');
    det.open = openManage.has(a.id);
    det.addEventListener('toggle', () => det.open ? openManage.add(a.id) : openManage.delete(a.id));
    det.append(el('summary', null, creds.length === 1 ? 'Manage' : `Manage (${creds.length})`));
    for (const c of creds) {
      const r = el('div', 'grant');
      r.append(el('span', 'grant-name', grantName(c)));
      if (manageable() && c.managed === true) {
        for (const action of ['reconnect', 'remove']) {
          if (!allowed(action, a, c)) continue;
          const b = el('button', 'link' + (action === 'remove' ? ' danger' : ''), action === 'reconnect' ? 'Reconnect' : 'Remove');
          b.type = 'button'; b.disabled = busy;
          b.addEventListener('click', () => ask(action, a, c));
          r.append(b);
        }
      }
      det.append(r);
    }
    li.append(det);
  }
  return li;
}

function render(data) {
  snapshot = data;
  $('title').textContent = data.name || brand();
  document.title = data.name || brand();
  const n = data.accounts.length;
  $('count').textContent = String(n); $('count').hidden = !n;
  $('accounts').replaceChildren(...data.accounts.map(renderAccount));
  // The agent could not be asked (or cannot take sign-ins from here): say why with a Retry, never a dead Sign in.
  const problem = data.inventoryStatus === 'unavailable' ? (data.error?.message || 'Can’t reach your agent right now.')
    : data.integration?.connected === false ? data.integration.message
    : (data.management?.status === 'off' && !n ? (data.management.message || 'Signing in from this app is switched off on your agent.') : '');
  $('problem').hidden = !problem || data.legacySignin === true;
  $('problem-text').textContent = problem;
  $('empty').hidden = n > 0 || !$('problem').hidden;
  const resume = dialogHidden && busy;
  const fallback = legacyFallback();
  $('add').hidden = !resume && !allowed('add') && !fallback;
  $('add').disabled = !resume && !fallback && (busy || !allowed('add'));
  $('add').textContent = resume ? 'Resume sign-in' : (n && !fallback ? '+ Add' : 'Sign in');
}

// Without the agent's account broker (the fresh-mode Hermes Agent app or an older plugin) Claude still signs in the
// original way: the official Claude Code login on its own page, which that agent shares. The server decides
// (`legacySignin`): on an alongside box that login would not reach the agent, so the page says why instead.
function legacyFallback() {
  if (provider() !== 'claude' || allowed('add') || snapshot?.legacySignin !== true) return false;
  return !(snapshot?.accounts || []).some(a => ['ready', 'connected', 'renewing'].includes(a.status));
}

async function refresh() {
  $('refresh').classList.add('spin');
  try {
    const data = await api('api/accounts');
    render(data);
    $('message').textContent = data.error && data.inventoryStatus !== 'unavailable' ? 'Problem reading accounts.' : '';
  } catch {
    if (snapshot) render({...snapshot, management: {status: 'unavailable'}});
    $('add').hidden = true;
    $('message').textContent = 'Offline. Showing last reading.';
  } finally { $('refresh').classList.remove('spin'); }
}

/* ---------- dialog ---------- */
function setStatus(text, kind) { const s = $('dlg-status'); s.textContent = text || ''; s.className = 'dlg-status' + (kind ? ' ' + kind : ''); }
function showButtons({go = false, goLabel = 'Continue', danger = false, cancel = true, cancelLabel = 'Cancel', close = false}) {
  $('go').hidden = !go; $('go').textContent = goLabel; $('go').classList.toggle('danger', danger); $('go').disabled = false;
  $('cancel').hidden = !cancel; $('cancel').textContent = cancelLabel; $('cancel').disabled = false;
  $('close').hidden = !close;
}

function ask(action, account, credential) {
  if (busy || !allowed(action, account, credential)) return;
  intent = {action, account, credential}; active = null;
  dialogHidden = false;
  const b = brand();
  $('steps').hidden = true; $('nick').value = ''; setStatus('');
  $('nick-wrap').hidden = action !== 'add';
  if (action === 'add') {
    $('dlg-title').textContent = `Sign in to ${b}`;
    $('dlg-text').textContent = provider() === 'chatgpt'
      ? DEVICE_HINT + (hermesAccounts().length ? ' Use an account not listed yet.' : '')
      : '';
    showButtons({go: true, goLabel: 'Continue'});
  } else if (action === 'reconnect') {
    $('dlg-title').textContent = `Reconnect ${account.name}`;
    $('dlg-text').textContent = '';
    showButtons({go: true, goLabel: 'Continue'});
  } else {
    $('dlg-title').textContent = 'Remove this sign-in?';
    $('dlg-text').textContent = `${grantName(credential)} · ${account.name}`;
    showButtons({go: true, goLabel: 'Remove', danger: true});
  }
  if (!$('manage').open) $('manage').showModal();
}

async function start() {
  const {action, account, credential} = intent;
  if (!allowed(action, account, credential)) return;
  busy = true; $('add').disabled = true; $('go').disabled = true; $('cancel').disabled = true;
  before = hermesAccounts().length;
  setStatus(action === 'remove' ? 'Removing…' : 'Starting…');
  try {
    display(await api('api/operations', {action, target: credential?.id, label: action === 'add' ? $('nick').value.trim() : '', confirmed: true}));
  } catch (e) {
    busy = false; $('add').disabled = false;
    setStatus(e.status === 409 ? 'Another sign-in is running.' : (e.detail || 'Could not start. Try again.'), 'err');
    showButtons({cancel: true, cancelLabel: 'Close'});
  }
}

function display(job) {
  if (active?.id === job.id && TERMINAL.has(active.status) && !TERMINAL.has(job.status)) return;
  clearTimeout(timer); active = job;
  if (!$('manage').open && !dialogHidden) $('manage').showModal();
  $('nick-wrap').hidden = true;
  if (!TERMINAL.has(job.status)) {
    // ChatGPT: copy the code first, then open the sign-in page and paste it there.
    $('dlg-text').textContent = job.userCode ? DEVICE_HINT : '';
    $('steps').hidden = !(job.url || job.userCode || job.inputRequired);
    $('signin').href = job.url || '#'; $('signin-step').hidden = !job.url;
    $('signin').textContent = job.userCode ? 'Open sign-in and paste the code' : 'Open sign-in';
    $('code-step').hidden = !job.userCode; $('user-code').textContent = job.userCode || '';
    $('paste-step').hidden = !job.inputRequired;
    setStatus(job.url ? 'Waiting…' : 'Starting…', 'wait');
    showButtons({cancel: true, cancelLabel: 'Cancel'});
    sessionStorage.setItem('account-operation', job.id);
    timer = setTimeout(poll, 1500);
    return;
  }
  sessionStorage.removeItem('account-operation');
  busy = false; $('steps').hidden = true;
  showButtons({cancel: false, close: true});
  const done = {
    succeeded: job.action === 'remove' ? 'Removed.' : 'Connected.',
    cancelled: 'Cancelled.',
    expired: 'Timed out. Try again.',
    failed: 'Did not finish. Try again.',
    needs_review: 'Unclear. Check the list first.'
  }[job.status];
  setStatus(done, job.status === 'succeeded' ? 'ok' : (job.status === 'cancelled' ? '' : 'err'));
  $('dlg-text').textContent = provider() === 'chatgpt' && ['failed', 'expired'].includes(job.status)
    ? 'If ChatGPT refused the code: ' + DEVICE_HINT.replace(/^First, in/, 'in') + ' Then try again.' : '';
  refresh().then(() => {
    // Success means the agent itself now lists the sign-in; if it does not, say so rather than “Connected”.
    if (job.status === 'succeeded' && job.action !== 'remove' && snapshot?.inventoryStatus === 'unavailable') {
      setStatus('Signed in, but your agent can’t be reached to confirm it. Retry in a moment.', 'err');
    }
    if (job.status === 'succeeded' && job.action === 'add' && before > 0 && hermesAccounts().length <= before) {
      $('dlg-title').textContent = 'Already added';
      setStatus(`That account is already listed. Sign out of ${brand()} in your browser or use a private window.`, 'err');
    }
  });
}

async function poll() {
  const id = active.id;
  try { const job = await api('api/operations/' + id); if (active?.id === id) display(job); }
  catch (e) {
    if (active?.id !== id || TERMINAL.has(active.status)) return;
    if (e.status === 404) display({...active, status: 'needs_review'});
    else { setStatus('Checking…'); timer = setTimeout(poll, 3000); }
  }
}

$('go').addEventListener('click', start);
$('cancel').addEventListener('click', async () => {
  if (!busy || !active?.id) { $('manage').close(); return; }
  $('cancel').disabled = true; setStatus('Cancelling…');
  try { display(await api('api/operations', {action: 'cancel', operation: active.id})); }
  catch { setStatus('Checking…'); $('cancel').disabled = false; }
});
$('close').addEventListener('click', () => $('manage').close());
$('manage').addEventListener('cancel', e => { if (busy) e.preventDefault(); });
$('copy').addEventListener('click', async () => {
  const code = $('user-code').textContent;
  try { await navigator.clipboard.writeText(code); $('copy').textContent = 'Copied'; }
  catch { const r = document.createRange(); r.selectNodeContents($('user-code')); getSelection().removeAllRanges(); getSelection().addRange(r); $('copy').textContent = 'Selected'; }
  setTimeout(() => { $('copy').textContent = 'Copy'; }, 1800);
});
$('submit-code').addEventListener('click', async () => {
  const code = $('code').value.trim(); if (!code) return;
  $('code').value = ''; $('submit-code').disabled = true; setStatus('Checking…');
  try { display(await api('api/operations', {action: 'submit', operation: active.id, code})); }
  catch { setStatus('Checking… do not paste again.'); }
  finally { $('submit-code').disabled = false; }
});
$('refresh').addEventListener('click', refresh);
$('retry').addEventListener('click', refresh);
$('add').addEventListener('click', () => {
  if (dialogHidden && busy && active) { dialogHidden = false; display(active); render(snapshot); }
  else if (legacyFallback()) location.href = 'claude-code/';
  else if (snapshot) ask('add');
});

// Shared Wizard back contract: close nested UI; consume root Back without leaving.
window.WizardApp?.back.register({canGoBack: () => true, back: () => {
  if ($('manage').open) { dialogHidden = busy; $('manage').close(); if (snapshot) render(snapshot); }
  else if (openManage.size) { openManage.clear(); if (snapshot) render(snapshot); }
  else $('message').textContent = 'Subscriptions home.';
  return true;
}});
setInterval(() => { if (!document.hidden && !$('manage').open) refresh(); }, 60000);

refresh();
const saved = sessionStorage.getItem('account-operation');
if (saved && /^[A-Za-z0-9_-]{20,64}$/.test(saved)) {
  busy = true; $('add').disabled = true; active = {id: saved};
  $('dlg-title').textContent = 'Signing in'; $('dlg-text').textContent = ''; $('nick-wrap').hidden = true;
  showButtons({cancel: true, cancelLabel: 'Cancel'}); $('manage').showModal(); poll();
}
