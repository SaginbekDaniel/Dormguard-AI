'use strict';

/* All operational content is fetched from the same-origin API. No provider keys,
   camera connection URLs, simulated incidents, or operational fixtures belong here. */
(() => {
  const API = '/api/v1';
  const ACTIONS = Object.freeze({
    DETECTED: ['review', 'acknowledge', 'false-positive'],
    PENDING_REVIEW: ['acknowledge', 'false-positive'],
    ACKNOWLEDGED: ['respond', 'confirm', 'false-positive'],
    RESPONDING: ['resolve', 'false-positive'], CONFIRMED: ['resolve'],
    RESOLVED: ['close'], CLOSED: [], FALSE_POSITIVE: []
  });
  const ACTION_LABELS = {review: 'Begin review', acknowledge: 'Acknowledge', respond: 'Mark responding', confirm: 'Confirm incident', 'false-positive': 'Dismiss detection', resolve: 'Resolve incident', close: 'Close incident'};
  const STATUS_LABELS = {DETECTED: 'Suspected', PENDING_REVIEW: 'Pending review', ACKNOWLEDGED: 'Acknowledged', RESPONDING: 'Responding', CONFIRMED: 'Confirmed', RESOLVED: 'Resolved', CLOSED: 'Closed', FALSE_POSITIVE: 'Dismissed'};
  const PATHS = {
    shield: ['M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6l-8-3Z', 'm8 12 3 3 5-6'],
    fullscreen: ['M8 3H3v5M16 3h5v5M3 16v5h5M21 16v5h-5'],
    overview: ['M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z'],
    camera: ['M3 7h12v11H3z', 'm15 10 6-3v11l-6-3', 'M6 4h6'],
    incidents: ['m12 3 10 18H2L12 3Z', 'M12 9v5', 'M12 17h.01'],
    evidence: ['M6 3h9l4 4v14H6z', 'M14 3v5h5', 'm10 12 5 3-5 3v-6Z'],
    settings: ['M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z', 'm9 3-1 3-3 1-2 3 2 2-1 4 3 2 3-1 3 2 3-2 1-3 3-2-1-3-3-1-1-3H9Z'],
    logout: ['M9 4H4v16h5', 'M9 12h12', 'm17 8 4 4-4 4'],
    refresh: ['M20 11a8 8 0 1 0-2 6', 'M20 4v7h-7'],
    arrow: ['M5 12h14', 'm14 7 5 5-5 5'],
    check: ['m5 12 4 4L19 6'],
    clock: ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z', 'M12 7v5l3 2'],
    activity: ['M2 12h5l3-8 4 16 3-8h5'],
    lock: ['M5 10h14v11H5z', 'M8 10V7a4 4 0 0 1 8 0v3'],
    plus: ['M12 5v14', 'M5 12h14'],
    menu: ['M4 6h16M4 12h16M4 18h16'],
    close: ['m6 6 12 12', 'M18 6 6 18'],
    bell: ['M5 17h14l-2-4V9a5 5 0 0 0-10 0v4l-2 4Z', 'M10 21h4'],
    download: ['M12 3v12', 'm7 10 5 5 5-5', 'M4 16v5h16v-5'],
    link: ['m10 14 4-4', 'm8 16-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0', 'm16 8 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0']
  };
  const VIEWS = [['overview', 'Overview', 'overview'], ['cameras', 'Live Cameras', 'camera'], ['incidents', 'Incidents', 'incidents'], ['analytics', 'AI Analytics', 'activity'], ['journal', 'Activity Journal', 'clock'], ['health', 'System Health', 'shield'], ['settings', 'Settings', 'settings']];
  const state = { refreshPromise: null, completedSerial: -1, completedSession: -1, entrySerial: 0, system: null, spotlight: null, cameraLayout: 'grid', selectedCamera: '', incidentOffset: 0, journalOffset: 0, journalFilters: {q: '', category: '', from_date: '', to_date: ''}, user: null, auth: {}, view: 'overview', cameras: [], detailId: null, detail: null, telegramLink: null, streams: new Map(), drafts: new Map(), filters: {status: '', camera: '', search: '', from_date: '', to_date: '', sort: 'newest', min_score: ''}, eventSource: null, poll: null, busy: false, refreshAgain: false, renderSerial: 0, sessionSerial: 0, lastUpdated: null, connected: false };
  const app = document.getElementById('app');
  const detailDialog = document.getElementById('detail-dialog');
  const actionDialog = document.getElementById('action-dialog');
  let toastTimer;
  const authChannel = 'BroadcastChannel' in window ? new BroadcastChannel('dormguard-session') : null;
  if (authChannel) authChannel.onmessage = event => {if (event.data === 'logout' && state.user) expireSession();};

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
      else if (key === 'checked' || key === 'disabled' || key === 'hidden' || key === 'required' || key === 'selected') node[key] = Boolean(value);
      else node.setAttribute(key, String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child == null || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }
  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('class', 'icon'); svg.setAttribute('aria-hidden', 'true');
    for (const d of PATHS[name] || PATHS.shield) {const path = document.createElementNS(svg.namespaceURI, 'path'); path.setAttribute('d', d); svg.append(path);}
    return svg;
  }
  function button(text, fn, cls = '', glyph = null) {const iconOnly = cls.split(' ').includes('icon-button'); return el('button', {type: 'button', class: `${cls}${glyph && !iconOnly ? ' button-with-icon' : ''}`, onclick: fn, ...(iconOnly ? {'aria-label': text, title: text} : {})}, glyph && icon(glyph), !iconOnly && text);}
  function items(value) {return Array.isArray(value) ? value : (value && Array.isArray(value.items) ? value.items : []);}
  function title(value) {return String(value || 'Unknown').replace(/[_-]/g, ' ').toLowerCase().replace(/^./, c => c.toUpperCase());}
  function date(value, short = false) {if (value == null || value === '') return 'Not reported'; const d = new Date(typeof value === 'number' ? value * 1000 : value); return Number.isNaN(d.getTime()) ? 'Unknown time' : new Intl.DateTimeFormat(undefined, short ? {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'} : {dateStyle: 'medium', timeStyle: 'short'}).format(d);}
  function bytes(value) {if (!Number.isFinite(Number(value))) return 'Size unavailable'; return Number(value) >= 1048576 ? `${(Number(value) / 1048576).toFixed(1)} MB` : `${Math.ceil(Number(value) / 1024)} KB`;}
  function isDirector() {return ['director', 'super_admin'].includes(state.user?.role);}
  function accountName() {return state.user?.display_name || state.user?.email || 'Authorized account';}
  function roleName() {return {super_admin: 'Super Administrator', director: 'Dormitory Director', deputy_director: 'Deputy Director for Educational Affairs'}[state.user?.role] || 'Unavailable role';}
  function status(value) {const key = String(value || '').toUpperCase(); return el('span', {class: `pill ${['DETECTED', 'PENDING_REVIEW', 'ACKNOWLEDGED', 'RESPONDING'].includes(key) ? 'warn' : ['CONFIRMED'].includes(key) ? 'bad' : ['RESOLVED', 'CLOSED'].includes(key) ? 'good' : ''}`}, STATUS_LABELS[key] || title(value));}
  function health(value) {const v = String(value || 'unknown').toLowerCase(); const good = ['healthy', 'active', 'online', 'running', 'ready', 'connected'].includes(v); const bad = ['error', 'failed', 'offline', 'disconnected', 'stopped'].includes(v); return el('span', {class: `pill ${good ? 'good' : bad ? 'bad' : 'warn'}`}, el('span', {class: `dot ${good ? 'good' : bad ? 'bad' : 'warn'}`}), title(v));}
  function logo() {return el('div', {class: 'logo'}, el('div', {class: 'brand-mark', 'aria-hidden': 'true'}, 'D'), el('div', {}, el('strong', {}, 'DormGuard', el('span', {class: 'muted'}, ' AI')), el('small', {}, 'Safety operations')));}
  function toast(message) {const node = document.getElementById('toast'); clearTimeout(toastTimer); node.textContent = message; node.hidden = false; toastTimer = setTimeout(() => {node.hidden = true;}, 6000);}
  function empty(glyph, heading, message, action) {return el('div', {class: 'empty'}, el('div', {class: 'empty-icon'}, icon(glyph)), el('h3', {}, heading), el('p', {}, message), action);}
  function banner(heading, message, kind = '', action = null) {return el('div', {class: `banner ${kind}`, role: kind === 'error' ? 'alert' : 'status'}, icon(kind === 'info' ? 'shield' : 'incidents'), el('div', {}, el('strong', {}, heading), el('p', {}, message)), action && el('div', {class: 'button-row'}, action));}
  function panel(heading, subtitle, body, action = null, footer = null) {return el('section', {class: 'panel'}, el('div', {class: 'panel-heading'}, el('div', {}, el('h2', {}, heading), subtitle && el('p', {}, subtitle)), action), body, footer && el('div', {class: 'panel-footer'}, footer));}
  function definition(data) {return el('dl', {class: 'definition-list'}, Object.entries(data).map(([k, v]) => el('div', {}, el('dt', {}, k), el('dd', {}, v == null ? 'Not reported' : v))));}
  function field(label, input, help) {const id = input.id || `field-${Math.random().toString(36).slice(2)}`; input.id = id; return el('div', {class: 'field'}, el('label', {for: id}, label), input, help && el('small', {}, help));}
  function closeButton(dialog) {return button('Close', () => dialog.close(), 'icon-button', 'close');}
  function safePath(value, prefix) {try {const url = new URL(value, location.origin); return url.origin === location.origin && url.pathname.startsWith(prefix) ? url.pathname + url.search : null;} catch {return null;}}
  function safeTelegram(value) {try {const u = new URL(value); return u.protocol === 'https:' && u.hostname === 't.me' ? u.href : null;} catch {return null;}}

  async function api(path, options = {}) {
    const session = state.sessionSerial;
    const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 20000);
    const method = options.method || 'GET';
    const headers = {'Accept': 'application/json'};
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    if (method !== 'GET' && state.user?.csrf_token) headers['X-CSRF-Token'] = state.user.csrf_token;
    try {
      const response = await fetch(API + path, {method, credentials: 'same-origin', headers, signal: controller.signal, cache: 'no-store', ...(options.body !== undefined ? {body: JSON.stringify(options.body)} : {})});
      let data = null; if (response.status !== 204) {try {data = await response.json();} catch {if (response.ok) throw new Error('The server returned an unreadable response.');}}
      if (!response.ok) {
        const error = new Error(typeof data?.detail === 'string' ? data.detail : `Request failed (${response.status}).`); error.status = response.status;
        if (response.status === 401 && state.user && session === state.sessionSerial && !options.allowUnauthorized) expireSession();
        throw error;
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('The request timed out. Check the server connection and try again.');
      if (error instanceof TypeError) throw new Error('Cannot reach the server. Check your connection and try again.');
      throw error;
    } finally {clearTimeout(timeout);}
  }
  function stopStreams() {for (const entry of state.streams.values()) {clearTimeout(entry.timer); entry.img.removeAttribute('src');} state.streams.clear();}
  function stopUpdates() {state.eventSource?.close(); state.eventSource = null; clearInterval(state.poll); state.poll = null; stopStreams(); state.connected = false;}
  function expireSession() {history.replaceState(null, '', location.pathname); state.localAlerts = null; stopUpdates(); document.querySelectorAll('video').forEach(video => {video.pause(); video.removeAttribute('src'); video.load();}); state.user = null; state.telegramLink = null; state.cameras = []; state.filters = {status: '', camera: '', search: '', from_date: '', to_date: '', sort: 'newest', min_score: ''}; state.view = 'overview'; state.system = null; state.incidentOffset = 0; state.journalOffset = 0; state.journalFilters = {q: '', category: '', from_date: '', to_date: ''}; state.sessionSerial++; state.renderSerial++; state.detailId = null; state.detail = null; state.drafts.clear(); if (detailDialog.open) detailDialog.close(); if (actionDialog.open) actionDialog.close(); detailDialog.replaceChildren(); actionDialog.replaceChildren(); renderLogin('Your session has ended. Sign in again to continue.');}
  function connectionLabel(live) {state.connected = live; const node = document.getElementById('connection'); if (node) node.replaceChildren(el('span', {class: `dot ${live ? 'good' : 'warn'}`}), live ? 'Live updates connected' : 'Polling for updates');}
  function startUpdates() {
    stopUpdates();
    if ('EventSource' in window) {
      const source = new EventSource(API + '/events', {withCredentials: true}); state.eventSource = source;
      source.onopen = () => connectionLabel(true);
      source.onerror = () => connectionLabel(false);
      const invalidate = () => {if (!document.hidden && state.user) refresh(true);};
      source.onmessage = invalidate; source.addEventListener('invalidate', invalidate); source.addEventListener('revision', invalidate);
      source.addEventListener('auth-expired', expireSession);
    }
    state.poll = setInterval(() => {if (!document.hidden && state.user) refresh(true);}, 15000);
  }
  document.addEventListener('visibilitychange', () => {if (!document.hidden && state.user) refresh(true);});
  window.addEventListener('pagehide', stopUpdates);
  document.addEventListener('fullscreenchange', () => {if (!document.fullscreenElement && state.user) refresh(true);});

  function renderLogin(message = '') {
    if (state.auth.demo_mode === 'offline') {
      const feedback = el('p', {role: 'alert', class: 'form-error'}, message);
      const enter = button('Enter as Demo Director', async () => {
        enter.disabled = true;
        try {state.user = await api('/demo/enter', {method: 'POST', body: {}}); state.sessionSerial++; await enterWorkspace();}
        catch (e) {feedback.textContent = e.message; enter.disabled = false;}
      }, 'primary', 'arrow');
      app.replaceChildren(el('main', {class: 'login-form-region', id: 'main'}, logo(), el('span', {class: 'pill warn'}, 'Offline Demo Director Mode'), el('h1', {}, 'DormGuard AI'), el('p', {}, 'Analyze local video. Preserve evidence. Make a human decision.'), enter, feedback, el('p', {class: 'settings-help'}, 'Isolated localhost demonstration · prerecorded sources · real uncalibrated model scores. Telegram is unavailable.')));
      return;
    }
    app.replaceChildren(el('div', {class: 'login-layout'},
      el('aside', {class: 'login-brand'}, logo(), el('div', {class: 'login-message'}, el('div', {class: 'eyebrow'}, 'A safer place to call home'), el('h1', {}, 'See what matters.', el('br'), 'Respond with care.'), el('p', {}, 'A private safety workspace for dormitory leaders. Review suspected incidents, understand the context, and coordinate a human response.'), el('div', {class: 'login-principles'}, el('div', {class: 'login-principle'}, icon('shield'), 'Human judgment at every critical decision'), el('div', {class: 'login-principle'}, icon('lock'), 'Restricted access to cameras and evidence'), el('div', {class: 'login-principle'}, icon('clock'), 'An auditable record of every response'))), el('p', {class: 'login-brand-footer'}, 'DORMGUARD AI  /  AUTHORIZED PERSONNEL ONLY')),
      el('main', {id: 'main', class: 'login-form-region'}, state.auth.evaluation && el('div', {class: 'evaluation-label'}, el('span', {class: 'pill warn'}, 'Local evaluation environment')), el('h2', {}, 'Sign in to your workspace'), el('p', {}, 'Use your institution-provisioned account.'), loginForm(message), el('div', {class: 'login-access'}, el('strong', {}, 'Access is by invitation'), 'Accounts are provisioned for authorized administrators, Dormitory Directors and Deputy Directors for Educational Affairs. Contact your system administrator if you need access.'), el('p', {class: 'login-privacy'}, icon('lock'), 'Camera footage and incident records are confidential. Access and review actions are audited.'))));
  }
  function loginForm(message) {
    const email = el('input', {name: 'email', type: 'email', autocomplete: 'username', required: true, placeholder: 'you@college.edu', maxlength: 254});
    const password = el('input', {id: 'login-password', name: 'password', type: 'password', autocomplete: 'current-password', required: true, maxlength: 256});
    const reveal = button('Show password', () => {const visible = password.type === 'password'; password.type = visible ? 'text' : 'password'; reveal.textContent = visible ? 'Hide password' : 'Show password'; reveal.setAttribute('aria-pressed', String(visible));}, 'password-toggle');
    reveal.setAttribute('aria-controls', password.id); reveal.setAttribute('aria-pressed', 'false');
    const error = el('div', {class: 'form-error', role: 'alert', hidden: !message}, message);
    const submit = el('button', {type: 'submit', class: 'primary', disabled: state.auth.configured === false}, 'Sign in securely');
    const form = el('form', {onsubmit: async event => {
      event.preventDefault(); if (submit.disabled) return; error.hidden = true; submit.disabled = true; submit.textContent = 'Signing in…';
      try {await api('/auth/login', {method: 'POST', body: {email: email.value, password: password.value}, allowUnauthorized: true}); password.value = ''; state.user = await api('/auth/me'); state.sessionSerial++; await enterWorkspace();}
      catch (e) {error.textContent = e.message; error.hidden = false; submit.disabled = false; submit.textContent = 'Sign in securely';}
    }}, state.auth.configured === false && banner('Authentication needs configuration', 'The administrator must configure Supabase or explicitly enable local evaluation authentication before sign-in is available.', 'error'), field('Email address', email), el('div', {class: 'field'}, el('label', {for: password.id}, 'Password'), el('div', {class: 'password-control'}, password, reveal)), error, submit);
    return form;
  }
  function renderShell() {
    const nav = el('nav', {class: 'nav', 'aria-label': 'Main navigation'});
    for (const [view, label, glyph] of VIEWS) {const item = button(label, () => navigate(view), state.view === view ? 'active' : '', glyph); if (state.view === view) item.setAttribute('aria-current', 'page'); nav.append(item);}
    const sidebar = el('div', {class: 'sidebar', id: 'sidebar'}, nav);
    const menu = button('Open navigation', () => {const open = sidebar.classList.toggle('open'); menu.setAttribute('aria-expanded', String(open)); sidebar.inert = !open;}, 'icon-button mobile-menu', 'menu');
    menu.setAttribute('aria-controls', 'sidebar'); menu.setAttribute('aria-expanded', 'false');
    const account = el('details', {class: 'account-menu'}, el('summary', {'aria-label': 'Account menu'},
      el('span', {class: 'avatar', 'aria-hidden': 'true'}, accountName().slice(0, 1).toUpperCase()),
      el('div', {class: 'account-info'}, el('strong', {'data-account-name': ''}, accountName()), el('small', {}, roleName()))),
      el('div', {class: 'account-dropdown'}, el('strong', {'data-account-name': ''}, accountName()), el('p', {}, roleName()),
        button('Account settings', () => {account.open = false; navigate('settings');}), button('Sign out', logout, '', 'logout')));
    app.replaceChildren(el('div', {class: 'shell'},
      el('header', {class: 'app-header'}, menu, logo(), sidebar,
        el('div', {class: 'header-actions'}, button('Notifications', showLocalAlerts, 'icon-button notification-button', 'bell'), account,
          button('Sign out', logout, 'icon-button', 'logout'))),
      el('div', {class: 'workspace'}, state.auth.demo_mode && el('div', {class: 'demo-mode-banner', role: 'status'}, state.auth.demo_mode === 'fixture' ? 'Controlled Demo Fixture — Not Real AI Detection' : 'Prerecorded Demo — Real Model Inference', el('span', {}, 'Disposable local workspace · external delivery disabled')), el('div', {class: 'topbar'},
        el('div', {class: 'breadcrumb'}, el('span', {class: 'workspace-name'}, 'Safety workspace'), el('span', {class: 'workspace-name', 'aria-hidden': 'true'}, '/'), el('span', {id: 'breadcrumb-view'}, viewLabel())),
        el('div', {class: 'topbar-right'}, el('span', {class: 'workspace-badge'}, state.auth.evaluation ? 'LOCAL EVALUATION' : 'PRIVATE WORKSPACE'),
          el('span', {class: 'connection', id: 'connection', role: 'status'}, el('span', {class: 'dot warn'}), 'Connecting to updates'))),
        el('main', {id: 'main', class: 'content', tabindex: '-1'}))));
    document.addEventListener('keydown', closeMenuOnEscape);
    updateSidebarInert();
  }
  function updateSidebarInert() {const sidebar = document.getElementById('sidebar'); if (sidebar) sidebar.inert = matchMedia('(max-width: 700px)').matches && !sidebar.classList.contains('open');}
  matchMedia('(max-width: 700px)').addEventListener('change', updateSidebarInert);
  function closeMenuOnEscape(event) {if (event.key === 'Escape') {const wasOpen = document.getElementById('sidebar')?.classList.contains('open'); document.getElementById('sidebar')?.classList.remove('open'); if (wasOpen) document.querySelector('.mobile-menu')?.focus(); document.querySelector('.mobile-menu')?.setAttribute('aria-expanded', 'false'); updateSidebarInert();}}
  function viewLabel() {return VIEWS.find(v => v[0] === state.view)?.[1] || 'Evidence';}
  async function logout() {try {await api('/auth/logout', {method: 'POST', body: {}}); authChannel?.postMessage('logout'); expireSession(); renderLogin();} catch (e) {toast(`Sign-out could not be confirmed: ${e.message}`);}}
  async function updateLocalAlerts() {
    const session = state.sessionSerial;
    try {
      const data = await api('/alerts');
      if (!state.user || session !== state.sessionSerial) return;
      const previous = state.localAlerts;
      state.localAlerts = data;
      const bell = document.querySelector('.notification-button');
      if (bell) {bell.setAttribute('aria-label', `Notifications (${data.unread} unread)`); bell.querySelector('.alert-count')?.remove(); if (data.unread) bell.append(el('span', {class: 'alert-count'}, data.unread));}
      if (previous && items(data).some(row => !row.is_read && !items(previous).some(old => old.id === row.id))) toast('New potential physical conflict — open Notifications to review.');
    } catch {const bell = document.querySelector('.notification-button'); if (bell) bell.setAttribute('aria-label', 'Notifications unavailable');}
  }
  async function showLocalAlerts() {
    await updateLocalAlerts();
    const rows = items(state.localAlerts);
    showAction(el('div', {}, el('div', {class: 'dialog-header'}, el('h2', {id: 'action-title'}, 'Local notifications'), closeButton(actionDialog)), el('div', {class: 'dialog-body'}, el('p', {class: 'settings-help'}, 'Stored locally. Telegram delivery and human acknowledgment are separate.'), rows.length ? rows.map(row => el('article', {class: 'audit-event'}, el('strong', {}, 'Potential physical conflict · ' + row.camera_name), el('small', {}, date(row.detected_at)), el('div', {class: 'button-row'}, status(row.status), el('span', {class: 'pill'}, row.is_read ? 'Read' : 'Unread'), button('Review incident', async () => {actionDialog.close(); await openIncident(row.incident_id);}), !row.is_read && button('Mark read', async () => {try {await api('/alerts/' + encodeURIComponent(row.id) + '/read', {method: 'POST', body: {}}); await showLocalAlerts();} catch (e) {toast(e.message);}})))) : empty('bell', 'No local notifications', 'A real incident creates an alert for the demo director.'))));
  }
  function resetDemoDialog() {
    const feedback = el('p', {role: 'status'});
    const confirm = button('Confirm demo reset', async () => {
      confirm.disabled = true;
      try {await api('/demo/reset', {method: 'POST', body: {confirm: true}}); stopUpdates(); actionDialog.close(); expireSession(); renderLogin('Reset requested. Reconnecting to a fresh demo run…'); setTimeout(() => location.reload(), 6000);}
      catch (e) {feedback.textContent = e.message; confirm.disabled = false;}
    }, 'danger');
    showAction(el('div', {}, el('div', {class: 'dialog-header'}, el('h2', {id: 'action-title'}, 'Reset this demo run?'), closeButton(actionDialog)), el('div', {class: 'dialog-body'}, el('p', {}, 'The launcher will start a fresh run with zero incidents and unread alerts. Settings, original videos and model weights are preserved. Earlier demo evidence and audit history remain archived privately.'), feedback), el('div', {class: 'dialog-footer'}, button('Cancel', () => actionDialog.close()), confirm)));
  }
  async function navigate(view) {
    document.querySelectorAll('video').forEach(video => {video.pause(); video.removeAttribute('src'); video.load();});
    stopStreams(); state.entrySerial++; state.view = view; state.renderSerial++; history.replaceState(null, '', location.pathname + '#' + view);
    document.getElementById('sidebar')?.classList.remove('open'); document.querySelector('.mobile-menu')?.setAttribute('aria-expanded', 'false');
    updateSidebarInert();
    document.querySelectorAll('.nav button').forEach(b => {const active = b.textContent === viewLabel(); b.classList.toggle('active', active); if (active) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');});
    document.getElementById('breadcrumb-view').textContent = viewLabel(); await refresh(); document.getElementById('main').focus({preventScroll: true});
  }
  function heading(subtitle, action) {return el('div', {class: 'page-heading'}, el('div', {}, el('h1', {}, viewLabel()), el('p', {}, subtitle)), el('div', {class: 'heading-actions'}, action, button('Refresh', () => refresh(), '', 'refresh')));}
  function loading() {const main = document.getElementById('main'); main?.replaceChildren(heading('Loading your safety workspace…'), el('div', {class: 'metric-grid', 'aria-hidden': 'true'}, ...Array.from({length: 4}, () => el('div', {class: 'loading-metrics'}))), el('div', {class: 'panel loading-box', role: 'status', 'aria-label': 'Loading data'}, el('div', {class: 'loading-line'}), el('div', {class: 'loading-line'}), el('div', {class: 'loading-line'}))); main?.setAttribute('aria-busy', 'true');}
  async function refresh(background = false) {
    if (!state.user) return;
    if (state.busy) {state.refreshAgain = true; await state.refreshPromise; if (state.user && (state.completedSerial !== state.renderSerial || state.completedSession !== state.sessionSerial)) await refresh(background); return;}
    state.busy = true; let finishRefresh; state.refreshPromise = new Promise(resolve => {finishRefresh = resolve;}); const serial = state.renderSerial; const session = state.sessionSerial; const view = state.view;
    if (!background) {stopStreams(); loading();}
    try {
      const endpoints = {overview: ['/overview', '/cameras', '/system', '/integrations'], cameras: ['/cameras', '/system'], incidents: ['/incidents' + incidentQuery(), '/cameras'], analytics: ['/system', '/cameras'], health: ['/system', '/cameras'], journal: ['/audit' + journalQuery(), '/cameras'], evidence: ['/evidence'], settings: ['/system', '/integrations', '/cameras', '/audit?limit=10', '/notifications', ...(state.user.role === 'super_admin' ? ['/users'] : [])]};
      const results = await Promise.allSettled(endpoints[view].map(path => api(path)));
      if (!state.user || session !== state.sessionSerial || serial !== state.renderSerial) return;
      if (results[0].status === 'rejected') throw results[0].reason;
      const values = results.map(result => result.status === 'fulfilled' ? result.value : null);
      const partial = results.some(result => result.status === 'rejected');
      const camerasIndex = {overview: 1, cameras: 0, incidents: 1, analytics: 1, health: 1, journal: 1, settings: 2}[view]; if (camerasIndex !== undefined && values[camerasIndex]) state.cameras = items(values[camerasIndex]);
      const main = document.getElementById('main');
      {
        const systemIndex = {overview: 2, cameras: 1, settings: 0, analytics: 0, health: 0}[view];
        if (systemIndex !== undefined) state.system = values[systemIndex];
        if (background && document.fullscreenElement?.classList.contains('camera-preview')) {updateCameraHealth(); return;}
        const previews = background ? Array.from(new Set([...Array.from(state.streams.keys()).map(id => document.getElementById('preview-' + id)), ...main.querySelectorAll('.camera-preview[data-opening-stream]')])).filter(Boolean) : [];
        const renders = {overview: renderOverview, cameras: renderCameras, incidents: renderIncidents, analytics: renderAnalytics, journal: renderJournal, health: renderHealth, evidence: renderEvidence, settings: renderSettings};
        const focus = preserveFocus(main); const players = Array.from(main.querySelectorAll('video[data-evidence-id]'));
        main.replaceChildren(...renders[view](...values));
        for (const video of players) {const target = Array.from(main.querySelectorAll('[data-player-for]')).find(node => node.getAttribute('data-player-for') === video.getAttribute('data-evidence-id')); if (target) target.replaceChildren(video); else {video.pause(); video.removeAttribute('src'); video.load();}}
        for (const preview of previews) {const target = document.getElementById(preview.id); if (target) target.replaceWith(preview); else state.streams.get(preview.id.slice(8))?.stop();}
        updateCameraHealth();
        restoreFocus(main, focus);
        if (partial) main.prepend(banner('Some information is unavailable', 'One or more supporting services could not be loaded. Refresh to retry; unavailable information is not reported as healthy.', 'error'));
      }
      state.lastUpdated = new Date();
      await updateLocalAlerts();
      if (state.detailId && detailDialog.open) await refreshDetail();
    } catch (e) {
      if (!state.user || serial !== state.renderSerial || session !== state.sessionSerial) return;
      const main = document.getElementById('main');
      if (background && main.childElementCount) {document.getElementById('refresh-warning')?.remove(); const warning = banner('Updates are unavailable', 'Showing the last loaded observations. Refresh before relying on current monitoring status.', 'error'); warning.id = 'refresh-warning'; main.prepend(warning); toast(`Update failed: ${e.message}`);}
      else main.replaceChildren(heading('Your workspace data is temporarily unavailable.'), el('section', {class: 'panel error-box', role: 'alert'}, el('h2', {}, 'We could not load this view'), el('p', {}, e.message), button('Try again', () => refresh(), 'primary', 'refresh')));
    } finally {
      document.getElementById('main')?.removeAttribute('aria-busy'); if (serial === state.renderSerial && session === state.sessionSerial) {state.completedSerial = serial; state.completedSession = session;} state.busy = false; finishRefresh();
      if (state.refreshAgain && state.user) {state.refreshAgain = false; queueMicrotask(() => refresh(true));}
    }
  }
  function preserveFocus(root) {const active = document.activeElement; if (!active || !root.contains(active) || !active.id) return null; return {id: active.id, start: active.selectionStart, end: active.selectionEnd, value: active.value};}
  function restoreFocus(root, saved) {if (!saved) return; const node = root.querySelector(`#${CSS.escape(saved.id)}`); if (node && saved.value != null) node.value = saved.value; node?.focus({preventScroll: true}); if (node?.setSelectionRange && saved.start != null && node.type !== 'email' && node.type !== 'number') node.setSelectionRange(saved.start, saved.end);}
  function metric(label, value, note, glyph, warn = false) {return el('div', {class: 'metric'}, el('div', {class: 'metric-top'}, label, icon(glyph)), el('div', {class: `metric-value ${warn ? 'warn' : ''}`}, value ?? '—'), el('div', {class: 'metric-note'}, note));}
  function inferenceWorker(system) {return items(system?.workers).find(w => w.name === 'inference');}
  function freshCamera(c) {const last = c.health?.last_frame_at; return c.enabled !== false && typeof last === 'number' && Date.now() / 1000 - last < 10 && ['connected', 'active'].includes(c.health?.state);}
  function monitoring(cameras, system) {
    const enabled = cameras.filter(c => c.enabled !== false); const worker = inferenceWorker(system);
    const fresh = enabled.filter(freshCamera).length;
    if (!enabled.length) return ['Monitoring not configured', 'Register an authorized source to begin.', 'warn'];
    if (!worker || worker.stale || worker.state !== 'ready') return ['System degraded', 'Camera preview and AI readiness are reported separately.', 'warn'];
    if (!fresh) return ['Monitoring offline', 'No recent camera frames are available.', 'bad'];
    const analyzed = enabled.filter(c => {const cm = worker.metrics?.per_camera?.[c.id]; const last = Date.parse(cm?.last_inference_utc); return (cm?.analyzed_windows || 0) > 0 && Number.isFinite(last) && Date.now() - last < 20000 && !/unavailable|overloaded|failed|not_started|continuity/.test(cm?.analysis_state || 'not_started');}).length;
    if (fresh === enabled.length && analyzed === enabled.length) return ['Monitoring active', 'Fresh frames and recent analysis reported for every enabled source.', 'good'];
    return ['Partial monitoring', 'Some sources are disconnected, waiting for analysis, or degraded.', 'warn'];
  }
  function numberOrDash(value) {return typeof value === 'number' && Number.isFinite(value) ? value : '—';}
  function duration(value) {return typeof value === 'number' && Number.isFinite(value) ? (value < 1 ? Math.round(value * 1000) + ' ms' : value.toFixed(2) + ' s') : 'Not reported';}
  function renderOverview(data, camerasResponse, system, integrations, audit) {
    const cameras = items(camerasResponse); const recent = items(data.recent_incidents); const worker = inferenceWorker(system);
    const m = worker?.metrics || {}; const counts = Object.fromEntries(items(data.notifications).map(r => [r.state, r.count]));
    const configured = system?.configuration?.model_configured === true && system?.configuration?.model_rights_confirmed === true;
    const [label, detail, kind] = monitoring(cameras, system);
    const today = data.incidents_today ?? items(data.trends).find(r => (r.day || r.date) === new Date().toISOString().slice(0, 10))?.count;
    const todayStates = Object.fromEntries(items(data.today_states).map(r => [r.status, r.count]));
    const online = camerasResponse ? cameras.filter(freshCamera).length : null;
    const selected = cameras.find(c => c.id === state.spotlight) || cameras.find(freshCamera) || cameras[0];
    if (selected) state.spotlight = selected.id;
    const switcher = cameras.length > 1 ? el('select', {class: 'spotlight-select', 'aria-label': 'Spotlight camera', onchange: e => {stopStreams(); state.spotlight = e.target.value; refresh(true);}}, cameras.map(c => el('option', {value: c.id, selected: selected?.id === c.id}, c.name))) : button('All cameras', () => navigate('cameras'), '', 'arrow');
    const spotlight = panel('Camera spotlight', 'Protected viewing · opens only on request', selected ? cameraCard(selected) : empty('camera', 'A clear view starts here', 'Register an authorized camera. Live footage will only be visible to your institution’s designated officials.', isDirector() && button('Register a camera', () => cameraDialog(), 'primary', 'plus')), switcher);
    const activity = items(data.recent_activity || audit).filter(row => row.action?.startsWith('incident.') || row.action?.startsWith('notification.') || row.action === 'pipeline.persisted').slice(0, 4);
    const telegram = integrations?.telegram;
    return [
      el('div', {class: 'page-heading overview-heading'}, el('div', {}, el('div', {class: 'eyebrow'}, 'DORMITORY SAFETY / COMMAND CENTER'), el('h1', {}, 'Overview'), el('p', {}, state.user.display_name ? `Welcome back, ${state.user.display_name}.` : 'Real-time monitoring. Thoughtful human response.')), el('div', {class: 'heading-actions'}, button('Review incidents', () => navigate('incidents'), 'primary', 'arrow'), button('Refresh', () => refresh(), 'icon-button', 'refresh'))),
      el('div', {class: 'monitoring-strip'}, el('div', {class: 'monitoring-state'}, el('div', {class: 'status-orb'}, icon('shield')), el('div', {}, el('strong', {}, el('span', {class: 'dot ' + kind}), ' ' + label), el('p', {}, detail))), el('div', {class: 'monitoring-date'}, new Intl.DateTimeFormat(undefined, {weekday: 'long', month: 'long', day: 'numeric'}).format(new Date()), el('p', {}, 'Incident counts use UTC'))),
      el('div', {class: 'metric-grid'},
        metric('Active cameras', online, camerasResponse ? cameras.filter(c => c.enabled !== false).length + ' enabled · ' + cameras.filter(c => c.enabled !== false && !freshCamera(c)).length + ' without fresh frames' : 'Camera service unavailable', 'camera'),
        metric('Incidents today', numberOrDash(today), data.today_states ? (todayStates.DETECTED || 0) + ' new · ' + (todayStates.PENDING_REVIEW || 0) + ' in review · ' + ((todayStates.RESOLVED || 0) + (todayStates.CLOSED || 0)) + ' resolved' : 'UTC · status breakdown not reported', 'incidents', Number(today) > 0),
        metric('AI processing', worker && !worker.stale && worker.state === 'ready' ? 'Active' : 'Not ready', m.queue_depth != null ? m.queue_depth + ' queued · ' + (m.dropped ?? 0) + ' skipped · p50 ' + duration(m.timings?.series?.inference_seconds?.p50) : 'No recent inference measurements', 'activity', !worker || worker.stale || worker.state !== 'ready'),
        metric('Notification health', data.notifications ? ((counts.queued || 0) + (counts.sending || 0)) + ' pending' : '—', data.notifications ? (counts.accepted || 0) + ' API accepted · ' + ((counts.failed || 0) + (counts.blocked || 0)) + ' failed / blocked' : 'Delivery counts unavailable', 'bell')),
      !configured && banner('AI detection is not operational', 'A rights-cleared model and running inference worker are required. Preview and human review remain available.', '', button('AI status', () => navigate('analytics'), 'link-button')),
      telegram && !telegram.linked && banner('Connect Telegram for incident alerts', telegram.configured ? 'Create a private one-time link, then tap Start in Telegram. Your browser cannot authorize the bot for you.' : 'Telegram needs server configuration before officials can connect their accounts.', 'info', button('Set up alerts', () => navigate('settings'), 'link-button')),
      el('div', {class: 'grid-main'}, spotlight, el('div', {class: 'stack overview-side'},
        panel('Needs your attention', (data.open_incidents ?? '—') + ' open suspected events · human review required', recent.length ? el('div', {class: 'panel-body'}, recent.map(incidentBrief)) : empty('shield', 'No incidents recorded', 'Check monitoring coverage before relying on an empty incident list.'), button('View all', () => navigate('incidents'))),
        panel('Processing at a glance', 'Observed worker state', el('div', {class: 'panel-body'}, worker ? workerRow(worker) : el('p', {class: 'settings-help'}, 'No inference heartbeat reported. Monitoring readiness is unknown.')), button('AI Analytics', () => navigate('analytics'))))),
      el('section', {class: 'panel activity-overview'}, el('div', {class: 'panel-heading'}, el('div', {}, el('h2', {}, 'Incident activity'), el('p', {}, 'Real detection, dispatch and review events')), button('Activity journal', () => navigate('journal'), '', 'arrow')), activity.length ? el('div', {class: 'activity-rail'}, activity.map(row => {
        const id = row.detail?.correlation_id || (row.action.startsWith('incident.') ? row.resource_id : null);
        return el('div', {class: 'activity-node ' + (row.action === 'notification.accepted' ? 'event-accepted' : /failed|blocked/.test(row.action) ? 'event-failed' : row.actor_id ? 'event-reviewed' : '')}, el('small', {}, date(row.created_at, true)), id ? button(title(row.action.replaceAll('.', ' ')), () => openIncident(id), 'row-link') : el('strong', {}, title(row.action.replaceAll('.', ' '))), el('p', {}, row.action === 'notification.accepted' ? 'Telegram API accepted · device receipt unverified' : row.actor_id ? 'Authorized official action' : 'Recorded by the system'));
      })) : empty('clock', 'The response timeline starts with a real event', 'Detection, dispatch, provider acceptance and human response will appear as they are recorded.')),
      el('p', {class: 'footnote'}, icon('shield'), 'AI signals prompt a review. They do not establish violence or misconduct. Telegram API acceptance does not establish human receipt.')
    ];
  }
  function incidentBrief(i) {
    return el('article', {class: 'incident-brief'}, el('span', {class: 'incident-marker' + (i.status === 'CONFIRMED' ? ' critical' : '')}), el('div', {class: 'incident-info'},
      button('Suspected physical altercation', () => openIncident(i.id), 'row-link'), el('small', {}, i.camera_name || cameraName(i.camera_id)), el('small', {}, date(i.detected_at, true) + ' · signal ' + score(i.score)), el('small', {}, i.evidence_count == null ? 'Evidence availability not reported' : i.evidence_count + ' evidence clips')), status(i.status));
  }
  function trendChart(trends) {
    if (!trends.length) return empty('activity', 'Activity will appear here', 'There is no server-reported daily activity in this period.');
    const rows = trends.slice(-14); const max = Math.max(1, ...rows.map(r => Number(r.count) || 0));
    const chart = el('div', {class: 'panel-body'}, el('div', {class: 'trends', role: 'img', 'aria-label': rows.map(r => `${r.date || r.day}: ${r.count}`).join(', ')}, rows.map(row => {
      const track = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); track.setAttribute('viewBox', '0 0 24 100'); track.setAttribute('class', 'trend-track'); track.setAttribute('preserveAspectRatio', 'none'); track.setAttribute('aria-hidden', 'true');
      const rect = document.createElementNS(track.namespaceURI, 'rect'); const height = Math.max(0, (Number(row.count) || 0) / max * 90); rect.setAttribute('x', '2'); rect.setAttribute('y', String(100 - height)); rect.setAttribute('width', '20'); rect.setAttribute('height', String(height)); rect.setAttribute('rx', '2'); rect.setAttribute('fill', '#e7b34c'); track.append(rect);
      return el('div', {class: 'trend-column'}, el('span', {class: 'trend-count'}, row.count), track, el('small', {}, String(row.date || row.day).slice(5)));
    })));
    return chart;
  }
  function cameraHealthRow(c) {return el('div', {class: 'camera-health-row'}, el('span', {class: 'camera-icon'}, icon('camera')), el('div', {class: 'health-info'}, el('strong', {}, c.name), el('small', {}, c.location || 'Location not set')), health(c.enabled === false ? 'disabled' : c.health?.state));}
  function workerRow(w) {
    const m = w.metrics || {}; const timing = m.timings?.series?.inference_seconds;
    const observed = Number.isFinite(timing?.p50) ? `Inference median ${timing.p50.toFixed(3)}s (${timing.count} recent windows)` : '';
    return el('div', {class: 'health-row'}, el('div', {class: 'health-info'}, el('strong', {}, title(w.name || w.kind || w.worker || 'Worker')), el('small', {}, w.detail ? title(w.detail) : (w.last_seen || w.updated_at ? `Last reported ${date(w.last_seen || w.updated_at, true)}` : 'Heartbeat details unavailable')), observed && el('small', {}, observed), m.queue_depth != null && el('small', {}, `Queued windows: ${m.queue_depth} · Skipped: ${m.dropped ?? 0} · Errors: ${m.errors ?? 0}`)), health(w.stale ? 'stale' : w.state || w.status));
  }
  function incidentTable(rows) {
    const head = el('thead', {}, el('tr', {}, el('th', {scope: 'col'}, 'Incident / camera'), el('th', {scope: 'col'}, 'Status'), el('th', {scope: 'col'}, 'Detected'), el('th', {scope: 'col', class: 'mobile-hide'}, 'Signal')));
    const body = el('tbody', {}, rows.map(i => el('tr', {},
      el('td', {}, button(i.type ? title(i.type) : 'Suspected physical altercation', () => openIncident(i.id), 'row-link'), el('small', {}, `${i.camera_name || cameraName(i.camera_id)} · ${i.location || `#${String(i.id).slice(0, 8)}`}`)),
      el('td', {}, status(i.status)),
      el('td', {}, el('span', {}, date(i.detected_at || i.created_at, true))),
      el('td', {class: 'mobile-hide'}, el('span', {title: 'Uncalibrated model suspicion score'}, score(i.score)))
    )));
    return el('div', {class: 'table-wrap'}, el('table', {}, head, body));
  }
  function score(value) {const n = Number(value); return value != null && Number.isFinite(n) ? n.toFixed(3) : 'Not reported';}
  function cameraName(id) {return state.cameras.find(c => c.id === id)?.name || `Camera ${String(id || 'unknown').slice(0, 8)}`;}
  function incidentQuery() {
    const query = new URLSearchParams({limit: '25', offset: String(state.incidentOffset), sort: state.filters.sort || 'newest'});
    for (const [key, value] of Object.entries({status: state.filters.status, camera_id: state.filters.camera, q: state.filters.search, from_date: state.filters.from_date, to_date: state.filters.to_date, min_score: state.filters.min_score})) if (value !== '') query.set(key, value);
    return '?' + query;
  }
  function pagination(data, onPage) {
    const offset = data?.offset || 0; const limit = data?.limit || items(data).length || 25; const total = data?.total;
    const previous = button('Previous', () => onPage(Math.max(0, offset - limit))); previous.disabled = offset === 0;
    const next = button('Next', () => onPage(data.next_offset)); next.disabled = data?.next_offset == null;
    return el('div', {class: 'pagination'}, el('span', {}, total == null ? 'Latest server results · total not reported' : total ? (offset + 1) + '–' + Math.min(offset + items(data).length, total) + ' of ' + total : '0 results'), el('div', {class: 'button-row'}, previous, next));
  }
  function renderIncidents(data) {
    const update = (key, value) => {state.filters[key] = value; state.incidentOffset = 0; refresh();};
    const statusSelect = el('select', {id: 'status-filter', onchange: e => update('status', e.target.value)}, el('option', {value: ''}, 'All statuses'), Object.entries(STATUS_LABELS).map(([value, label]) => el('option', {value, selected: state.filters.status === value}, label)));
    const cameraSelect = el('select', {id: 'camera-filter', onchange: e => update('camera', e.target.value)}, el('option', {value: ''}, 'All cameras'), state.cameras.map(c => el('option', {value: c.id, selected: state.filters.camera === c.id}, c.name)));
    const search = el('input', {id: 'incident-search', type: 'search', placeholder: 'Camera, location or incident ID', value: state.filters.search, 'aria-label': 'Search incidents', maxlength: 200});
    const from = el('input', {id: 'incident-from', type: 'date', value: state.filters.from_date});
    const to = el('input', {id: 'incident-to', type: 'date', value: state.filters.to_date});
    const sort = el('select', {id: 'incident-sort', onchange: e => update('sort', e.target.value)}, [['newest', 'Newest first'], ['oldest', 'Oldest first'], ['score', 'Highest signal first']].map(([value, label]) => el('option', {value, selected: value === state.filters.sort}, label)));
    const signal = el('select', {id: 'incident-signal', onchange: e => update('min_score', e.target.value)}, [['', 'All signals'], ['0.7', 'Signal ≥ 0.7'], ['0.9', 'Signal ≥ 0.9']].map(([value, label]) => el('option', {value, selected: value === state.filters.min_score}, label)));
    const form = el('form', {class: 'filters', onsubmit: e => {e.preventDefault(); state.filters.search = search.value; state.filters.from_date = from.value; state.filters.to_date = to.value; state.incidentOffset = 0; refresh();}},
      el('div', {class: 'filter-search'}, search), el('label', {}, 'Status', statusSelect), el('label', {}, 'Camera', cameraSelect), el('label', {}, 'From · UTC', from), el('label', {}, 'Through · UTC', to), el('label', {}, 'Sort', sort), el('label', {}, 'Uncalibrated signal', signal), el('button', {type: 'submit'}, 'Apply filters'));
    return [heading('A considered response starts with the full context.'), form,
      el('section', {class: 'panel', 'aria-label': 'Incident results'}, el('div', {class: 'panel-heading'}, el('h2', {}, 'Incident journal'), button('Evidence library', () => navigate('evidence'), '', 'evidence')), items(data).length ? incidentTable(items(data)) : empty('incidents', 'No incidents match this view', 'Try another date, camera, status or search. Dates use UTC.'), pagination(data, offset => {state.incidentOffset = offset; refresh();})),
      el('p', {class: 'footnote'}, icon('shield'), 'Scores are uncalibrated suspicion signals. No reviewer assignment is inferred from viewing an incident.')];
  }
  function renderCameras(response, system) {
    const cameras = items(response); const selected = cameras.find(c => c.id === state.selectedCamera) || cameras[0];
    const choose = el('select', {class: 'spotlight-select', 'aria-label': 'Select camera', onchange: e => {state.selectedCamera = e.target.value; if (state.cameraLayout === 'single') refresh();}}, cameras.map(c => el('option', {value: c.id, selected: c.id === selected?.id}, c.name)));
    return [heading('A private view of the places in your care.', isDirector() && button('Register camera', () => cameraDialog(), 'primary', 'plus')),
      !system?.configuration?.model_configured && banner('Live viewing and AI detection have separate readiness', 'Test sources are explicitly labeled. A visible camera does not establish active AI monitoring.'),
      el('div', {class: 'camera-controls'}, el('div', {class: 'segmented', role: 'group', 'aria-label': 'Camera layout'}, ['single', 'grid'].map(layout => button(layout === 'single' ? 'Single camera' : '2 × 2 grid', () => {state.cameraLayout = layout; refresh();}, state.cameraLayout === layout ? 'active' : ''))), cameras.length ? choose : null),
      cameras.length ? el('div', {class: 'camera-grid' + (state.cameraLayout === 'single' ? ' single' : '')}, (state.cameraLayout === 'single' ? [selected] : cameras).map(cameraCard)) : el('div', {class: 'panel'}, empty('camera', 'Connect your first camera', isDirector() ? 'Register an RTSP camera or an approved prerecorded test source.' : 'A Dormitory Director can register sources for this workspace.', isDirector() && button('Register camera', () => cameraDialog(), 'primary', 'plus')))];
  }
  function cameraCard(camera) {
    const preview = el('div', {class: 'camera-preview', id: `preview-${camera.id}`});
    preview.replaceChildren(previewPlaceholder(camera, preview));
    return el('article', {class: 'camera-card'}, preview, el('div', {class: 'camera-card-body'}, el('div', {class: 'camera-card-title'}, el('h3', {}, camera.name), el('span', {id: `camera-health-${camera.id}`}, health(camera.enabled === false ? 'disabled' : camera.health?.state))), el('p', {}, camera.location || 'Location not set'), el('div', {class: 'camera-details'}, el('span', {id: 'frame-time-' + camera.id}, 'Last frame: ' + date(camera.health?.last_frame_at, true)), el('span', {id: 'frame-size-' + camera.id}, 'Resolution not reported'), el('span', {}, 'AI: ' + title(inferenceWorker(state.system)?.metrics?.per_camera?.[camera.id]?.analysis_state || 'not reported')), el('span', {}, 'Suspicion score: ' + (inferenceWorker(state.system)?.metrics?.per_camera?.[camera.id]?.last_suspicion_score != null ? (100 * inferenceWorker(state.system).metrics.per_camera[camera.id].last_suspicion_score).toFixed(1) + '% · uncalibrated' : 'Awaiting inference')), el('span', {}, 'Source FPS: ' + (inferenceWorker(state.system)?.metrics?.per_camera?.[camera.id]?.source_fps?.toFixed(1) || 'Awaiting decoder')), el('span', {}, 'Inference: ' + (inferenceWorker(state.system)?.metrics?.per_camera?.[camera.id]?.last_inference_seconds != null ? (1000 * inferenceWorker(state.system).metrics.per_camera[camera.id].last_inference_seconds).toFixed(0) + ' ms' : 'Awaiting inference')), el('span', {}, 'Temporal positives: ' + (inferenceWorker(state.system)?.metrics?.per_camera?.[camera.id]?.positive_observations ?? 0) + ' / ' + (inferenceWorker(state.system)?.metrics?.per_camera?.[camera.id]?.required_confirmations ?? 2))), el('div', {class: 'camera-card-meta'}, el('span', {}, camera.kind === 'test' ? 'Prerecorded test source' : 'RTSP camera'), state.auth.demo_mode === 'offline' && camera.health?.state !== 'completed' && button(camera.enabled ? 'Stop analysis' : 'Start analysis', async () => {try {await api('/cameras/' + encodeURIComponent(camera.id), {method: 'PATCH', body: {enabled: !camera.enabled}}); await refresh();} catch (e) {toast(e.message);}}, 'primary'), state.auth.demo_mode === 'offline' && camera.health?.state === 'completed' && el('span', {class: 'pill'}, 'Playback finished · reset demo to repeat'), isDirector() && button('Manage', () => cameraDialog(camera), 'link-button'))));
  }
  function previewPlaceholder(camera, preview, message) {return el('div', {class: 'preview-placeholder'}, icon('camera'), el('p', {}, message || (camera.enabled === false ? 'Camera is disabled' : ['disconnected', 'offline', 'stale', 'blocked', 'completed'].includes(camera.health?.state) ? 'Camera unavailable · retry to request a fresh frame' : 'Protected preview · opens on request')), camera.enabled !== false && button(message ? 'Retry preview' : 'Open live preview', () => openStream(camera, preview)));}
  async function openStream(camera, preview) {
    const session = state.sessionSerial; const view = state.view; preview.dataset.openingStream = 'true';
    preview.replaceChildren(el('div', {class: 'preview-placeholder', role: 'status'}, icon('camera'), el('p', {}, 'Authorizing camera access…')));
    try {
      const ticket = await api(`/cameras/${encodeURIComponent(camera.id)}/stream-session`, {method: 'POST', body: {}});
      if (!state.user || session !== state.sessionSerial || view !== state.view || !preview.isConnected) return;
      const url = safePath(ticket.url, '/api/v1/streams/'); if (!url) throw new Error('The server returned an invalid protected stream URL.');
      const expiry = typeof ticket.expires_at === 'number' ? ticket.expires_at * 1000 : Date.parse(ticket.expires_at); if (!Number.isFinite(expiry) || expiry <= Date.now()) throw new Error('The stream authorization has expired.');
      const previous = state.streams.get(camera.id); if (previous) {previous.img.removeAttribute('src'); clearTimeout(previous.timer);}
      const img = el('img', {alt: `${camera.name} ${camera.kind === 'test' ? 'prerecorded test preview' : 'live preview'}`});
      const stop = message => {if (state.streams.get(camera.id)?.img !== img) return; img.removeAttribute('src'); const entry = state.streams.get(camera.id); if (entry) clearTimeout(entry.timer); state.streams.delete(camera.id); if (preview.isConnected) preview.replaceChildren(previewPlaceholder(camera, preview, message));};
      img.addEventListener('error', () => stop('Camera unavailable or authorization expired.'));
      img.addEventListener('load', () => {const node = document.getElementById('frame-size-' + camera.id); if (node && img.naturalWidth) node.textContent = 'Preview ' + img.naturalWidth + ' × ' + img.naturalHeight;});
      const fullscreen = button('Fullscreen camera', async () => {try {if (document.fullscreenElement) await document.exitFullscreen(); else if (preview.requestFullscreen) await preview.requestFullscreen(); else toast('Fullscreen is unavailable in this browser.');} catch {toast('Fullscreen could not be opened. Try the browser display controls.');}}, 'icon-button preview-fullscreen', 'fullscreen');
      preview.replaceChildren(img, fullscreen, el('span', {class: 'preview-label'}, el('span', {class: 'dot good'}), camera.kind === 'test' ? 'TEST SOURCE' : 'LIVE PREVIEW'), button('Stop preview', () => stop(), 'preview-stop'));
      const timer = setTimeout(() => stop('Preview authorization expired.'), Math.min(expiry - Date.now(), 2147483647));
      state.streams.set(camera.id, {img, timer, stop}); img.src = url;
    } catch (e) {if (preview.isConnected && state.user) preview.replaceChildren(previewPlaceholder(camera, preview, e.message));} finally {delete preview.dataset.openingStream;}
  }
  function updateCameraHealth() {for (const camera of state.cameras) {
    document.getElementById(`camera-health-${camera.id}`)?.replaceChildren(health(camera.enabled === false ? 'disabled' : camera.health?.state));
    const frameTime = document.getElementById('frame-time-' + camera.id); if (frameTime) frameTime.textContent = 'Last frame: ' + date(camera.health?.last_frame_at, true);
    const last = camera.health?.last_frame_at; const stale = typeof last === 'number' && Date.now() / 1000 - last > 10;
    if (camera.enabled === false || stale || ['disconnected', 'offline', 'stale', 'blocked', 'completed'].includes(camera.health?.state)) state.streams.get(camera.id)?.stop(stale ? 'Recent frame is stale. Camera unavailable.' : 'Camera unavailable.');
  }}
  function cameraDialog(camera = null) {
    const name = el('input', {name: 'name', required: true, maxlength: 100, value: camera?.name || ''});
    const locationInput = el('input', {name: 'location', required: true, maxlength: 200, value: camera?.location || ''});
    const kind = el('select', {name: 'kind', disabled: Boolean(camera)}, el('option', {value: 'rtsp', selected: camera?.kind !== 'test'}, 'RTSP camera'), el('option', {value: 'test', selected: camera?.kind === 'test'}, 'Prerecorded test source'));
    const source = el('input', {name: 'source_ref', required: !camera, placeholder: 'DORMGUARD_CAMERA_HALL', pattern: '[A-Z][A-Z0-9_]{2,99}', maxlength: 100});
    const enabled = el('input', {type: 'checkbox', name: 'enabled', checked: camera?.enabled !== false});
    const error = el('div', {class: 'form-error', role: 'alert', hidden: true}); const submit = el('button', {type: 'submit', class: 'primary'}, camera ? 'Save changes' : 'Register camera');
    const form = el('form', {onsubmit: async e => {e.preventDefault(); submit.disabled = true; error.hidden = true; try {const body = {name: name.value.trim(), location: locationInput.value.trim()}; if (camera) body.enabled = enabled.checked; else {body.kind = kind.value; body.source_ref = source.value.trim();} await api(camera ? `/cameras/${encodeURIComponent(camera.id)}` : '/cameras', {method: camera ? 'PATCH' : 'POST', body}); actionDialog.close(); toast(camera ? 'Camera updated.' : 'Camera registered. Worker connectivity will appear in camera health.'); refresh();} catch (err) {error.textContent = err.message; error.hidden = false;} finally {submit.disabled = false;}}}, el('div', {class: 'dialog-header'}, el('div', {}, el('h2', {id: 'action-title'}, camera ? 'Manage camera' : 'Register camera'), el('p', {}, 'Connection credentials remain on the server.')), closeButton(actionDialog)), el('div', {class: 'dialog-body'}, field('Camera name', name), field('Location', locationInput), field('Source type', kind), !camera && field('Server environment reference', source, 'Ask your administrator to configure this environment variable privately. Enter its name here, never an RTSP URL or a video file path.'), camera && el('label', {class: 'button-row'}, enabled, 'Camera enabled'), camera && el('p', {class: 'settings-help'}, camera.health?.detail || 'Worker health reports whether this source can be ingested.'), error), el('div', {class: 'dialog-footer'}, button('Cancel', () => actionDialog.close()), submit));
    showAction(form);
  }
  function showAction(content) {if (actionDialog.open) actionDialog.close(); actionDialog.replaceChildren(content); actionDialog.showModal();}
  async function openIncident(id) {
    state.detailId = id; state.detail = null; history.replaceState(null, '', location.pathname + '#incidents/' + encodeURIComponent(id));
    detailDialog.replaceChildren(el('div', {class: 'dialog-header'}, el('h2', {id: 'detail-title'}, 'Loading incident…'), closeButton(detailDialog)), el('div', {class: 'dialog-body', role: 'status'}, el('div', {class: 'loading-line'}), el('div', {class: 'loading-line'})));
    if (!detailDialog.open) detailDialog.showModal(); await refreshDetail();
    if (state.user && state.detail?.id === id) {try {await api(`/incidents/${encodeURIComponent(id)}/opened`, {method: 'POST', body: {}});} catch (e) {toast(`Incident access audit could not be recorded: ${e.message}`);}}
  }
  detailDialog.addEventListener('close', () => {if (detailDialog.open) return; if (requestedTarget()?.id) history.replaceState(null, '', location.pathname + '#' + state.view); state.detailId = null; state.detail = null; for (const v of detailDialog.querySelectorAll('video')) {v.pause(); v.removeAttribute('src'); v.load();}});
  async function refreshDetail() {
    const id = state.detailId; const session = state.sessionSerial; if (!id) return;
    try {const detail = await api(`/incidents/${encodeURIComponent(id)}`); if (state.user && session === state.sessionSerial && state.detailId === id && detailDialog.open) {state.detail = detail; renderDetail(detail);}}
    catch (e) {if (!state.user || state.detailId !== id) return; if (state.detail) toast(`Incident update failed: ${e.message}`); else detailDialog.replaceChildren(el('div', {class: 'dialog-header'}, el('h2', {id: 'detail-title'}, 'Incident unavailable'), closeButton(detailDialog)), el('div', {class: 'dialog-body'}, banner('Could not load incident', e.message, 'error'), button('Retry', refreshDetail, 'primary')));}
  }
  function renderDetail(i) {
    const focus = preserveFocus(detailDialog); const currentVideo = detailDialog.querySelector('video');
    const verificationNodes = Array.from(detailDialog.querySelectorAll('[data-evidence-verify], [data-evidence-feedback]'));
    const note = el('textarea', {id: 'incident-note', name: 'note', maxlength: 2000, placeholder: 'Add context for the next reviewer…', oninput: e => state.drafts.set(i.id, e.target.value)}); note.value = state.drafts.get(i.id) || '';
    const noteError = el('div', {class: 'form-error', role: 'alert', hidden: true});
    const saveNote = el('button', {type: 'submit'}, 'Add note');
    const noteForm = el('form', {onsubmit: async event => {event.preventDefault(); if (!note.value.trim()) {note.focus(); return;} saveNote.disabled = true; try {await api(`/incidents/${encodeURIComponent(i.id)}/notes`, {method: 'POST', body: {reason: note.value.trim()}}); state.drafts.delete(i.id); toast('Review note recorded.'); await refreshDetail();} catch (e) {noteError.textContent = e.message; noteError.hidden = false;} finally {saveNote.disabled = false;}}}, field('Reviewer note', note, 'Saved notes become part of the incident history.'), noteError, saveNote);
    const evidenceSection = el('section', {class: 'detail-section', id: 'detail-evidence'}, el('h3', {}, 'Evidence'), items(i.evidence).length ? items(i.evidence).map(e => evidenceItem(e, true)) : el('p', {class: 'detail-description'}, 'No evidence clips are available for this incident. Evidence may still be capturing, unavailable, or not configured.'));
    const actions = ACTIONS[String(i.status).toUpperCase()] || [];
    const main = el('div', {class: 'detail-grid'}, el('div', {}, el('div', {class: 'detail-summary'}, status(i.status), el('span', {class: 'pill outline'}, i.camera_name || cameraName(i.camera_id))), el('p', {class: 'detail-description'}, `${i.location || 'Location not reported'} · ${date(i.detected_at || i.created_at)}`), el('div', {class: 'detail-warning'}, 'Model scores are uncalibrated suspicion signals. Review the footage and surrounding context; no disciplinary decision is made automatically.'), evidenceSection, el('section', {class: 'detail-section'}, el('h3', {}, 'Incident timeline'), timeline(items(i.events))), el('section', {class: 'detail-section'}, el('h3', {}, 'Notification attempts · your account'), notificationList(items(i.notifications))), el('section', {class: 'detail-section'}, el('h3', {}, 'Incident audit history'), items(i.audit_events).length ? el('div', {}, items(i.audit_events).slice(0, 15).map(row => el('div', {class: 'audit-event'}, el('strong', {}, title(row.action?.replaceAll('.', ' '))), el('small', {}, date(row.created_at)), el('details', {}, el('summary', {}, 'Recorded observation'), el('pre', {class: 'mono'}, JSON.stringify(row.detail || {}, null, 2)))))) : el('p', {class: 'detail-description'}, 'No audit observations returned.'))), el('aside', {class: 'detail-aside'}, el('section', {class: 'detail-section'}, el('h3', {}, 'Detection information'), definition({'Incident ID': el('span', {class: 'mono'}, i.id), 'Model': typeof i.model === 'object' ? JSON.stringify(i.model) : (i.model || 'Not reported'), 'Suspicion score': score(i.score), 'Detected': date(i.detected_at || i.created_at)})), el('section', {class: 'detail-section'}, el('h3', {}, 'Human review'), actions.length ? el('div', {class: 'button-row'}, actions.map(action => button(ACTION_LABELS[action], () => transitionDialog(i, action), action === 'false-positive' ? 'danger' : action === actions[0] ? 'primary' : ''))) : el('p', {class: 'detail-description'}, 'This incident has no further state transitions.'), el('div', {class: 'button-row'}, button('View camera', async () => {detailDialog.close(); await navigate('cameras'); const camera = state.cameras.find(c => c.id === i.camera_id); const preview = document.getElementById(`preview-${i.camera_id}`); if (camera && preview) await openStream(camera, preview);}, '', 'camera'))), noteForm));
    const oldVideoId = currentVideo?.getAttribute('data-evidence-id');
    detailDialog.replaceChildren(el('div', {class: 'dialog-header'}, el('div', {}, el('div', {class: 'eyebrow'}, 'Incident review'), el('h2', {id: 'detail-title'}, i.type ? title(i.type) : 'Suspected physical altercation'), el('p', {}, 'Human review and every status change are audited.')), closeButton(detailDialog)), el('div', {class: 'dialog-body'}, main));
    if (currentVideo && oldVideoId) {const replacement = Array.from(detailDialog.querySelectorAll('[data-player-for]')).find(n => n.getAttribute('data-player-for') === oldVideoId); if (replacement) replacement.replaceChildren(currentVideo); else {currentVideo.pause(); currentVideo.removeAttribute('src'); currentVideo.load();}}
    for (const node of verificationNodes) {const attribute = node.hasAttribute('data-evidence-verify') ? 'data-evidence-verify' : 'data-evidence-feedback'; const replacement = Array.from(detailDialog.querySelectorAll(`[${attribute}]`)).find(n => n.getAttribute(attribute) === node.getAttribute(attribute)); if (replacement) replacement.replaceWith(node);}
    restoreFocus(detailDialog, focus);
  }
  function timeline(events) {if (!events.length) return el('p', {class: 'detail-description'}, 'No timeline events were returned.'); return el('ol', {class: 'timeline'}, events.map(e => el('li', {}, el('strong', {}, e.new_state ? `${title(e.kind)} · ${STATUS_LABELS[e.new_state] || title(e.new_state)}` : title(e.kind)), e.reason && el('p', {}, e.reason), el('small', {}, `${date(e.created_at)} · ${e.actor_id ? `Actor ${String(e.actor_id).slice(0, 12)}` : 'System'}`))));}
  function transitionDialog(incident, action) {
    const reason = el('textarea', {name: 'reason', id: 'transition-reason', required: true, minlength: 3, maxlength: 2000, placeholder: action === 'false-positive' ? 'Explain why the detection should be dismissed…' : 'Record the context and reason for this action…'});
    const error = el('div', {class: 'form-error', role: 'alert', hidden: true}); const submit = el('button', {type: 'submit', class: action === 'false-positive' ? 'danger' : 'primary'}, ACTION_LABELS[action]);
    showAction(el('form', {onsubmit: async e => {e.preventDefault(); submit.disabled = true; try {await api(`/incidents/${encodeURIComponent(incident.id)}/${action}`, {method: 'POST', body: {reason: reason.value.trim()}}); actionDialog.close(); toast('Incident history updated.'); await refreshDetail(); refresh(true);} catch (err) {error.textContent = err.message; error.hidden = false;} finally {submit.disabled = false;}}}, el('div', {class: 'dialog-header'}, el('h2', {id: 'action-title'}, ACTION_LABELS[action]), closeButton(actionDialog)), el('div', {class: 'dialog-body'}, el('p', {class: 'settings-help'}, 'This action records your identity, the previous and new states, and your reason in the incident timeline.'), field('Reason for this action', reason, 'Use observed facts and context. Avoid unnecessary personal information.'), error), el('div', {class: 'dialog-footer'}, button('Cancel', () => actionDialog.close()), submit)));
  }
  function renderEvidence(data) {
    const rows = items(data);
    const body = rows.length ? el('div', {class: 'settings-grid'}, rows.map(e => el('section', {class: 'panel panel-body'}, evidenceItem(e)))) : el('div', {class: 'panel'}, empty('evidence', 'No evidence clips available', 'Clips associated with suspected incidents will appear here after the camera worker captures and stores them.'));
    return [heading('Review restricted clips and verify their recorded SHA-256 integrity.'), banner('Evidence requires careful handling', 'Original clips and hashes are retained by the server. Downloads contain sensitive footage; follow your institution’s access and retention policies.', 'info'), body];
  }
  function evidenceItem(e, embedded = false) {
    const feedback = el('p', {class: 'settings-help', role: 'status', 'data-evidence-feedback': e.id}); const player = el('div', {'data-player-for': e.id});
    const verify = button('Verify integrity', async () => {verify.disabled = true; feedback.textContent = 'Checking the stored original against its recorded hash…'; try {const result = await api(`/evidence/${encodeURIComponent(e.id)}/verify`, {method: 'POST', body: {}}); feedback.textContent = result.valid ? 'PASS · Stored original matches its recorded SHA-256 hash.' : 'FAIL · Evidence integrity did not match. Escalate before relying on this clip.'; feedback.className = result.valid ? 'form-success' : 'form-error';} catch (err) {feedback.textContent = err.message; feedback.className = 'form-error';} finally {verify.disabled = false;}});
    verify.setAttribute('data-evidence-verify', e.id);
    const play = button('Play clip', () => {const video = el('video', {class: 'video-player', controls: true, preload: 'metadata', playsinline: true, 'data-evidence-id': e.id}); video.src = API + `/evidence/${encodeURIComponent(e.id)}/video`; video.addEventListener('error', () => {const currentFeedback = video.closest('article')?.querySelector('[data-evidence-feedback]'); if (video.isConnected && currentFeedback) {currentFeedback.textContent = 'Playback is unavailable. The authorized derivative may not be ready or your browser may not support its format.'; currentFeedback.className = 'form-error';}}); player.replaceChildren(video);}, '', 'evidence');
    const download = el('a', {class: 'primary-link', href: API + `/evidence/${encodeURIComponent(e.id)}/download`, download: ''}, icon('download'), ' Download original');
    return el('article', {class: embedded ? 'evidence-item' : ''}, el('h3', {}, `Evidence ${String(e.id).slice(0, 12)}`), el('p', {class: 'settings-help'}, `${bytes(e.size_bytes)} · ${date(e.clip_start || e.created_at, true)}${e.clip_end ? ' – ' + date(e.clip_end, true) : ''}`), !embedded && e.incident_id && button(`Open incident ${String(e.incident_id).slice(0, 8)}`, () => openIncident(e.incident_id), 'link-button'), el('div', {class: 'mono'}, `SHA-256: ${e.sha256 || 'Not reported'}`), el('p', {class: 'settings-help'}, e.retain_until ? `Retention until ${date(e.retain_until)}` : 'Automatic retention is not active. Preserve this evidence until an authorized policy applies.'), player, el('div', {class: 'button-row'}, play, verify, download), feedback);
  }
  function renderAnalytics(system, camerasResponse) {
    const worker = inferenceWorker(system); const m = worker?.metrics || {}; const config = system?.configuration || {};
    const timings = m.timings?.series || {};
    const timingRows = Object.entries(timings);
    const latencyHeader = el('thead', {}, el('tr', {}, ['Measured stage', 'Samples', 'p50', 'p95', 'p99'].map(v => el('th', {scope: 'col'}, v))));
    const latencyBody = el('tbody', {}, timingRows.map(([key, value]) => el('tr', {}, el('td', {}, title(key.replaceAll('_seconds', '').replaceAll('_', ' '))), el('td', {}, value.count ?? '—'), el('td', {}, duration(value.p50)), el('td', {}, duration(value.p95)), el('td', {}, duration(value.p99)))));
    const latency = timingRows.length ? el('div', {class: 'table-wrap'}, el('table', {}, latencyHeader, latencyBody)) : empty('activity', 'No processing measurements yet', 'Latency distributions appear after the worker reports actual inference observations.');
    return [heading('Understand coverage, processing and the limits of the model.'),
      el('div', {class: 'metric-grid'}, metric('Inference worker', worker && !worker.stale ? title(worker.state) : 'Unknown', worker?.stale ? 'Heartbeat is stale' : 'Readiness from the worker heartbeat', 'activity', !worker || worker.stale || worker.state !== 'ready'), metric('Analyzed windows', numberOrDash(m.inferred), 'Current worker process · not lifetime totals', 'check'), metric('Queue utilization', m.queue_depth == null ? '—' : m.queue_depth + ' / ' + (config.max_inference_queue ?? '—'), 'Bounded current windows', 'clock'), metric('Skipped windows', numberOrDash(m.dropped), 'Coverage gaps require attention', 'incidents', Number(m.dropped) > 0)),
      el('div', {class: 'settings-grid'}, panel('Model & runtime', 'Reported configuration and active model identity', el('div', {class: 'panel-body'}, definition({'Active model': m.model || 'Not loaded / not reported', 'Compute device': config.model_device, 'Sampling cadence': config.inference_fps == null ? null : config.inference_fps + ' FPS', 'Suspicion threshold': config.detection_threshold, 'Rights configuration': config.model_rights_confirmed ? 'Confirmed by operator' : 'Not confirmed', 'Process memory': m.rss_mb == null ? null : m.rss_mb + ' MiB', 'Process CPU': m.cpu_percent_process == null ? null : m.cpu_percent_process + '% (100% = one CPU)', 'GPU utilization': 'Not measured'}), worker?.detail && el('p', {class: 'form-error'}, title(worker.detail)))),
        panel('Inference latency', 'Bounded recent samples · missing tails remain unreported', latency)),
      panel('Camera analysis', 'Camera connectivity does not imply inference coverage', items(camerasResponse).length ? el('div', {class: 'panel-body'}, items(camerasResponse).map(c => {
        const cm = m.per_camera?.[c.id] || {};
        return el('div', {class: 'audit-event'}, el('div', {class: 'button-row'}, el('strong', {}, c.name), health(c.health?.state)), definition({'Analysis state': title(cm.analysis_state || 'not reported'), 'Analyzed windows': cm.analyzed_windows, 'Skipped windows': cm.skipped_windows, 'Reconnections': cm.reconnects, 'Last frame': date(c.health?.last_frame_at), 'Last inference': cm.last_inference_utc ? date(cm.last_inference_utc) : 'Not reported', 'Analysis rate': cm.analysis_rate_hz_lifetime == null ? 'Not reported' : cm.analysis_rate_hz_lifetime + ' windows/s · process lifetime average', 'Observed frame rate': 'Not measured', 'Visibility assessment': 'Not assessed'}));
      })) : empty('camera', 'No sources to analyze', 'Register a camera to inspect its processing state.')),
      banner('Field accuracy is not established', 'Scores are uncalibrated. The offline demo uses a separately evaluated VideoMAE candidate; sports false alerts remain. Representative low-light, crowd, occlusion and dormitory footage require an authorized held-out evaluation.', 'info')];
  }
  function renderHealth(system, camerasResponse) {
    const cameras = items(camerasResponse); const workers = items(system?.workers); const ops = system?.operations;
    const counts = ops?.outbox_counts || {}; const storage = ops?.evidence_storage;
    return [heading('Know which parts of monitoring are available right now.'),
      el('div', {class: 'metric-grid'}, metric('API', 'Responding', 'This authenticated response · liveness only', 'activity'), metric('Database', ops?.database === 'connected' ? 'Connected' : 'Unknown', 'Read query at observation time', 'evidence'), metric('Evidence storage', storage?.state ? title(storage.state) : 'Unknown', 'Directory availability · integrity checked per clip', 'lock', storage?.state !== 'available'), metric('Notification backlog', ops ? (counts.queued || 0) + (counts.sending || 0) : '—', (counts.failed || 0) + ' failed · ' + (counts.blocked || 0) + ' blocked', 'bell', Boolean(counts.failed || counts.blocked))),
      el('div', {class: 'settings-grid'}, panel('Worker readiness', 'Stale heartbeats override a formerly ready state', el('div', {class: 'panel-body'}, workers.length ? workers.map(workerRow) : el('p', {class: 'settings-help'}, 'No heartbeats received. Worker readiness is unknown.'))),
        panel('Storage & delivery', 'Observations, not a production readiness assertion', el('div', {class: 'panel-body'}, definition({'Free storage': storage?.free_bytes == null ? null : bytes(storage.free_bytes), 'Recorded delivery attempts': ops?.recorded_delivery_attempts, 'Oldest pending': ops?.oldest_pending_seconds_wall_estimate == null ? 'None reported' : duration(ops.oldest_pending_seconds_wall_estimate), 'Telegram configuration': system?.telegram_configured ? 'Configured · external delivery unverified' : 'Unconfigured', 'Retention / legal holds': 'Institutional policy required', 'Observed at': date(ops?.observed_at)}), button('Notification settings', () => navigate('settings'), '', 'bell'))),
        panel('Camera connectivity', 'Fresh frame availability is separate from AI readiness', el('div', {class: 'panel-body'}, cameras.length ? cameras.map(cameraHealthRow) : el('p', {class: 'settings-help'}, 'No camera sources registered.')), button('Open cameras', () => navigate('cameras'))),
        panel('Recovery', 'Safe observation and configuration', el('div', {class: 'panel-body'}, el('p', {class: 'settings-help'}, 'Refresh to inspect current state. Preview retries reauthorize the current account. Worker restarts and infrastructure changes remain restricted maintenance operations.'), button('Refresh health', () => refresh(), '', 'refresh'), button('Open activity journal', () => navigate('journal'), 'link-button'))))];
  }
  function journalQuery() {
    const query = new URLSearchParams({limit: '30', offset: String(state.journalOffset)});
    for (const [key, value] of Object.entries(state.journalFilters)) if (value) query.set(key, value);
    return '?' + query;
  }
  function renderJournal(data) {
    const f = state.journalFilters;
    const search = el('input', {id: 'journal-search', type: 'search', placeholder: 'Event, actor or resource ID', 'aria-label': 'Search activity journal', value: f.q, maxlength: 200});
    const category = el('select', {id: 'journal-category'}, [['', 'All activity'], ['operations', 'Operations'], ['security', 'Security & access']].map(([value, label]) => el('option', {value, selected: f.category === value}, label)));
    const from = el('input', {id: 'journal-from', type: 'date', value: f.from_date}); const to = el('input', {id: 'journal-to', type: 'date', value: f.to_date});
    const form = el('form', {class: 'filters', onsubmit: e => {e.preventDefault(); state.journalFilters = {q: search.value, category: category.value, from_date: from.value, to_date: to.value}; state.journalOffset = 0; refresh();}}, el('div', {class: 'filter-search'}, search), el('label', {}, 'Category', category), el('label', {}, 'From · UTC', from), el('label', {}, 'Through · UTC', to), el('button', {type: 'submit'}, 'Search journal'));
    const valid = data?.integrity === true || data?.integrity?.valid === true;
    return [heading('A traceable record of detection, response and access.'), el('div', {class: 'journal-integrity'}, el('span', {class: 'pill ' + (valid ? 'good' : 'warn')}, valid ? 'Audit chain verified' : 'Integrity not verified'), el('span', {}, 'Application hash chain · privileged database operators remain in the trust boundary')), form,
      el('section', {class: 'panel'}, el('div', {class: 'panel-heading'}, el('h2', {}, 'Activity journal'), el('span', {class: 'muted'}, 'Read only · UTC filters')), items(data).length ? el('div', {class: 'panel-body'}, items(data).map(row => {
        const incidentId = row.detail?.correlation_id || (row.action?.startsWith('incident.') ? row.resource_id : null);
        return el('article', {class: 'audit-event'}, el('div', {class: 'button-row'}, el('strong', {}, title(row.action?.replaceAll('.', ' '))), el('span', {class: 'pill outline'}, /^(auth|user|telegram|evidence)\./.test(row.action || '') ? 'Security & access' : 'Operations')),
          el('small', {}, date(row.created_at) + ' · ' + (row.actor_id ? 'Actor ' + row.actor_id : 'System service')),
          el('small', {class: 'mono'}, 'Resource ' + row.resource_id), incidentId && button('Open related incident', () => openIncident(incidentId), 'link-button'),
          el('details', {}, el('summary', {}, 'Recorded details'), el('pre', {class: 'mono'}, JSON.stringify(row.detail || {}, null, 2))));
      })) : empty('clock', 'No activity matches this view', 'Try another date, category or search term.'), pagination(data, offset => {state.journalOffset = offset; refresh();}))];
  }
  function profileForm() {
    const session = state.sessionSerial;
    const name = el('input', {id: 'profile-display-name', name: 'display_name', autocomplete: 'name', maxlength: 80, required: true, value: state.drafts.get('profile-name') ?? state.user.display_name ?? '', oninput: e => state.drafts.set('profile-name', e.target.value)});
    const feedback = el('p', {class: 'settings-help', role: 'status'});
    const save = el('button', {type: 'submit', class: 'primary'}, 'Save display name');
    return el('form', {class: 'profile-form', onsubmit: async event => {
      event.preventDefault(); if (save.disabled) return; save.disabled = true; feedback.textContent = '';
      try {
        const user = await api('/auth/profile', {method: 'PATCH', body: {display_name: name.value}});
        if (!state.user || session !== state.sessionSerial) return;
        state.user = user; state.drafts.delete('profile-name'); name.value = user.display_name;
        document.querySelectorAll('[data-account-name]').forEach(node => {node.textContent = accountName();});
        feedback.className = 'form-success'; feedback.textContent = 'Display name saved.';
      } catch (e) {feedback.className = 'form-error'; feedback.textContent = e.message;}
      finally {save.disabled = false;}
    }}, field('Display name', name, 'Shown in your workspace. Your trusted role remains unchanged.'), save, feedback);
  }
  function accountManagement(users) {
    return el('div', {class: 'panel-body'}, users.map(user => el('article', {class: 'audit-event'},
      el('strong', {}, user.display_name || user.email), el('small', {}, user.email + ' · ' + title(user.role)),
      el('div', {class: 'button-row'}, el('span', {class: 'pill ' + (user.enabled ? 'good' : 'warn')}, user.enabled ? 'Enabled' : 'Disabled'),
        user.id !== state.user.id && button(user.enabled ? 'Revoke access' : 'Enable access', () => accountAvailabilityDialog(user))))),
      el('p', {class: 'settings-help'}, 'New identities and role assignments require trusted server provisioning. Re-enabling requires a fresh sign-in.'));
  }
  function accountAvailabilityDialog(user) {
    const label = user.enabled ? 'Revoke account access' : 'Enable account access';
    const feedback = el('p', {class: 'settings-help', role: 'status'});
    const confirm = button(label, async () => {
      confirm.disabled = true;
      try {await api('/users/' + encodeURIComponent(user.id), {method: 'PATCH', body: {enabled: !user.enabled}}); actionDialog.close(); toast('Account access updated. Previous sessions and preview tickets revoked.'); await refresh();}
      catch (e) {feedback.className = 'form-error'; feedback.textContent = e.message; confirm.disabled = false;}
    }, user.enabled ? 'danger' : 'primary');
    showAction(el('div', {}, el('div', {class: 'dialog-header'}, el('h2', {id: 'action-title'}, label), closeButton(actionDialog)), el('div', {class: 'dialog-body'}, el('p', {}, user.email), el('p', {class: 'settings-help'}, user.enabled ? 'This account will lose access and all its existing application sessions and camera tickets will be revoked. Incident and evidence history will be preserved.' : 'This trusted account can sign in again. Previous sessions will remain revoked.'), feedback), el('div', {class: 'dialog-footer'}, button('Cancel', () => actionDialog.close()), confirm)));
  }
  function renderSettings(system, integrations, camerasResponse, audit, notifications, users) {
    const config = system?.configuration || {}; const telegram = integrations?.telegram;
    const workers = items(system?.workers); const cameras = items(camerasResponse);
    const demoControls = state.auth.demo_mode === 'offline' && panel('Offline demo', 'Persistent local run · no external providers', el('div', {class: 'panel-body'}, el('p', {}, 'Demo Dormitory Director · real pretrained frame screening · human review required.'), button('Reset demo', resetDemoDialog, 'danger', 'refresh')));
    return [heading('Manage your workspace connections and inspect operational readiness.'), state.auth.evaluation && banner(state.auth.mode === 'local' ? 'Local evaluation mode' : 'Non-production evaluation', state.auth.mode === 'local' ? 'Authentication is using explicitly enabled local evaluation accounts. Configure Supabase and secure transport before institutional use.' : 'Authentication uses Supabase. This is an evaluation workspace; secure transport and deployment acceptance are required before institutional use.'),
      el('div', {class: 'settings-grid'}, panel('Your account', 'Trusted organizational role · provisioned by an administrator', el('div', {class: 'panel-body'}, definition({'Email': state.user.email, 'Role': roleName(), 'Authentication': title(system?.auth_mode || state.auth.mode)}), profileForm(), el('p', {class: 'settings-help'}, 'Account creation and role changes require trusted administrator provisioning. Your display name does not grant access.'))),
        state.user.role === 'super_admin' && panel('Authorized accounts', 'Trusted assignments · session revocation is audited', users ? accountManagement(items(users)) : empty('lock', 'Accounts unavailable', 'Refresh to retry.')),
        panel('Telegram alerts', 'Bind your own private Telegram account', telegramPanel(telegram)),
        panel('Camera management', 'Credentials are configured privately using environment references', el('div', {class: 'panel-body'}, cameras.length ? cameras.map(cameraHealthRow) : el('p', {class: 'settings-help'}, 'No camera sources are registered.'), isDirector() ? el('div', {class: 'button-row'}, button('Register camera', () => cameraDialog(), 'primary', 'plus'), button('Manage cameras', () => navigate('cameras'))) : el('p', {class: 'settings-help'}, 'Camera management is available to the Dormitory Director.'))),
        panel('Detection configuration', 'Server configuration · changes require a worker restart', el('div', {class: 'panel-body'}, definition({'Model configured': config.model_configured == null ? 'Unknown' : config.model_configured ? 'Yes' : 'No', 'Model rights confirmed': config.model_rights_confirmed == null ? 'Unknown' : config.model_rights_confirmed ? 'Yes' : 'No', 'Inference sampling': config.inference_fps != null ? `${config.inference_fps} FPS` : null, 'Queue capacity': config.max_inference_queue, 'Compute device': config.model_device, 'Suspicion threshold': config.detection_threshold, 'Incident cooldown': config.incident_cooldown_seconds != null ? `${config.incident_cooldown_seconds} seconds` : null}), el('p', {class: 'settings-help'}, 'A configured model does not establish detection quality. Scores remain uncalibrated; human review is required.'))),
        panel('Worker health', 'A running API does not establish camera or inference readiness', el('div', {class: 'panel-body'}, workers.length ? workers.map(workerRow) : el('p', {class: 'settings-help'}, 'No worker health has been reported. Start the workers and refresh.'))),
        panel('Operational checks', 'Observed server state; liveness is separate from readiness', el('div', {class: 'panel-body'}, definition({'Database': system?.operations?.database || 'Unknown', 'Pending notifications': system?.operations ? (system.operations.outbox_counts?.queued || 0) + (system.operations.outbox_counts?.sending || 0) : 'Unknown', 'Failed notifications': system?.operations?.outbox_counts?.failed ?? 'Unknown', 'Blocked recipients': system?.operations?.outbox_counts?.blocked ?? 'Unknown', 'Retention enforcement': 'Blocked pending approved retention and hold policy'}))),
        panel('Notification delivery', 'Provider delivery is separate from human acknowledgment', notifications ? notificationList(items(notifications)) : empty('bell', 'Delivery data unavailable', 'Refresh to retry the notification service.')),
        panel('Audit history', 'Recent append-only application events', audit ? auditPanel(audit) : empty('shield', 'Audit data unavailable', 'Refresh to retry. No integrity result is assumed.'))),
      demoControls, el('p', {class: 'footnote'}, icon('lock'), 'Operational access is enforced by the server. Database owners and infrastructure administrators remain within the trust boundary.')];
  }
  function telegramPanel(telegram) {
    if (!telegram) return empty('bell', 'Integration status unavailable', 'The server did not return Telegram configuration. Refresh to retry.');
    if (!telegram.configured) return el('div', {class: 'panel-body'}, health('unconfigured'), el('p', {class: 'settings-help'}, 'Telegram is not configured on the server. Your administrator must configure the bot token, webhook secret, and public dashboard URL. No external message has been sent.'));
    const result = el('div', {}); const feedback = el('p', {class: 'settings-help', role: 'status'});
    const showLink = data => {
      const url = safeTelegram(data.url);
      result.replaceChildren(el('div', {class: 'token-box'}, el('span', {class: 'mono'}, data.token || 'Link token not returned'), url ? el('a', {href: url, target: '_blank', rel: 'noopener noreferrer', class: 'primary-link'}, 'Open Telegram to complete binding') : el('p', {}, 'Open your configured Telegram bot and send the link token.'), el('small', {}, `Expires ${date(data.expires_at)}. This one-time token binds your own account. Refresh after completing the bot flow.`)));
      feedback.textContent = 'Open Telegram and tap Start to authorize this private binding. Return here to check the connection.';
    };
    if (telegram.linked) state.telegramLink = null;
    else if (state.telegramLink) showLink(state.telegramLink);
    const link = button(telegram.linked ? 'Unlink Telegram' : 'Link Telegram account', async () => {
      link.disabled = true;
      try {
        if (telegram.linked) {await api('/integrations/telegram/link', {method: 'DELETE'}); state.telegramLink = null; toast('Telegram account unlinked.'); refresh();}
        else {state.telegramLink = await api('/integrations/telegram/link', {method: 'POST', body: {}}); showLink(state.telegramLink);}
      } catch (e) {feedback.textContent = e.message; feedback.className = 'form-error';}
      finally {link.disabled = false;}
    }, telegram.linked ? '' : 'primary', 'link');
    return el('div', {class: 'panel-body'}, el('div', {class: 'button-row'}, health(telegram.linked ? 'connected' : 'not linked'), telegram.bot_username && el('span', {class: 'muted'}, `@${telegram.bot_username}`)), el('p', {class: 'settings-help'}, telegram.linked ? 'Telegram connected. This official account has a private-chat binding. Incident notifications follow the server delivery policy.' : 'Create a short-lived link, then complete binding in a private chat with the configured bot.'), telegram.linked && el('p', {class: 'settings-help'}, 'Private chat connected ' + date(telegram.linked_at) + '. Telegram identity is kept private on the server.'), el('div', {class: 'button-row'}, link, telegram.linked && button('Send test notification', telegramTestDialog, '', 'bell'), !telegram.linked && button('Check connection', () => refresh(), '', 'refresh')), feedback, result);
  }
  function telegramTestDialog() {
    const feedback = el('p', {class: 'settings-help', role: 'status'});
    const send = button('Send my test', async () => {
      send.disabled = true;
      try {const result = await api('/integrations/telegram/test', {method: 'POST', body: {confirm: true}}); feedback.textContent = result.accepted === true ? 'Telegram API accepted the test. Check your device; receipt is not verified by the server.' : 'The server did not confirm acceptance.';}
      catch (e) {feedback.textContent = e.message; feedback.className = 'form-error';}
    }, 'primary', 'bell');
    showAction(el('div', {}, el('div', {class: 'dialog-header'}, el('h2', {id: 'action-title'}, 'Test your Telegram connection'), closeButton(actionDialog)), el('div', {class: 'dialog-body'}, el('p', {class: 'settings-help'}, 'Send one clearly labeled test to the private Telegram account currently connected to your official login. No incident is created.'), feedback), el('div', {class: 'dialog-footer'}, button('Cancel', () => actionDialog.close()), send)));
  }
  function notificationList(rows) {
    if (!rows.length) return empty('bell', 'No notification deliveries', 'Attempts, retries and failures appear when alerts are queued for your account.');
    return el('div', {class: 'panel-body audit-scroll'}, rows.slice(0, 100).map(row => {
      const retry = button('Retry delivery', async () => {retry.disabled = true; try {await api('/notifications/' + encodeURIComponent(row.id) + '/retry', {method: 'POST', body: {}}); toast('Delivery queued for retry. Provider acceptance is not yet confirmed.'); if (state.detailId) await refreshDetail(); else await refresh();} catch (e) {toast(e.message); retry.disabled = false;}});
      return el('article', {class: 'audit-event'}, el('div', {class: 'button-row'}, button('Incident ' + String(row.incident_id || '').slice(0, 8), () => openIncident(row.incident_id), 'row-link'), el('span', {class: 'pill ' + (row.state === 'accepted' ? 'good' : 'warn')}, row.state === 'accepted' ? 'Telegram API accepted' : title(row.state || row.status))),
        row.recipient_role && el('small', {}, 'Recipient: ' + title(row.recipient_role)),
        el('small', {}, date(row.created_at) + ' · Attempts: ' + (row.attempts ?? 0)), row.last_error && el('small', {}, row.last_error),
        row.state === 'accepted' && el('small', {}, 'Device receipt unverified · human acknowledgement is separate.'),
        row.state === 'queued' && row.next_attempt && el('small', {}, 'Next eligible attempt ' + date(row.next_attempt)),
        ['failed', 'blocked'].includes(row.state) && row.can_retry !== false && retry,
        items(row.history).length > 0 && el('details', {}, el('summary', {}, 'Attempt history'), items(row.history).map(attempt => el('p', {class: 'settings-help'}, date(attempt.created_at) + ' · ' + title(attempt.state) + ' · ' + (attempt.detail || '')))));
    }));
  }
  function auditPanel(data) {const rows = items(data); return el('div', {class: 'panel-body'}, el('div', {class: 'button-row'}, el('span', {class: `pill ${data.integrity === true || data.integrity?.valid === true ? 'good' : 'warn'}`}, data.integrity === true || data.integrity?.valid === true ? 'Audit chain verified' : 'Integrity not verified')), el('p', {class: 'settings-help'}, 'Application-level tamper evidence does not prevent a privileged database administrator from rewriting the audit chain.'), rows.length ? el('div', {class: 'audit-scroll'}, rows.slice(0, 40).map(row => el('div', {class: 'audit-event'}, el('strong', {}, title(row.action || row.kind || row.event)), el('small', {}, `${date(row.created_at)} · ${row.actor_id ? `Actor ${String(row.actor_id).slice(0, 12)}` : 'System'}`)))) : el('p', {class: 'settings-help'}, 'No recent audit events were returned.'));}

  async function boot() {
    try {state.auth = await api('/auth/config', {allowUnauthorized: true}) || {};}
    catch (e) {state.auth = {}; renderLogin(`Cannot load authentication configuration: ${e.message}`); return;}
    try {state.user = await api('/auth/me', {allowUnauthorized: true}); state.sessionSerial++; await enterWorkspace();
    } catch (e) {state.user = null; renderLogin(e.status === 401 ? '' : e.message);}
  }
  function requestedTarget() {
    const fragment = /^#(incidents|cameras)\/([A-Za-z0-9_-]{1,100})$/.exec(location.hash);
    if (fragment) return {view: fragment[1], id: fragment[2]};
    const view = /^#(overview|cameras|incidents|analytics|journal|health|settings|evidence)$/.exec(location.hash);
    if (view) return {view: view[1]};
    const id = new URLSearchParams(location.search).get('incident');
    return id && /^[A-Za-z0-9_-]{1,100}$/.test(id) ? {view: 'incidents', id} : null;
  }
  async function enterWorkspace() {
    const target = requestedTarget(); const entry = ++state.entrySerial; state.renderSerial++;
    if (target) state.view = target.view;
    if (target?.view === 'cameras' && target.id) {state.cameraLayout = 'single'; state.selectedCamera = target.id; if (detailDialog.open) detailDialog.close();}
    renderShell(); startUpdates(); await refresh();
    if (target?.id && state.user && entry === state.entrySerial) {
      if (target.view === 'incidents') await openIncident(target.id);
      else {
        const camera = state.cameras.find(c => c.id === target.id);
        const preview = document.getElementById('preview-' + target.id);
        if (camera && preview) await openStream(camera, preview);
        else toast('The requested camera is unavailable or outside the current authorized workspace.');
      }
    }
  }
  addEventListener('hashchange', async () => {if (state.user && requestedTarget()) await enterWorkspace();});
  boot();
})();
