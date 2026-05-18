/*
  Helen Admin Mobile — operator console app logic.

  Intentionally framework-free. Screens: auth, overview, users,
  userDetail, network, backups.

  REST contract (all under /api/admin/* except auth):
    POST /api/auth/login
    GET  /api/admin/stats
    GET  /api/admin/users
    POST /api/admin/kick/{user_id}
    POST /api/admin/ban/{user_id}
    POST /api/admin/set-role/{user_id}     { role: 'admin' }
    GET  /api/admin/connectivity
    GET  /api/admin/federation/bridges
    GET  /api/admin/diagnostics/network
    GET  /api/admin/backups
    POST /api/admin/backups/run-now

  HELEN_BASE is resolved by config.js: when served from Helen itself
  under /admin-mobile/ it equals location.origin; otherwise defaults
  to http://localhost:3000. Operator can override in the auth screen
  via a hidden developer gesture (query string ?server=).
*/
(function () {
  'use strict';

  // ── Store ───────────────────────────────────────────────────
  const Store = {
    get serverUrl()  { return localStorage.getItem('helen.admin.serverUrl') || window.HELEN_BASE; },
    set serverUrl(v) { v ? localStorage.setItem('helen.admin.serverUrl', v)
                          : localStorage.removeItem('helen.admin.serverUrl'); },
    get token()      { return localStorage.getItem('helen.admin.token') || ''; },
    set token(v)     { v ? localStorage.setItem('helen.admin.token', v)
                          : localStorage.removeItem('helen.admin.token'); },
    get user()       {
      try { return JSON.parse(localStorage.getItem('helen.admin.user') || 'null'); }
      catch { return null; }
    },
    set user(v) {
      if (v) localStorage.setItem('helen.admin.user', JSON.stringify(v));
      else localStorage.removeItem('helen.admin.user');
    },
    clear() {
      localStorage.removeItem('helen.admin.token');
      localStorage.removeItem('helen.admin.user');
    },
  };

  // ── Api ─────────────────────────────────────────────────────
  const Api = {
    async request(method, path, body) {
      const headers = { 'Content-Type': 'application/json' };
      if (Store.token) headers.Authorization = 'Bearer ' + Store.token;
      let resp;
      try {
        resp = await fetch(Store.serverUrl + path, {
          method, headers,
          body: body ? JSON.stringify(body) : undefined,
        });
      } catch (e) {
        throw new Error('تعذّر الاتصال بالخادم');
      }
      let data = null;
      const txt = await resp.text();
      if (txt) {
        try { data = JSON.parse(txt); } catch { data = { detail: txt }; }
      }
      if (!resp.ok) {
        const msg = (data && (data.detail || data.message)) || ('HTTP ' + resp.status);
        const err = new Error(msg);
        err.status = resp.status;
        throw err;
      }
      return data;
    },
    get(p)       { return this.request('GET',    p); },
    post(p, b)   { return this.request('POST',   p, b); },
    del(p)       { return this.request('DELETE', p); },
    patch(p, b)  { return this.request('PATCH',  p, b); },
  };

  // ── Helpers ─────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  function escape(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  function setText(el, txt, kind) {
    if (!el) return;
    el.textContent = txt || '';
    el.classList.remove('ok', 'err');
    if (kind) el.classList.add(kind);
  }
  function initials(name) {
    const parts = String(name || '').trim().split(/\s+/);
    return ((parts[0]||'').slice(0,1) + (parts[1]||'').slice(0,1)).toUpperCase() || '?';
  }
  function fmtBytes(n) {
    if (!n && n !== 0) return '—';
    const u = ['B','KB','MB','GB','TB'];
    let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + ' ' + u[i];
  }
  function fmtRel(iso) {
    if (!iso) return '—';
    // Server emits naive ISO timestamps representing UTC. Tell JS so by
    // appending 'Z' if the string lacks a tz suffix.
    let s = String(iso);
    if (!/[zZ]|[+\-]\d{2}:?\d{2}$/.test(s)) s += 'Z';
    const t = new Date(s).getTime();
    if (isNaN(t)) return String(iso);
    const d = Math.max(0, (Date.now() - t) / 1000);
    if (d < 60) return 'قبل ' + Math.round(d) + 'ث';
    if (d < 3600) return 'قبل ' + Math.round(d/60) + 'د';
    if (d < 86400) return 'قبل ' + Math.round(d/3600) + 'س';
    return 'قبل ' + Math.round(d/86400) + 'ي';
  }

  // ── Toast ────────────────────────────────────────────────────
  function toast(msg, kind) {
    const host = $('toastHost');
    if (!host) return;
    const el = document.createElement('div');
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; }, 2400);
    setTimeout(() => { el.remove(); }, 2800);
  }

  // ── Status bar clock ─────────────────────────────────────────
  function tickClock() {
    const el = $('statusTime');
    if (!el) return;
    const d = new Date();
    el.textContent = d.getHours().toString().padStart(2,'0') + ':' +
                     d.getMinutes().toString().padStart(2,'0');
  }
  tickClock();
  setInterval(tickClock, 30_000);

  // ── Router ──────────────────────────────────────────────────
  const _screens = {};
  document.querySelectorAll('.screen').forEach((s) => {
    _screens[s.dataset.screen] = s;
  });
  const _tabs = {
    overview: $('tabBar').querySelector('[data-tab="overview"]'),
    users:    $('tabBar').querySelector('[data-tab="users"]'),
    network:  $('tabBar').querySelector('[data-tab="network"]'),
    backups:  $('tabBar').querySelector('[data-tab="backups"]'),
    more:     $('tabBar').querySelector('[data-tab="more"]'),
  };
  const TAB_SCREENS = new Set(['overview','users','network','backups','more']);

  function show(name, opts) {
    opts = opts || {};
    Object.values(_screens).forEach((s) => { s.hidden = true; });
    const target = _screens[name];
    if (!target) return;
    target.hidden = false;
    // Tab bar visible only on signed-in tabs.
    $('tabBar').hidden = !TAB_SCREENS.has(name);
    // Highlight tab.
    Object.entries(_tabs).forEach(([tab, btn]) => {
      btn.classList.toggle('tab-active', tab === name);
    });
    if (typeof opts.onEnter === 'function') opts.onEnter();
  }
  Object.entries(_tabs).forEach(([tab, btn]) => {
    btn.addEventListener('click', () => {
      if (tab === 'overview') loadOverview();
      else if (tab === 'users') loadUsers();
      else if (tab === 'network') loadNetwork();
      else if (tab === 'backups') loadBackups();
      else if (tab === 'more') loadMore();
      show(tab);
    });
  });

  // Back-button wiring for all sub-screens.
  document.querySelectorAll('[data-back]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const dest = btn.dataset.back;
      if (dest === 'more') loadMore();
      show(dest);
    });
  });

  // ── Auth ────────────────────────────────────────────────────
  function enterAuth() {
    $('authUser').value = '';
    $('authPass').value = '';
    setText($('authStatus'), '');
    $('authServerHint').textContent = 'الخادم: ' + Store.serverUrl;
  }
  $('authForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = $('authUser').value.trim();
    const password = $('authPass').value;
    if (!username || !password) return;
    setText($('authStatus'), 'جارٍ الدخول…');
    try {
      const r = await Api.post('/api/auth/login', { username, password });
      const tok = (r && r.tokens && r.tokens.access_token) || r.access_token;
      if (!tok) throw new Error('لا يوجد رمز دخول في الرد');
      Store.token = tok;
      // Role lives in the JWT payload, not the user object. Decode it.
      let role = (r.user && r.user.role) || '';
      try {
        const payload = JSON.parse(
          atob(tok.split('.')[1].replace(/-/g,'+').replace(/_/g,'/'))
        );
        role = payload.role || role;
      } catch { /* token may be opaque; leave role as-is */ }
      Store.user = Object.assign({}, r.user || { username }, { role });
      if (role !== 'admin') {
        toast('تحذير: حسابك ليس admin — بعض الوظائف قد لا تعمل.', 'err');
      } else {
        toast('أهلاً ' + ((r.user && r.user.display_name) || username), 'ok');
      }
      show('overview');
      loadOverview();
    } catch (err) {
      setText($('authStatus'), err.message || 'فشل الدخول', 'err');
    }
  });

  // ── Overview ────────────────────────────────────────────────
  const _sparkData = { cpu: [], mem: [] };
  const SPARK_MAX = 30;

  function pushSpark(series, value) {
    series.push(value);
    while (series.length > SPARK_MAX) series.shift();
  }
  function renderSpark(svgId, series, maxHint) {
    const svg = $(svgId);
    if (!svg || series.length < 2) return;
    const max = Math.max(maxHint || 1, ...series);
    const w = 200, h = 40;
    const step = w / (SPARK_MAX - 1);
    const points = series.map((v, i) => {
      const x = (SPARK_MAX - series.length + i) * step;
      const y = h - (max > 0 ? (v / max) * (h - 4) : 0) - 2;
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
    const poly = svg.querySelector('polyline');
    if (poly) poly.setAttribute('points', points);
  }
  function fmtUptime(sec) {
    if (!sec && sec !== 0) return '—';
    sec = Math.floor(sec);
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (d) return d + 'ي ' + h + 'س';
    if (h) return h + 'س ' + m + 'د';
    return m + 'د ' + (sec % 60) + 'ث';
  }

  async function loadOverview() {
    // Health / discovery first — works even without admin role.
    try {
      const disc = await Api.get('/api/discovery');
      $('healthDot').style.background = '#5cd7a5';
      setText($('healthText'), 'الخادم يعمل');
      $('healthDetail').textContent = (disc.name || 'Helen') +
        ' · v' + (disc.version || '?');
      document.querySelectorAll('#serverMetrics [data-m]').forEach((el) => {
        const k = el.dataset.m;
        // These keys come from /api/admin/stats (populated below), leave
        // them alone if discovery doesn't carry them.
        if (['uptime','db_size','hostname','lan_ip'].includes(k)) return;
        el.textContent = disc[k] != null ? String(disc[k]) : '—';
      });
    } catch (e) {
      $('healthDot').style.background = '#ff5c7a';
      setText($('healthText'), 'لا يوجد اتصال', 'err');
      $('healthDetail').textContent = e.message;
    }
    // Admin-only stats.
    try {
      const stats = await Api.get('/api/admin/stats');
      setKpi('users',   stats.total_users,    'ok');
      setKpi('online',  stats.connected_users ?? stats.online_users ?? 0, 'ok');
      setKpi('channels',stats.total_channels, 'ok');
      setKpi('messages',stats.total_messages, 'ok');
      // Extended server metrics.
      const setM = (k, v) => {
        const el = document.querySelector('#serverMetrics [data-m="' + k + '"]');
        if (el) el.textContent = v;
      };
      setM('hostname', stats.hostname || '—');
      setM('lan_ip',   stats.lan_ip || '—');
      setM('uptime',   fmtUptime(stats.uptime_seconds));
      setM('db_size',  fmtBytes(stats.db_size_bytes));
      // Sparklines.
      pushSpark(_sparkData.cpu, stats.cpu_percent || 0);
      pushSpark(_sparkData.mem, stats.memory_usage_mb || 0);
      renderSpark('cpuSpark', _sparkData.cpu, 100);
      renderSpark('memSpark', _sparkData.mem, Math.max(512, ...(_sparkData.mem)));
      $('cpuVal').textContent = (stats.cpu_percent || 0).toFixed(1) + '%';
      $('memVal').textContent = Math.round(stats.memory_usage_mb || 0) + ' MB';
    } catch (e) {
      setKpi('users',   '—', 'warn');
      setKpi('online',  '—', 'warn');
      setKpi('channels','—', 'warn');
      setKpi('messages','—', 'warn');
      if (e.status === 403) {
        toast('الحساب ليس admin — لا يمكن قراءة الإحصائيات.', 'err');
      }
    }
    // SFU + auth-queue snapshot — both new endpoints from the desktop
    // admin parity pass. Render inline below the existing KPIs as a
    // compact card; failures are silent so legacy servers without these
    // endpoints don't surface an error to the operator.
    try {
      const sfu = await Api.get('/api/admin/sfu/status');
      const sfuEl = $('sfuStatus');
      if (sfuEl) {
        const dot = sfu.healthy ? '🟢' : (sfu.running ? '🟡' : '🔴');
        const lbl = sfu.healthy ? 'يعمل وسليم' : (sfu.running ? 'يعمل بدون استجابة' : (sfu.enabled ? 'متوقف' : 'معطّل'));
        sfuEl.innerHTML =
          dot + ' <strong>SFU:</strong> ' + lbl +
          ' <span class="muted">· ' + (sfu.control_host || '—') + ':' +
          (sfu.control_port || '—') + ' · إعادة تشغيل ' +
          (sfu.restart_count || 0) + '</span>';
      }
    } catch { /* legacy server — drop silently */ }
  }
  // Auto-refresh overview every 5s while visible.
  setInterval(() => {
    if (!_screens.overview.hidden) loadOverview();
  }, 5000);
  function setKpi(key, value, kind) {
    const tile = document.querySelector('.kpi-tile[data-kpi="' + key + '"]');
    if (!tile) return;
    tile.classList.remove('ok','warn','err');
    if (kind) tile.classList.add(kind);
    const v = tile.querySelector('[data-slot="value"]');
    if (v) v.textContent = (value == null || value === '—') ? '—' : String(value);
  }
  $('overviewRefresh').addEventListener('click', loadOverview);

  // ── Sheet modal ─────────────────────────────────────────────
  // openSheet({title, bodyHtml, confirmLabel?, onConfirm})
  let _sheetResolver = null;
  function openSheet(opts) {
    $('sheetTitle').textContent = opts.title || '';
    $('sheetBody').innerHTML = opts.bodyHtml || '';
    const conf = $('sheetConfirm');
    conf.textContent = opts.confirmLabel || 'حفظ';
    $('sheet').hidden = false;
    return new Promise((res) => { _sheetResolver = res; });
  }
  function closeSheet(value) {
    $('sheet').hidden = true;
    if (_sheetResolver) { const r = _sheetResolver; _sheetResolver = null; r(value); }
  }
  $('sheetCancel').addEventListener('click', () => closeSheet(null));
  $('sheetConfirm').addEventListener('click', () => {
    // Gather form values from inside the sheet body.
    const vals = {};
    $('sheetBody').querySelectorAll('input[data-field], textarea[data-field]').forEach((el) => {
      vals[el.dataset.field] = el.value;
    });
    closeSheet(vals);
  });

  // ── Server-config edit (rename) ─────────────────────────────
  $('btnEditServerName').addEventListener('click', async () => {
    let current = '';
    try {
      const c = await Api.get('/api/admin/server-config');
      current = c.server_name || c.name || '';
    } catch (_e) { /* keep empty */ }
    const vals = await openSheet({
      title: 'تعديل اسم الخادم',
      bodyHtml: '<input type="text" data-field="server_name" ' +
                'placeholder="اسم جديد" value="' + escape(current) + '" />',
      confirmLabel: 'حفظ',
    });
    if (!vals || !vals.server_name || !vals.server_name.trim()) return;
    try {
      await Api.patch('/api/admin/server-config',
                      { server_name: vals.server_name.trim() });
      toast('تم تحديث الاسم', 'ok');
      loadOverview();
    } catch (e) { toast(e.message, 'err'); }
  });

  // ── Users ───────────────────────────────────────────────────
  let _allUsers = [];
  async function loadUsers() {
    setText($('usersStatus'), 'جارٍ التحميل…');
    try {
      // List endpoint lives at /api/users (not /api/admin/users) — admin
      // permissions enforced on the server side.
      const r = await Api.get('/api/users');
      _allUsers = Array.isArray(r) ? r : (r.users || r.items || []);
      renderUsers(_allUsers);
      setText($('usersStatus'),
        _allUsers.length + ' مستخدم', _allUsers.length ? 'ok' : '');
    } catch (e) {
      setText($('usersStatus'), e.message, 'err');
      if (e.status === 403) {
        toast('الحساب ليس admin — لا يمكن قراءة المستخدمين.', 'err');
      }
    }
  }
  function renderUsers(users) {
    const list = $('userList');
    list.innerHTML = '';
    if (!users.length) {
      list.innerHTML = '<li class="empty-row">لا يوجد مستخدمون.</li>';
      return;
    }
    users.forEach((u) => {
      const li = document.createElement('li');
      const role = u.role || 'user';
      const roleLbl = role === 'admin' ? 'admin' : (u.banned ? 'banned' : 'user');
      li.innerHTML =
        '<div class="avatar-sm">' + escape(initials(u.display_name || u.username)) + '</div>' +
        '<div class="row-body">' +
          '<div class="row-title">' + escape(u.display_name || u.username) + '</div>' +
          '<div class="row-sub mono">' + escape('@' + u.username) +
            ' · <span class="role-badge ' + roleLbl + '">' + roleLbl + '</span></div>' +
        '</div>';
      li.addEventListener('click', () => openUserDetail(u));
      list.appendChild(li);
    });
  }
  $('userSearch').addEventListener('input', (e) => {
    const q = e.target.value.trim().toLowerCase();
    if (!q) { renderUsers(_allUsers); return; }
    renderUsers(_allUsers.filter((u) =>
      (u.username || '').toLowerCase().includes(q) ||
      (u.display_name || '').toLowerCase().includes(q) ||
      (u.id || '').toLowerCase().includes(q)
    ));
  });

  // ── User detail ─────────────────────────────────────────────
  let _currentUser = null;
  function openUserDetail(u) {
    _currentUser = u;
    $('userDetailTitle').textContent = u.display_name || u.username;
    $('userDetailHero').innerHTML =
      '<div class="avatar-xl">' + escape(initials(u.display_name || u.username)) + '</div>' +
      '<h2 class="hero-title">' + escape(u.display_name || u.username) + '</h2>' +
      '<p class="hero-sub">' + escape('@' + u.username) + '</p>';
    const meta = $('userDetailMeta');
    meta.innerHTML = '';
    const rows = [
      ['المعرّف',   u.id || '—'],
      ['الدور',     u.role || 'user'],
      ['محظور',     u.banned ? 'نعم' : 'لا'],
      ['الإنشاء',   u.created_at || '—'],
      ['آخر دخول', u.last_seen || u.last_login || '—'],
    ];
    rows.forEach(([k,v]) => {
      const row = document.createElement('div');
      row.className = 'metric-row';
      row.innerHTML = '<span class="metric-key">' + escape(k) + '</span>' +
                      '<span class="metric-val">' + escape(String(v)) + '</span>';
      meta.appendChild(row);
    });
    setText($('userDetailStatus'), '');
    show('userDetail');
  }
  $('userDetailBack').addEventListener('click', () => {
    show('users');
    loadUsers();
  });
  $('btnKick').addEventListener('click', async () => {
    if (!_currentUser || !confirm('فصل جميع جلسات المستخدم؟')) return;
    try {
      await Api.post('/api/admin/kick/' + encodeURIComponent(_currentUser.id));
      toast('تم فصل الجلسات', 'ok');
    } catch (e) { setText($('userDetailStatus'), e.message, 'err'); }
  });
  $('btnBan').addEventListener('click', async () => {
    if (!_currentUser || !confirm('حظر هذا المستخدم؟')) return;
    try {
      await Api.post('/api/admin/ban/' + encodeURIComponent(_currentUser.id));
      toast('تم الحظر', 'ok');
    } catch (e) { setText($('userDetailStatus'), e.message, 'err'); }
  });
  $('btnPromote').addEventListener('click', async () => {
    if (!_currentUser || !confirm('ترقية المستخدم إلى admin؟')) return;
    try {
      await Api.post('/api/admin/set-role/' + encodeURIComponent(_currentUser.id),
                     { role: 'admin' });
      toast('تمت الترقية', 'ok');
    } catch (e) { setText($('userDetailStatus'), e.message, 'err'); }
  });
  $('btnUnban').addEventListener('click', async () => {
    if (!_currentUser || !confirm('إلغاء حظر هذا المستخدم؟')) return;
    try {
      await Api.post('/api/admin/unban/' + encodeURIComponent(_currentUser.id));
      toast('تم إلغاء الحظر', 'ok');
    } catch (e) { setText($('userDetailStatus'), e.message, 'err'); }
  });
  $('btnSessions').addEventListener('click', () => {
    if (!_currentUser) return;
    loadSessions(_currentUser);
    show('sessions');
  });

  // ── Sessions (per user) ─────────────────────────────────────
  async function loadSessions(user) {
    $('sessionsTitle').textContent = 'جلسات: ' + (user.display_name || user.username);
    setText($('sessionsStatus'), 'جارٍ التحميل…');
    $('sessionsList').innerHTML = '';
    try {
      const r = await Api.get('/api/admin/users/' +
                              encodeURIComponent(user.id) + '/sessions');
      const items = Array.isArray(r) ? r : (r.sessions || r.items || []);
      if (!items.length) {
        $('sessionsList').innerHTML =
          '<li class="empty-row">لا توجد جلسات نشطة.</li>';
        setText($('sessionsStatus'), '');
        return;
      }
      items.forEach((s) => {
        const li = document.createElement('li');
        const label = (s.device_type || s.device || s.user_agent || 'جلسة');
        li.innerHTML =
          '<div class="avatar-sm">' + escape(initials(label)) + '</div>' +
          '<div class="row-body">' +
            '<div class="row-title">' + escape(String(label).slice(0,36)) + '</div>' +
            '<div class="row-sub mono">' +
              escape(s.remote_addr || s.ip || '—') +
              ' · ' + fmtRel(s.last_seen || s.issued_at || s.created_at) +
            '</div>' +
          '</div>' +
          '<div class="row-actions">' +
            '<button class="danger" data-revoke>إبطال</button>' +
          '</div>';
        li.querySelector('[data-revoke]').addEventListener('click', async (e) => {
          e.stopPropagation();
          if (!confirm('إبطال هذه الجلسة؟')) return;
          try {
            await Api.del('/api/admin/users/' +
                          encodeURIComponent(user.id) +
                          '/sessions/' + encodeURIComponent(s.id || s.session_id));
            toast('تم الإبطال', 'ok');
            loadSessions(user);
          } catch (err) { toast(err.message, 'err'); }
        });
        $('sessionsList').appendChild(li);
      });
      setText($('sessionsStatus'), items.length + ' جلسة', 'ok');
    } catch (e) {
      setText($('sessionsStatus'), e.message, 'err');
    }
  }
  $('btnRevokeAll').addEventListener('click', async () => {
    if (!_currentUser) return;
    if (!confirm('إبطال جميع جلسات هذا المستخدم؟ سيخرج من كل الأجهزة.')) return;
    try {
      await Api.post('/api/admin/users/' +
                     encodeURIComponent(_currentUser.id) +
                     '/sessions/revoke-all');
      toast('تم إبطال الجميع', 'ok');
      loadSessions(_currentUser);
    } catch (e) { setText($('sessionsStatus'), e.message, 'err'); }
  });

  // ── Network ─────────────────────────────────────────────────
  async function loadNetwork() {
    // Tunnel / connectivity.
    try {
      const c = await Api.get('/api/admin/connectivity');
      const t = (c && c.tunnel) || {};
      const metrics = document.querySelectorAll('#tunnelMetrics [data-t]');
      const map = {
        status: t.status || (t.connected ? 'متصل' : 'غير متصل'),
        url:    t.public_url || t.url || '—',
        hb:     t.last_heartbeat ? fmtRel(t.last_heartbeat) : '—',
      };
      metrics.forEach((el) => { el.textContent = map[el.dataset.t] || '—'; });
    } catch (e) {
      document.querySelectorAll('#tunnelMetrics [data-t]').forEach((el) => {
        el.textContent = '—';
      });
    }
    // Federation bridges.
    try {
      const b = await Api.get('/api/admin/federation/bridges');
      const list = $('bridgesList');
      list.innerHTML = '';
      const items = Array.isArray(b) ? b : (b.bridges || b.items || []);
      if (!items.length) {
        list.innerHTML = '<li class="empty-row">لا توجد جسور نشطة.</li>';
      } else {
        items.forEach((br) => {
          const li = document.createElement('li');
          li.innerHTML =
            '<div class="avatar-sm">' + escape(initials(br.name || br.peer || 'B')) + '</div>' +
            '<div class="row-body">' +
              '<div class="row-title">' + escape(br.name || br.peer || '—') + '</div>' +
              '<div class="row-sub mono">in=' + (br.msg_in ?? 0) +
                ' / out=' + (br.msg_out ?? 0) + '</div>' +
            '</div>';
          list.appendChild(li);
        });
      }
    } catch (e) {
      $('bridgesList').innerHTML = '<li class="empty-row">' + escape(e.message) + '</li>';
    }
    // Diagnostics.
    try {
      const d = await Api.get('/api/admin/diagnostics/network');
      const panel = $('diagMetrics');
      panel.innerHTML = '';
      const pairs = [];
      if (d.host) pairs.push(['المضيف', d.host]);
      if (d.interfaces) pairs.push(['واجهات', String(d.interfaces.length || 0)]);
      if (d.external_ip) pairs.push(['IP خارجي', d.external_ip]);
      if (d.gateway) pairs.push(['البوابة', d.gateway]);
      if (d.dns) pairs.push(['DNS', (d.dns[0] || '—')]);
      if (d.latency_ms != null) pairs.push(['الكمون', d.latency_ms + 'ms']);
      if (!pairs.length) {
        panel.innerHTML =
          '<div class="metric-row"><span class="metric-key">—</span>' +
          '<span class="metric-val">لا توجد بيانات</span></div>';
        return;
      }
      pairs.forEach(([k,v]) => {
        const row = document.createElement('div');
        row.className = 'metric-row';
        row.innerHTML = '<span class="metric-key">' + escape(k) + '</span>' +
                        '<span class="metric-val">' + escape(String(v)) + '</span>';
        panel.appendChild(row);
      });
    } catch (e) {
      $('diagMetrics').innerHTML =
        '<div class="metric-row"><span class="metric-key">خطأ</span>' +
        '<span class="metric-val">' + escape(e.message) + '</span></div>';
    }
  }
  $('networkRefresh').addEventListener('click', loadNetwork);

  // ── Tunnel configure/disable ────────────────────────────────
  $('btnTunnelToggle').addEventListener('click', async () => {
    let cur = {};
    try {
      const c = await Api.get('/api/admin/connectivity');
      cur = (c && c.tunnel) || {};
    } catch (_e) { /* ignore */ }
    if (cur.configured || cur.connected) {
      if (!confirm('تعطيل النفق وإغلاقه؟')) return;
      try {
        await Api.del('/api/admin/connectivity/tunnel');
        toast('تم تعطيل النفق', 'ok');
        loadNetwork();
      } catch (e) { toast(e.message, 'err'); }
      return;
    }
    const vals = await openSheet({
      title: 'تكوين نفق Rendezvous',
      bodyHtml:
        '<input type="text" data-field="ws_url" ' +
          'placeholder="ws://rendezvous.example:9090/tunnel/register" />' +
        '<input type="text" data-field="token" ' +
          'placeholder="رمز المصادقة (إن وجد)" />' +
        '<input type="text" data-field="display_name" ' +
          'placeholder="اسم العرض (اختياري)" />',
      confirmLabel: 'تفعيل',
    });
    if (!vals || !vals.ws_url) return;
    try {
      await Api.post('/api/admin/connectivity/tunnel', {
        ws_url: vals.ws_url,
        token: vals.token || undefined,
        display_name: vals.display_name || undefined,
      });
      toast('تم تفعيل النفق', 'ok');
      loadNetwork();
    } catch (e) { toast(e.message, 'err'); }
  });

  // ── Router fix (UPnP/NAT-PMP full_fix) ──────────────────────
  $('btnRouterFix').addEventListener('click', async () => {
    if (!confirm('محاولة فتح المنفذ تلقائياً عبر UPnP / NAT-PMP؟')) return;
    setText($('networkActionStatus'), 'جارٍ المحاولة…');
    try {
      const r = await Api.post('/api/admin/connectivity/router/apply',
                               { action: 'full_fix' });
      setText($('networkActionStatus'),
              r.message || r.detail || 'تمت المحاولة', 'ok');
      toast('تم إصلاح الراوتر', 'ok');
      loadNetwork();
    } catch (e) {
      setText($('networkActionStatus'), e.message, 'err');
    }
  });

  // ── Backups ─────────────────────────────────────────────────
  async function loadBackups() {
    setText($('backupsStatus'), 'جارٍ التحميل…');
    try {
      const r = await Api.get('/api/admin/backups');
      const items = Array.isArray(r) ? r : (r.backups || r.items || []);
      const list = $('backupsList');
      list.innerHTML = '';
      if (!items.length) {
        list.innerHTML = '<li class="empty-row">لا توجد نسخ احتياطية بعد.</li>';
        setText($('backupsStatus'), '');
        return;
      }
      items.forEach((b) => {
        const li = document.createElement('li');
        const name = b.name || '—';
        li.innerHTML =
          '<div class="avatar-sm">' + escape(initials(name)) + '</div>' +
          '<div class="row-body">' +
            '<div class="row-title">' + escape(name) + '</div>' +
            '<div class="row-sub mono">' + fmtBytes(b.size ?? b.size_bytes) +
              ' · ' + fmtRel(b.created_at) + '</div>' +
          '</div>' +
          '<div class="row-actions">' +
            '<button data-v="verify">فحص</button>' +
            '<button data-v="restore">استرجاع</button>' +
            '<button data-v="delete" class="danger">حذف</button>' +
          '</div>';
        li.querySelector('[data-v="verify"]').addEventListener('click', async (e) => {
          e.stopPropagation();
          try {
            const r = await Api.post('/api/admin/backups/' +
                                     encodeURIComponent(name) + '/verify');
            toast(r.ok ? 'النسخة سليمة' :
                         (r.reason || 'فشل الفحص'),
                  r.ok ? 'ok' : 'err');
          } catch (err) { toast(err.message, 'err'); }
        });
        li.querySelector('[data-v="restore"]').addEventListener('click', async (e) => {
          e.stopPropagation();
          if (!confirm('استرجاع ' + name + '؟ ستُستبدل قاعدة البيانات الحالية.')) return;
          try {
            await Api.post('/api/admin/backups/' +
                           encodeURIComponent(name) + '/restore');
            toast('تم الاسترجاع', 'ok');
          } catch (err) { toast(err.message, 'err'); }
        });
        li.querySelector('[data-v="delete"]').addEventListener('click', async (e) => {
          e.stopPropagation();
          if (!confirm('حذف ' + name + ' نهائياً؟')) return;
          try {
            await Api.del('/api/admin/backups/' + encodeURIComponent(name));
            toast('تم الحذف', 'ok');
            loadBackups();
          } catch (err) { toast(err.message, 'err'); }
        });
        list.appendChild(li);
      });
      setText($('backupsStatus'), '');
    } catch (e) {
      setText($('backupsStatus'), e.message, 'err');
    }
  }
  $('backupRun').addEventListener('click', async () => {
    if (!confirm('بدء نسخ احتياطي الآن؟')) return;
    try {
      await Api.post('/api/admin/backups/run-now');
      toast('تم بدء النسخ', 'ok');
      setTimeout(loadBackups, 1500);
    } catch (e) {
      toast(e.message, 'err');
    }
  });

  // ── More tab + sub-screens ──────────────────────────────────
  async function loadMore() {
    $('logoutUser').textContent =
      (Store.user && (Store.user.display_name || Store.user.username)) || '—';
    // Populate badges.
    Promise.all([
      Api.get('/api/admin/connected-clients').then((r) => {
        const items = Array.isArray(r) ? r : (r.clients || r.items || []);
        $('clientsBadge').textContent = String(items.length);
      }).catch(() => { $('clientsBadge').textContent = '—'; }),
      Api.get('/api/admin/active-calls').then((r) => {
        const items = Array.isArray(r) ? r : (r.calls || r.items || []);
        $('callsBadge').textContent = String(items.length);
      }).catch(() => { $('callsBadge').textContent = '—'; }),
      Api.get('/api/admin/dlq/stats').then((r) => {
        const pending = (r && r.by_status && r.by_status.pending) ||
                        r.pending || 0;
        $('dlqBadge').textContent = String(pending);
      }).catch(() => { $('dlqBadge').textContent = '—'; }),
    ]);
  }
  document.querySelectorAll('[data-more]').forEach((li) => {
    li.addEventListener('click', () => {
      const dest = li.dataset.more;
      if (dest === 'clients')    { loadClients();    show('clients'); }
      else if (dest === 'calls')      { loadCalls();      show('calls'); }
      else if (dest === 'audit')      { loadAudit();      show('audit'); }
      else if (dest === 'dlq')        { loadDlq();        show('dlq'); }
      else if (dest === 'federation') { loadFederation(); show('federation'); }
      else if (dest === 'roles')      { loadRoles();     show('roles'); }
      else if (dest === 'logout') {
        if (!confirm('تسجيل الخروج؟')) return;
        Store.clear();
        localStorage.removeItem('helen.admin.serverUrl');
        toast('تم الخروج', 'ok');
        show('auth', { onEnter: enterAuth });
      }
    });
  });

  // ── Live clients ────────────────────────────────────────────
  async function loadClients() {
    setText($('clientsStatus'), 'جارٍ التحميل…');
    $('clientsList').innerHTML = '';
    try {
      const r = await Api.get('/api/admin/connected-clients');
      const items = Array.isArray(r) ? r : (r.clients || r.items || []);
      if (!items.length) {
        $('clientsList').innerHTML =
          '<li class="empty-row">لا يوجد عملاء متصلون.</li>';
        setText($('clientsStatus'), '');
        return;
      }
      items.forEach((c) => {
        const name = c.username || c.display_name || c.user_id || 'عميل';
        const li = document.createElement('li');
        li.innerHTML =
          '<div class="avatar-sm">' + escape(initials(name)) + '</div>' +
          '<div class="row-body">' +
            '<div class="row-title">' + escape(name) + '</div>' +
            '<div class="row-sub mono">' +
              escape(c.remote_addr || c.ip || '—') +
              ' · ' + escape(c.device_type || 'device') +
              ' · ' + fmtRel(c.connected_at || c.last_connect) +
            '</div>' +
          '</div>' +
          '<span class="row-time">' + escape(c.transport || 'ws') + '</span>';
        $('clientsList').appendChild(li);
      });
      setText($('clientsStatus'), items.length + ' متصل', 'ok');
    } catch (e) { setText($('clientsStatus'), e.message, 'err'); }
  }

  // ── Active calls ────────────────────────────────────────────
  async function loadCalls() {
    setText($('callsStatus'), 'جارٍ التحميل…');
    $('callsList').innerHTML = '';
    try {
      const r = await Api.get('/api/admin/active-calls');
      const items = Array.isArray(r) ? r : (r.calls || r.items || []);
      if (!items.length) {
        $('callsList').innerHTML =
          '<li class="empty-row">لا توجد مكالمات نشطة.</li>';
        setText($('callsStatus'), '');
        return;
      }
      items.forEach((c) => {
        const li = document.createElement('li');
        const label = c.channel_name || c.channel_id || c.id || 'مكالمة';
        const n = (c.participants || c.participant_ids || []).length;
        li.innerHTML =
          '<div class="avatar-sm">📞</div>' +
          '<div class="row-body">' +
            '<div class="row-title">' + escape(label) + '</div>' +
            '<div class="row-sub mono">' + n + ' مشارك · ' +
              escape(c.kind || c.type || 'voice') +
              ' · ' + fmtRel(c.started_at || c.created_at) +
            '</div>' +
          '</div>' +
          '<span class="row-time">' + escape((c.status || '').toUpperCase()) + '</span>';
        $('callsList').appendChild(li);
      });
      setText($('callsStatus'), items.length + ' مكالمة', 'ok');
    } catch (e) { setText($('callsStatus'), e.message, 'err'); }
  }

  // ── Audit log ───────────────────────────────────────────────
  async function loadAudit() {
    setText($('auditStatus'), 'جارٍ التحميل…');
    $('auditList').innerHTML = '';
    try {
      const r = await Api.get('/api/admin/audit-logs?limit=50');
      const items = Array.isArray(r)
        ? r : (r.results || r.logs || r.items || r.entries || []);
      if (!items.length) {
        $('auditList').innerHTML =
          '<li class="empty-row">لا توجد أحداث.</li>';
        setText($('auditStatus'), '');
        return;
      }
      items.forEach((a) => {
        const ok = a.success !== false;
        const when = a.occurred_at || a.timestamp || a.created_at || a.at;
        const who = a.username || a.user_id || a.ip_address || '—';
        const details = a.details || a.detail;
        const detailSummary = details
          ? (typeof details === 'object'
             ? (details.reason || Object.keys(details)[0] || '')
             : String(details))
          : '';
        const li = document.createElement('li');
        li.innerHTML =
          '<div class="avatar-sm" style="background:' +
            (ok ? 'rgba(92,215,165,0.2); color:#5cd7a5' :
                  'rgba(255,80,99,0.2); color:#ff7a8e') + ';">' +
            (ok ? '✓' : '✗') + '</div>' +
          '<div class="row-body">' +
            '<div class="row-title">' + escape(a.event || a.action || '—') + '</div>' +
            '<div class="row-sub mono">' +
              escape(String(who).slice(0,14)) +
              ' · ' + fmtRel(when) +
              (detailSummary ? ' · ' + escape(detailSummary.slice(0,30)) : '') +
            '</div>' +
          '</div>';
        $('auditList').appendChild(li);
      });
      setText($('auditStatus'), items.length + ' حدث', 'ok');
    } catch (e) { setText($('auditStatus'), e.message, 'err'); }
  }

  // ── DLQ ─────────────────────────────────────────────────────
  async function loadDlq() {
    setText($('dlqStatus'), 'جارٍ التحميل…');
    $('dlqList').innerHTML = '';
    // Stats
    try {
      const s = await Api.get('/api/admin/dlq/stats');
      const pending = (s.by_status && s.by_status.pending) || s.pending || 0;
      const replayed = (s.by_status && s.by_status.replayed) || s.replayed || 0;
      const t1 = document.querySelector('[data-kpi="dlq-pending"] [data-slot="value"]');
      const t2 = document.querySelector('[data-kpi="dlq-replayed"] [data-slot="value"]');
      if (t1) t1.textContent = String(pending);
      if (t2) t2.textContent = String(replayed);
    } catch (_e) { /* ignore — list below still useful */ }
    // Entries.
    try {
      const r = await Api.get('/api/admin/dlq?limit=30');
      const items = Array.isArray(r)
        ? r : (r.results || r.entries || r.items || []);
      if (!items.length) {
        $('dlqList').innerHTML =
          '<li class="empty-row">الطابور فارغ.</li>';
        setText($('dlqStatus'), '');
        return;
      }
      items.forEach((d) => {
        const li = document.createElement('li');
        li.innerHTML =
          '<div class="avatar-sm" style="background:rgba(255,92,122,0.2); color:#ff7a8e;">📮</div>' +
          '<div class="row-body">' +
            '<div class="row-title">' + escape(d.kind || d.event || '—') + '</div>' +
            '<div class="row-sub mono">' +
              escape((d.status || '—').toUpperCase()) +
              ' · ' + fmtRel(d.created_at || d.at) +
              ' · محاولات: ' + (d.attempt_count ?? 0) +
            '</div>' +
          '</div>' +
          '<div class="row-actions">' +
            '<button data-v="replay">إعادة</button>' +
            '<button data-v="abandon" class="danger">إلغاء</button>' +
          '</div>';
        li.querySelector('[data-v="replay"]').addEventListener('click', async (e) => {
          e.stopPropagation();
          try {
            await Api.post('/api/admin/dlq/' + encodeURIComponent(d.id) + '/replay');
            toast('أُعيدت', 'ok'); loadDlq();
          } catch (err) { toast(err.message, 'err'); }
        });
        li.querySelector('[data-v="abandon"]').addEventListener('click', async (e) => {
          e.stopPropagation();
          if (!confirm('وسم الرسالة كمرفوضة؟')) return;
          try {
            await Api.post('/api/admin/dlq/' + encodeURIComponent(d.id) + '/abandon',
                           { note: 'admin_mobile' });
            toast('تم', 'ok'); loadDlq();
          } catch (err) { toast(err.message, 'err'); }
        });
        $('dlqList').appendChild(li);
      });
      setText($('dlqStatus'), items.length + ' رسالة في الطابور', 'ok');
    } catch (e) { setText($('dlqStatus'), e.message, 'err'); }
  }

  // ── Server Roles ────────────────────────────────────────────
  // Structural roles (always on, locked), Toggle roles (boolean),
  // Policy (enum), Thresholds (integer steppers).
  const ROLE_GROUPS = {
    structural: ['auth', 'signaling', 'messaging', 'presence',
                 'database', 'admin'],
    toggle:     ['sfu', 'relay', 'recording', 'file_transfer',
                 'metrics', 'federation', 'auto_degrade'],
    policy:     ['policy_mode', 'sfu_max_participants'],
    thresholds: ['cpu_downshift_pct', 'loss_audio_pct', 'loss_chat_pct'],
  };
  const ROLE_LABELS_AR = {
    auth: 'المصادقة', signaling: 'الإشارات', messaging: 'الرسائل',
    presence: 'الحضور', database: 'قاعدة البيانات', admin: 'الإدارة',
    sfu: 'SFU (فيديو مجموعات)', relay: 'Relay (تجاوز NAT)',
    recording: 'التسجيل', file_transfer: 'نقل الملفات',
    metrics: 'القياسات', federation: 'الفيدرالية',
    auto_degrade: 'التدهور التلقائي',
    policy_mode: 'وضع السياسة', sfu_max_participants: 'حد مشتركي SFU',
    cpu_downshift_pct: 'CPU%% للتخفيض', loss_audio_pct: 'فقد%% للصوت فقط',
    loss_chat_pct: 'فقد%% للنص فقط',
  };
  const POLICY_LABELS_AR = {
    auto: 'تلقائي', chat_only: 'نص فقط', audio_only: 'صوت فقط',
    video_ok: 'فيديو مسموح', no_sfu_p2p_only: 'P2P فقط',
    no_relay: 'بلا Relay',
  };
  let _rolesState = null;

  async function loadRoles() {
    setText($('rolesStatus'), 'جارٍ التحميل…');
    try {
      const r = await Api.get('/api/admin/server-roles');
      _rolesState = (r && r.roles) || {};
      renderRoleGroups();
      setText($('rolesStatus'), '');
      const offCount = ROLE_GROUPS.toggle.filter((k) =>
        _rolesState[k] && !_rolesState[k].enabled).length;
      const rb = $('rolesBadge');
      if (rb) rb.textContent = offCount ? (offCount + ' مُعطّل') : 'كلها فعّالة';
    } catch (e) {
      setText($('rolesStatus'), e.message, 'err');
    }
  }

  function renderRoleGroups() {
    renderRoleList('rolesStructural', ROLE_GROUPS.structural);
    renderRoleList('rolesToggle',     ROLE_GROUPS.toggle);
    renderPolicyList('rolesPolicy',   ROLE_GROUPS.policy);
    renderPolicyList('rolesThresholds', ROLE_GROUPS.thresholds);
  }
  function renderRoleList(containerId, keys) {
    const box = $(containerId);
    box.innerHTML = '';
    for (const k of keys) {
      const r = _rolesState[k];
      if (!r) continue;
      const row = document.createElement('div');
      row.className = 'role-row';
      const label = ROLE_LABELS_AR[k] || k;
      row.innerHTML =
        '<div class="role-body">' +
          '<div class="role-title">' + escape(label) + '</div>' +
          '<div class="role-desc">' + escape(r.desc || '') + '</div>' +
        '</div>' +
        (r.locked
          ? '<span class="role-locked">ثابت</span>'
          : '<label class="toggle"><input type="checkbox" ' +
              (r.enabled ? 'checked' : '') +
              ' data-role="' + k + '">' +
              '<span class="slider"></span></label>');
      if (!r.locked) {
        row.querySelector('input').addEventListener('change', (ev) => {
          saveRolePatch({ [k]: { enabled: ev.target.checked } });
        });
      }
      box.appendChild(row);
    }
  }
  function renderPolicyList(containerId, keys) {
    const box = $(containerId);
    box.innerHTML = '';
    for (const k of keys) {
      const r = _rolesState[k];
      if (!r) continue;
      const row = document.createElement('div');
      row.className = 'role-row';
      const label = ROLE_LABELS_AR[k] || k;
      let control = '';
      if (r.options) {
        control = '<select class="role-select" data-role="' + k + '">' +
          r.options.map((o) =>
            '<option value="' + escape(o) + '"' +
            (o === r.value ? ' selected' : '') + '>' +
            escape(POLICY_LABELS_AR[o] || o) + '</option>').join('') +
          '</select>';
      } else if (r.min != null || r.max != null) {
        control = '<div class="role-stepper" data-role="' + k + '">' +
          '<button data-step="-1">−</button>' +
          '<span class="val">' + r.value + '</span>' +
          '<button data-step="+1">+</button>' +
          '</div>';
      }
      row.innerHTML =
        '<div class="role-body">' +
          '<div class="role-title">' + escape(label) + '</div>' +
          '<div class="role-desc">' + escape(r.desc || '') + '</div>' +
        '</div>' +
        control;
      box.appendChild(row);
      if (r.options) {
        row.querySelector('select').addEventListener('change', (ev) => {
          saveRolePatch({ [k]: { value: ev.target.value } });
        });
      } else if (r.min != null || r.max != null) {
        row.querySelectorAll('button[data-step]').forEach((b) => {
          b.addEventListener('click', () => {
            const delta = b.dataset.step === '-1' ? -1 : 1;
            const stepSize = (r.max - r.min) >= 50 ? 5 : 1;
            let nv = r.value + delta * stepSize;
            if (r.min != null) nv = Math.max(r.min, nv);
            if (r.max != null) nv = Math.min(r.max, nv);
            saveRolePatch({ [k]: { value: nv } });
          });
        });
      }
    }
  }
  async function saveRolePatch(updates) {
    setText($('rolesStatus'), 'جارٍ الحفظ…');
    try {
      const r = await Api.patch('/api/admin/server-roles',
                                { updates });
      _rolesState = (r && r.roles) || _rolesState;
      renderRoleGroups();
      setText($('rolesStatus'), 'تم الحفظ', 'ok');
      toast('تم التحديث', 'ok');
    } catch (e) {
      setText($('rolesStatus'), e.message, 'err');
      toast(e.message, 'err');
      // Reload to revert UI to server truth
      loadRoles();
    }
  }

  // ── Federation ──────────────────────────────────────────────
  async function loadFederation() {
    // Status.
    try {
      const st = await Api.get('/api/admin/federation/status');
      const panel = $('fedStatusMetrics');
      panel.innerHTML = '';
      const peerCount = st.peers_live != null
        ? st.peers_live
        : ((st.peers && st.peers.length) || st.peer_count || 0);
      const relayCount = Array.isArray(st.relay_sessions)
        ? st.relay_sessions.length
        : (st.relay_sessions || 0);
      const rows = [
        ['مُفعّل', st.enabled ? 'نعم' : 'لا'],
        ['المعرّف', (st.server_id || '—').slice(0, 16) + '…'],
        ['سرّ موجود', st.has_secret ? 'نعم' : 'لا'],
        ['أقران نشطون', String(peerCount)],
        ['جلسات التحويل', String(relayCount)],
      ];
      rows.forEach(([k,v]) => {
        const row = document.createElement('div');
        row.className = 'metric-row';
        row.innerHTML = '<span class="metric-key">' + escape(k) + '</span>' +
                        '<span class="metric-val">' + escape(String(v)) + '</span>';
        panel.appendChild(row);
      });
    } catch (e) {
      $('fedStatusMetrics').innerHTML =
        '<div class="metric-row"><span class="metric-key">خطأ</span>' +
        '<span class="metric-val">' + escape(e.message) + '</span></div>';
    }
    // Metrics.
    try {
      const m = await Api.get('/api/admin/federation/metrics');
      const panel = $('fedMetricsPanel');
      panel.innerHTML = '';
      Object.entries(m).slice(0, 10).forEach(([k, v]) => {
        if (typeof v === 'object') return;
        const row = document.createElement('div');
        row.className = 'metric-row';
        row.innerHTML = '<span class="metric-key">' + escape(k) + '</span>' +
                        '<span class="metric-val">' + escape(String(v)) + '</span>';
        panel.appendChild(row);
      });
      if (!panel.children.length) {
        panel.innerHTML =
          '<div class="metric-row"><span class="metric-key">—</span>' +
          '<span class="metric-val">لا توجد مقاييس</span></div>';
      }
    } catch (_e) { /* ignore */ }
    // Events.
    try {
      const ev = await Api.get('/api/admin/federation/events?limit=20');
      const items = Array.isArray(ev) ? ev : (ev.events || ev.items || []);
      const list = $('fedEventsList');
      list.innerHTML = '';
      if (!items.length) {
        list.innerHTML = '<li class="empty-row">لا توجد أحداث حديثة.</li>';
        return;
      }
      items.forEach((e) => {
        const li = document.createElement('li');
        li.innerHTML =
          '<div class="avatar-sm">🌐</div>' +
          '<div class="row-body">' +
            '<div class="row-title">' + escape(e.event || e.kind || '—') + '</div>' +
            '<div class="row-sub mono">' +
              escape(e.peer || e.source || '—') +
              ' · ' + fmtRel(e.at || e.timestamp) + '</div>' +
          '</div>';
        list.appendChild(li);
      });
    } catch (_e) { /* ignore */ }
  }

  // ── Boot ────────────────────────────────────────────────────
  function boot() {
    const qs = new URLSearchParams(location.search);
    // Optional ?server= override (for manual testing).
    const override = qs.get('server');
    if (override) Store.serverUrl = override;
    // Reset on explicit ?reset.
    if (qs.get('reset') === '1') {
      Store.clear();
      localStorage.removeItem('helen.admin.serverUrl');
    }

    // Screenshot harness: preload token + user via URL. Non-production
    // use — lets the harness skip auth and capture "logged-in" screens.
    const boot_token = qs.get('token');
    const boot_user  = qs.get('user');
    if (boot_token) {
      Store.token = boot_token;
      if (boot_user) {
        try { Store.user = JSON.parse(atob(boot_user)); }
        catch { Store.user = { username: boot_user }; }
      }
    }

    // Direct screen override for screenshot harness.
    const scr = qs.get('screen');
    if (scr && _screens[scr]) {
      const loaders = {
        overview:   loadOverview,
        users:      loadUsers,
        network:    loadNetwork,
        backups:    loadBackups,
        more:       loadMore,
        clients:    loadClients,
        calls:      loadCalls,
        audit:      loadAudit,
        dlq:        loadDlq,
        federation: loadFederation,
        roles:      loadRoles,
      };
      if (loaders[scr]) loaders[scr]();
      show(scr);
      return;
    }

    if (Store.token && Store.user) {
      show('overview');
      loadOverview();
    } else {
      // When served from Helen itself, serverUrl is already set via
      // config.js → location.origin. Otherwise we keep the default.
      if (location.pathname.indexOf('/admin-mobile/') === 0 &&
          !localStorage.getItem('helen.admin.serverUrl')) {
        Store.serverUrl = location.origin;
      }
      show('auth', { onEnter: enterAuth });
    }
  }
  boot();
})();
