/*
  Helen Mobile — app logic.

  Intentionally framework-free so the whole client is ~500 lines of
  readable JavaScript. Structure:

    1. Store           state + localStorage persistence
    2. Api             fetch + socket.io helpers
    3. Router          screen visibility + tab bar
    4. Screens         per-screen wire-up (onboarding, auth, chats,
                       chat, network, profile)
    5. Toast + status  small helpers
    6. Boot            initial route based on persisted state

  Everything talks to Helen over REST + Socket.IO, same contract as
  the Electron client. This is a separate client — no assumptions
  about being hosted by Helen, so it works from any static server.
*/

(function () {
  'use strict';

  // ── 1. Store ──────────────────────────────────────────────────

  const Store = {
    get serverUrl()  { return localStorage.getItem('helen.serverUrl') || window.HELEN_BASE; },
    set serverUrl(v) { v ? localStorage.setItem('helen.serverUrl', v)
                          : localStorage.removeItem('helen.serverUrl'); },
    get token()      { return localStorage.getItem('helen.token') || ''; },
    set token(v)     { v ? localStorage.setItem('helen.token', v)
                          : localStorage.removeItem('helen.token'); },
    get user()       {
      try { return JSON.parse(localStorage.getItem('helen.user') || 'null'); }
      catch { return null; }
    },
    set user(v) {
      if (v) localStorage.setItem('helen.user', JSON.stringify(v));
      else localStorage.removeItem('helen.user');
    },
    get activeChannel() { return localStorage.getItem('helen.activeChannel') || ''; },
    set activeChannel(v) { v ? localStorage.setItem('helen.activeChannel', v)
                              : localStorage.removeItem('helen.activeChannel'); },
    // Per-account "deleted from my view" channel IDs.
    // The server-side archive is set in parallel for persistence; this is
    // what the client filters against to keep removals consistent across
    // reloads even when the server doesn't surface per-user archive flags
    // in /api/channels.
    get hiddenChannels() {
      const u = (this.user && this.user.id) || 'anon';
      try { return new Set(JSON.parse(localStorage.getItem('helen.hidden.' + u) || '[]')); }
      catch { return new Set(); }
    },
    hideChannel(id) {
      const u = (this.user && this.user.id) || 'anon';
      const s = this.hiddenChannels; s.add(id);
      localStorage.setItem('helen.hidden.' + u, JSON.stringify([...s]));
    },
    unhideChannel(id) {
      const u = (this.user && this.user.id) || 'anon';
      const s = this.hiddenChannels; s.delete(id);
      localStorage.setItem('helen.hidden.' + u, JSON.stringify([...s]));
    },
    clear() {
      localStorage.removeItem('helen.token');
      localStorage.removeItem('helen.user');
      localStorage.removeItem('helen.activeChannel');
    },
  };

  // ── 2. Api ────────────────────────────────────────────────────

  const Api = {
    async request(method, path, body) {
      const headers = { 'Content-Type': 'application/json' };
      if (Store.token) headers.Authorization = 'Bearer ' + Store.token;
      let resp;
      try {
        resp = await fetch(Store.serverUrl + path, {
          method, headers,
          body: body ? JSON.stringify(body) : undefined,
          signal: AbortSignal.timeout ? AbortSignal.timeout(8000) : undefined,
        });
      } catch (e) {
        return { ok: false, status: 0, data: { detail: e.message } };
      }
      const text = await resp.text();
      let data;
      try { data = text ? JSON.parse(text) : null; } catch { data = text; }
      return { ok: resp.ok, status: resp.status, data };
    },
    health()        { return this.request('GET',  '/api/health'); },
    discovery()     { return this.request('GET',  '/api/discovery'); },
    register(u, d, p) { return this.request('POST','/api/auth/register',
                                            { username: u, display_name: d || u, password: p }); },
    login(u, p)     { return this.request('POST','/api/auth/login',
                                            { username: u, password: p }); },
    channels()      { return this.request('GET',  '/api/channels'); },
    createDM(uid)   { return this.request('POST','/api/channels',
                                            { type: 'dm', member_ids: [uid] }); },
    createGroup(name, desc, memberIds) {
      return this.request('POST', '/api/channels', {
        type: 'group', name, description: desc || undefined,
        member_ids: memberIds || [],
      });
    },
    searchUsers(q, limit = 50) {
      const qs = 'limit=' + limit +
                 (q ? '&search=' + encodeURIComponent(q) : '');
      return this.request('GET', '/api/users?' + qs);
    },
    userByCode(code) {
      return this.request('GET',
        '/api/users/by-code/' + encodeURIComponent(code));
    },
    /**
     * Update the current user's profile fields. Only the keys present
     * in `fields` are sent — the server treats missing keys as
     * "leave alone". Used by the Edit Profile sheet to change
     * `display_name`, `username`, `bio`, etc.
     */
    updateMe(fields) {
      return this.request('PATCH', '/api/users/me', fields);
    },
    channelDetail(cid) {
      return this.request('GET',
        '/api/channels/' + encodeURIComponent(cid));
    },
    addMember(cid, uid, role) {
      return this.request('POST',
        '/api/channels/' + encodeURIComponent(cid) + '/members',
        { user_id: uid, role: role || 'member' });
    },
    removeMember(cid, uid) {
      return this.request('DELETE',
        '/api/channels/' + encodeURIComponent(cid) +
        '/members/' + encodeURIComponent(uid));
    },
    messages(cid, limit = 50) {
      // The server exposes the message history under
      // `/api/channels/{cid}/messages`, not `/api/messages?channel_id=`.
      // The old endpoint shape returned 404 → "Could not load history".
      return this.request('GET',
        '/api/channels/' + encodeURIComponent(cid) +
        '/messages?limit=' + limit);
    },
    deleteMessage(mid) {
      return this.request('DELETE', '/api/messages/' + encodeURIComponent(mid));
    },
    editMessage(mid, content) {
      return this.request('PATCH', '/api/messages/' + encodeURIComponent(mid),
                          { content });
    },
    reactMessage(mid, emoji) {
      return this.request('POST', '/api/messages/' + encodeURIComponent(mid) + '/reactions',
                          { emoji });
    },
    pinMessage(mid) {
      return this.request('POST', '/api/messages/' + encodeURIComponent(mid) + '/pin');
    },
    unpinMessage(mid) {
      return this.request('DELETE', '/api/messages/' + encodeURIComponent(mid) + '/pin');
    },
    forwardMessage(mid, target_channel_id) {
      return this.request('POST', '/api/messages/' + encodeURIComponent(mid) + '/forward',
                          { target_channel_id });
    },
    archiveChannel(cid, archived = true) {
      return this.request('PUT', '/api/channels/' + encodeURIComponent(cid) + '/archive',
                          { archived });
    },
    muteChannel(cid, muted = true) {
      return this.request('PUT', '/api/channels/' + encodeURIComponent(cid) + '/mute',
                          { muted });
    },
    pinChannel(cid, pinned = true) {
      return this.request('PUT', '/api/channels/' + encodeURIComponent(cid) + '/pin',
                          { pinned });
    },
    async uploadFile(file, channel_id) {
      const headers = {};
      if (Store.token) headers.Authorization = 'Bearer ' + Store.token;
      const fd = new FormData();
      fd.append('file', file);
      if (channel_id) fd.append('channel_id', channel_id);
      try {
        const r = await fetch(Store.serverUrl + '/api/files/upload', {
          method: 'POST', headers, body: fd,
        });
        const data = await r.json().catch(() => ({}));
        return { ok: r.ok, status: r.status, data };
      } catch (e) {
        return { ok: false, status: 0, data: { detail: e.message } };
      }
    },
    fileUrl(fileId) {
      return Store.serverUrl + '/api/files/' + encodeURIComponent(fileId);
    },
    thumbUrl(fileId) {
      return Store.serverUrl + '/api/files/' + encodeURIComponent(fileId) + '/thumbnail';
    },
  };

  // ── Socket.IO wrapper ─────────────────────────────────────────

  let sock = null;
  let sockReady = false;

  function connectSocket() {
    if (sock || !Store.token) return;
    if (typeof io !== 'function') {
      console.warn('[socket] socket.io-client not loaded');
      return;
    }
    // Rendezvous tunnel awareness: socket.io hard-codes /socket.io/ as
    // the engine path, but a tunneled server lives under /t/<id>/. We
    // detect the tunnel pattern, split origin from prefix, and pass
    // the right path option so the WS upgrade hits the rendezvous WS
    // proxy. Same trick the Electron client uses.
    const url = Store.serverUrl;
    const tunnelMatch = url.match(/^(https?:\/\/[^/]+)(\/t\/[A-Za-z0-9_-]+)\/?$/i);
    const origin = tunnelMatch ? tunnelMatch[1] : url;
    const path   = tunnelMatch ? `${tunnelMatch[2]}/socket.io/` : '/socket.io/';
    sock = io(origin, {
      path,
      auth: { token: Store.token },
      transports: ['websocket', 'polling'],
      reconnection: true,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 8000,
    });
    sock.on('connect', () => {
      sockReady = true;
      setPresenceDot('ok');
      updateNetworkStatus('online', null);
      // Request OS-level notification permission once the user is
      // actually signed-in. Asking earlier (on page load) annoys users
      // who never log in; asking on first message is too late.
      if (typeof Notification !== 'undefined' &&
          Notification.permission === 'default' &&
          !_notifyPermissionAsked) {
        _notifyPermissionAsked = true;
        try { Notification.requestPermission().catch(() => {}); }
        catch { /* older browsers */ }
      }
    });
    sock.on('disconnect', (reason) => {
      sockReady = false;
      setPresenceDot('warn');
      updateNetworkStatus('reconnecting', reason);
      // Tear down any in-flight WebRTC. Peers can't recover after socket
      // loss because all signaling (renegotiation, ICE restart) flows
      // through this socket; leaving the pcs alive just leaks memory and
      // keeps tracks publishing to a dead path. On reconnect the call must
      // be re-established from scratch via `_activeCall = null`.
      if (_activeCall || _peers.size > 0) {
        try { _teardownWebRTC(); } catch (e) { console.warn('[rtc] teardown on disconnect failed', e); }
        _activeCall = null;
      }
    });
    sock.on('connect_error', (err) => {
      sockReady = false;
      setPresenceDot('bad');
      updateNetworkStatus('error', err.message);
    });
    for (const evt of ['v2_chat:new_message', 'v2_chat_new_message', 'new_message']) {
      sock.on(evt, onIncomingMessage);
    }

    // Live presence — the server pushes the authoritative roster on
    // connect, then per-user deltas. We only ever trust this set; the
    // `status` field on /api/users responses is a cache that can lag a
    // dropped client by minutes.
    sock.on('presence:online_list', (data) => {
      const list = data && data.online_users;
      _liveOnline = new Set();
      if (Array.isArray(list)) {
        for (const id of list) _liveOnline.add(String(id));
      } else if (list && typeof list === 'object') {
        for (const id of Object.keys(list)) _liveOnline.add(String(id));
      }
      _onPresenceChanged();
    });
    sock.on('presence:user_online', (data) => {
      if (data && data.user_id) {
        _liveOnline.add(String(data.user_id));
        _onPresenceChanged();
      }
    });
    sock.on('presence:user_offline', (data) => {
      if (data && data.user_id) {
        _liveOnline.delete(String(data.user_id));
        _onPresenceChanged();
      }
    });

    // Incoming call. The server fans this out to every member socket
    // — DM and group calls both arrive here, distinguished by the
    // presence of a `channel_id` on a group-typed channel.
    sock.on('call_incoming', (data) => {
      if (!data) return;
      _ringTone();

      // Always mark the channel as "live call in progress" so the row
      // gets a pulsing badge — even if the user is already in a
      // different call and we don't pop the overlay.
      const chId = data.channel_id || null;
      if (chId) {
        _activeChannelCalls.set(chId, {
          call_id:   data.call_id,
          mediaType: data.media_type || 'audio',
          callerId:  data.caller_id,
        });
        _updateChannelLiveBadge(chId, true);
      }

      // Don't pop a second overlay over an existing call.
      if (_activeCall) return;

      const channel = (allChannels || []).find((c) => c.id === chId);
      const isGroup = !!(channel && channel.type === 'group');
      const groupName = isGroup ? (channel.name || 'Group call') : null;

      showCallingOverlay({
        name:       isGroup
          ? groupName
          : (data.caller_name || data.caller_username || 'Unknown'),
        subtitle:   isGroup
          ? `Group call · ${data.caller_name || data.caller_username || 'someone'} started it`
          : null,
        username:   data.caller_username || null,
        shareCode:  data.caller_share_code || null,
        mediaType:  data.media_type || 'audio',
        dir:        'incoming',
        isGroup,
        channelId:  chId,
        callId:     data.call_id,
        callerId:   data.caller_id,
      });
    });
    sock.on('call_accepted', async (data) => {
      const host = document.getElementById('callingOverlay');
      if (host) {
        const s = host.querySelector('.calling-status');
        if (s) s.textContent = 'Connecting audio…';
      }
      // The caller side initiates the WebRTC offer the moment the
      // callee accepts. The remote user id we negotiate against is
      // either provided in the event or pulled from `_activeCall`.
      const remoteId = (data && data.callee_id) ||
                       (_activeCall && _activeCall.remoteUserId);
      if (!remoteId) {
        console.warn('[rtc] call_accepted but no remote id to negotiate with');
        return;
      }
      const wantVideo = _activeCall && _activeCall.mediaType === 'video';
      try {
        if (!_localStream) await _setupLocalMedia(wantVideo ? 'video' : 'audio');
        await _addPeer(remoteId, /*sendOffer*/ true);
      } catch (err) {
        console.error('[rtc] caller offer flow failed', err);
        toast('Could not start media: ' + err.message, 'err');
      }
    });
    sock.on('call_rejected', () => {
      toast('Call declined', 'err');
      _activeCall = null;
      _teardownWebRTC();
      hideCallingOverlay();
    });
    sock.on('call_hangup', (data) => {
      const chId = (data && data.channel_id) ||
                   (_activeCall && _activeCall.channelId);
      if (chId) {
        _activeChannelCalls.delete(chId);
        _updateChannelLiveBadge(chId, false);
      }
      _activeCall = null;
      _teardownWebRTC();
      hideCallingOverlay();
    });
    sock.on('call_ended', (data) => {
      // Some server paths emit `call_ended` at the end of a group call
      // when the last participant leaves.
      const chId = data && data.channel_id;
      if (chId) {
        _activeChannelCalls.delete(chId);
        _updateChannelLiveBadge(chId, false);
      }
    });

    // Mesh signaling — every event is keyed by `from_id` so we route
    // each piece of SDP / ICE to the correct peer connection. Group
    // calls with N participants run N-1 peer connections per client.
    sock.on('signal:offer', async (data) => {
      if (!data || !data.sdp || !data.from_id) return;

      // Inspect SDP for a video mline; if the offer wants video and
      // we don't have a camera up yet, attach one — same media setup
      // as the caller side, just delayed.
      const offerWantsVideo = typeof data.sdp.sdp === 'string' &&
                              /m=video \d+ /.test(data.sdp.sdp);
      if (!_localStream) {
        await _setupLocalMedia(offerWantsVideo ? 'video' : 'audio');
      }

      try {
        await _addPeer(data.from_id, /*sendOffer*/ false);
        const peer = _peers.get(data.from_id);
        if (!peer) return;
        await peer.pc.setRemoteDescription(data.sdp);
        const answer = await peer.pc.createAnswer();
        await peer.pc.setLocalDescription(answer);
        sock.emit('signal_answer', { target_id: data.from_id, sdp: answer });
      } catch (err) {
        console.error('[rtc] answer flow failed for', data.from_id, err);
        toast('Media setup failed: ' + err.message, 'err');
      }
    });
    sock.on('signal:answer', async (data) => {
      if (!data || !data.sdp || !data.from_id) return;
      const peer = _peers.get(data.from_id);
      if (!peer) return;
      try { await peer.pc.setRemoteDescription(data.sdp); }
      catch (err) { console.error('[rtc] setRemoteDescription(answer) for',
                                  data.from_id, 'failed', err); }
    });
    sock.on('signal:ice_candidate', async (data) => {
      if (!data || !data.candidate || !data.from_id) return;
      const peer = _peers.get(data.from_id);
      if (!peer) return;
      try { await peer.pc.addIceCandidate(data.candidate); }
      catch (err) { console.warn('[rtc] addIceCandidate failed for',
                                 data.from_id, err); }
    });

    // Mesh membership — server emits these for every join/leave.
    //
    // SDP-glare convention: only the *newcomer* offers. The newcomer
    // already runs `_addPeer(other, sendOffer=true)` for each
    // existing participant after their `v2_call_join_group` ack
    // returns. Existing participants must NOT offer back from this
    // event — they pre-create the pc (with the local tracks attached)
    // and wait for `signal:offer`. If both sides offered, both pcs
    // would land in `have-local-offer` and reject each other's offer
    // → no streams flow → user reports "I don't see them".
    sock.on('call_participant_joined', async (data) => {
      if (!data || !data.user_id) return;
      if (data.user_id === (Store.user && Store.user.id)) return;     // ourselves
      if (!_activeCall) return;
      console.log('[rtc] participant joined:', data.user_id.slice(0,6),
                  '— preparing pc, waiting for their offer');
      // sendOffer=false: pre-create peer + attach local tracks, wait
      // for the new joiner's `signal:offer`.
      await _addPeer(data.user_id, /*sendOffer*/ false);
    });
    sock.on('call_participant_left', (data) => {
      if (data && data.user_id) {
        console.log('[rtc] participant left:', data.user_id.slice(0,6));
        _removePeer(data.user_id);
      }
    });
  }

  // Authoritative live-online set fed by socket events. A user is only
  // ever marked online if they're in this Set — the server's socket
  // tracker is the source of truth for "actually connected right now".
  let _liveOnline = new Set();
  // Last-seen cache so we can render "Last seen X ago" even after a row
  // re-renders without a fresh /api/users call.
  const _lastSeenCache = new Map();
  // Subscribers for re-renders when presence changes.
  const _presenceSubs = new Set();
  function _onPresenceChanged() {
    for (const cb of _presenceSubs) {
      try { cb(); } catch { /* listener errors must not break the loop */ }
    }
  }

  function disconnectSocket() {
    try { sock && sock.disconnect(); } catch {}
    sock = null;
    sockReady = false;
  }

  // ── 3. Router ────────────────────────────────────────────────

  const screens = Array.from(document.querySelectorAll('[data-screen]'));
  const tabBar = document.getElementById('tabBar');

  function show(screenName, opts) {
    opts = opts || {};
    for (const s of screens) s.hidden = (s.dataset.screen !== screenName);
    // Hide the bottom tab bar on screens where it would overlap a
    // composer or detail surface. Auth/onboarding never had it; chat
    // and contacts have their own keyboards/forms at the bottom that
    // the tab bar would cover (and click-eat — see headless repro).
    const noTabBar = ['onboarding', 'auth', 'chat', 'contacts'];
    const showTabs = !noTabBar.includes(screenName);
    tabBar.hidden = !showTabs;
    // Mark tab-bar button active when we land on a top-level screen.
    const tabBtns = tabBar.querySelectorAll('.tab-btn');
    tabBtns.forEach((b) => {
      const active = (b.dataset.tab === screenName);
      b.classList.toggle('tab-active', active);
    });
    if (opts.onEnter) opts.onEnter();
  }

  // Tab-bar tap handler.
  tabBar.addEventListener('click', (ev) => {
    const btn = ev.target.closest('.tab-btn');
    if (!btn) return;
    const name = btn.dataset.tab;
    if (name === 'channels') { show('channels'); loadChannels(); }
    else if (name === 'network') { show('network'); refreshNetwork(); }
    else if (name === 'profile') { show('profile'); renderProfile(); }
    haptic();
  });

  // Nav back buttons. `data-back="<screen>"` routes explicitly; bare
  // `data-back` defaults to channels (or onboarding if not signed in).
  document.addEventListener('click', (ev) => {
    const back = ev.target.closest('[data-back]');
    if (!back) return;
    const dest = back.getAttribute('data-back');
    if (dest) { show(dest); return; }
    if (!Store.token) show('onboarding');
    else show('channels');
  });

  // ── 4. Screens ────────────────────────────────────────────────

  // 4.1 Onboarding — three connection methods.

  // Method picker (segmented control).
  const methodBtns = document.querySelectorAll('.seg-btn[data-method]');
  const methodPanes = document.querySelectorAll('.method-pane[data-pane]');
  methodBtns.forEach((b) => {
    b.addEventListener('click', () => {
      methodBtns.forEach((x) => {
        const active = x === b;
        x.classList.toggle('seg-active', active);
        x.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      const m = b.dataset.method;
      methodPanes.forEach((p) => { p.hidden = (p.dataset.pane !== m); });
    });
  });

  // -- Manual URL pane (preserved from before) --
  const onboardUrl = document.getElementById('onboardUrl');
  const onboardStatus = document.getElementById('onboardStatus');
  document.getElementById('onboardDiscover').addEventListener('click', async () => {
    const url = (onboardUrl.value || '').trim().replace(/\/+$/, '');
    if (!url) { setText(onboardStatus, 'Enter a URL first', 'err'); return; }
    setText(onboardStatus, 'Probing /api/discovery …');
    Store.serverUrl = url;
    const r = await Api.discovery();
    if (r.ok && r.data && r.data.type === 'commclient-server') {
      setText(onboardStatus,
        'Found ' + (r.data.name || 'Helen Server') + ' v' + (r.data.version || '?'),
        'ok');
    } else {
      setText(onboardStatus, 'No Helen Server at this URL (' + r.status + ')', 'err');
    }
  });
  document.getElementById('onboardContinue').addEventListener('click', () => {
    const url = (onboardUrl.value || '').trim().replace(/\/+$/, '');
    if (!url) { setText(onboardStatus, 'Enter a URL first', 'err'); return; }
    try { new URL(url); } catch {
      setText(onboardStatus, 'That is not a valid URL', 'err'); return;
    }
    Store.serverUrl = url;
    show('auth', { onEnter: enterAuth });
  });

  // -- WiFi scan pane --
  const wifiScanBtn = document.getElementById('wifiScanBtn');
  const wifiScanLabel = document.getElementById('wifiScanLabel');
  const wifiScanStatus = document.getElementById('wifiScanStatus');
  const wifiResults = document.getElementById('wifiResults');

  function buildLanCandidates() {
    // Browser can't enumerate NICs, so we probe a fixed shortlist:
    //   * mDNS-advertised hostname (works on iOS Safari + macOS via OS resolver)
    //   * loopback (handles "phone is the same machine as the server")
    //   * Apple iPhone hotspot subnet (172.20.10.x)
    //   * common consumer router /24s on .1 (the gateway) and .100-.110
    //     and .132 (the box this dev mostly tests on).
    // The list is intentionally short — each probe costs an HTTP round-
    // trip. ~30 candidates resolves in under 3 seconds with parallel
    // fetches.
    const hosts = [
      'helen.local',
      'localhost',
      '127.0.0.1',
      '192.168.1.1',  '192.168.1.10', '192.168.1.100', '192.168.1.132',
      '192.168.0.1',  '192.168.0.10', '192.168.0.100',
      '10.0.0.1',     '10.0.0.10',    '10.0.0.100',
      '10.0.1.10',
      '172.20.10.1',  '172.20.10.2',  // iPhone USB-tether
    ];
    const ports = [3000];
    const out = [];
    for (const h of hosts) for (const p of ports) out.push({ host: h, port: p });
    return out;
  }

  async function probeServer(host, port, signal) {
    const url = 'http://' + host + ':' + port;
    try {
      const r = await fetch(url + '/api/discovery', {
        signal, cache: 'no-store',
      });
      if (!r.ok) return null;
      const d = await r.json();
      if (d.type !== 'commclient-server') return null;
      return { url, name: d.name, server_id: d.server_id, version: d.version,
               https_url: d.https_url || null };
    } catch {
      return null;
    }
  }

  wifiScanBtn.addEventListener('click', async () => {
    wifiScanBtn.disabled = true;
    wifiScanLabel.textContent = 'Scanning…';
    setText(wifiScanStatus, '');
    wifiResults.innerHTML = '';
    const candidates = buildLanCandidates();
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 4000);
    let foundCount = 0;
    const seenIds = new Set();
    await Promise.all(candidates.map(async (c) => {
      const result = await probeServer(c.host, c.port, ctrl.signal);
      if (!result) return;
      if (seenIds.has(result.server_id)) return;
      seenIds.add(result.server_id);
      foundCount++;
      const li = document.createElement('li');
      li.innerHTML =
        '<div class="avatar-sm">' + initials(result.name || 'H') + '</div>' +
        '<div class="row-body">' +
          '<div class="row-title">' + escape(result.name || 'Helen Server') + '</div>' +
          '<div class="row-sub mono">' + escape(c.host + ':' + c.port) +
                                      ' · v' + escape(result.version || '?') + '</div>' +
        '</div>' +
        '<span class="row-time">FOUND</span>';
      li.addEventListener('click', () => {
        Store.serverUrl = result.url;
        haptic();
        toast('Connecting to ' + result.name, 'ok');
        show('auth', { onEnter: enterAuth });
      });
      wifiResults.appendChild(li);
    }));
    clearTimeout(timer);
    wifiScanBtn.disabled = false;
    wifiScanLabel.textContent = 'Scan again';
    if (foundCount === 0) {
      setText(wifiScanStatus,
        'No servers found. Try Bridge or Manual.', 'err');
    } else {
      setText(wifiScanStatus,
        'Found ' + foundCount + ' server' + (foundCount === 1 ? '' : 's') +
        '. Tap one to continue.', 'ok');
    }
  });

  // -- Bridge (rendezvous tunnel) pane --
  const bridgeUrl = document.getElementById('bridgeUrl');
  const bridgeStatus = document.getElementById('bridgeStatus');
  document.getElementById('bridgeContinue').addEventListener('click', async () => {
    let url = (bridgeUrl.value || '').trim().replace(/\/+$/, '');
    if (!url) { setText(bridgeStatus, 'Paste the tunnel URL', 'err'); return; }
    try { new URL(url); } catch {
      setText(bridgeStatus, 'That is not a valid URL', 'err'); return;
    }
    // Tunnel pattern: <origin>/t/<public_id>. The /api paths are
    // resolved relative to that, so we just store the URL verbatim.
    if (!/\/t\/[A-Za-z0-9_-]{6,}/i.test(url)) {
      setText(bridgeStatus,
        'URL should look like https://host/t/<public_id>', 'err');
      return;
    }
    setText(bridgeStatus, 'Verifying tunnel…');
    Store.serverUrl = url;
    const r = await Api.discovery();
    if (r.ok && r.data && r.data.type === 'commclient-server') {
      setText(bridgeStatus,
        'Tunnel reaches ' + (r.data.name || 'Helen Server'), 'ok');
      setTimeout(() => show('auth', { onEnter: enterAuth }), 600);
    } else {
      setText(bridgeStatus,
        'Tunnel did not respond (' + (r.status || 'no response') +
        '). Check the URL and try again.', 'err');
    }
  });

  // 4.2 Auth — login / register.
  const authModeBtns = document.querySelectorAll('.seg-btn[data-mode]');
  let authMode = 'login';
  const authDisplayWrap = document.getElementById('authDisplayWrap');
  const authTitle = document.getElementById('authTitle');
  const authSubmit = document.getElementById('authSubmit');
  const authStatus = document.getElementById('authStatus');
  const authServerEcho = document.getElementById('authServerEcho');

  authModeBtns.forEach((b) => {
    b.addEventListener('click', () => {
      authMode = b.dataset.mode;
      authModeBtns.forEach((x) => {
        const active = x === b;
        x.classList.toggle('seg-active', active);
        x.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      authTitle.textContent = authMode === 'login' ? 'Sign in' : 'Create account';
      authSubmit.textContent = authMode === 'login' ? 'Sign in' : 'Register & sign in';
      authDisplayWrap.hidden = (authMode !== 'register');
    });
  });

  function enterAuth() {
    authServerEcho.textContent = 'Server: ' + Store.serverUrl;
  }

  authSubmit.addEventListener('click', async () => {
    const u = (document.getElementById('authUser').value || '').trim();
    const p = document.getElementById('authPass').value || '';
    const d = (document.getElementById('authDisplay').value || '').trim();
    if (!u || !p) { setText(authStatus, 'Username and password required', 'err'); return; }
    setText(authStatus, 'Working …');
    authSubmit.disabled = true;
    try {
      if (authMode === 'register') {
        const r = await Api.register(u, d, p);
        if (!r.ok) { setText(authStatus, extractError(r), 'err'); return; }
      }
      const r2 = await Api.login(u, p);
      if (!r2.ok) { setText(authStatus, extractError(r2), 'err'); return; }
      Store.token = r2.data.tokens ? r2.data.tokens.access_token : r2.data.access_token;
      Store.user = r2.data.user;
      connectSocket();
      setText(authStatus, 'Signed in', 'ok');
      setTimeout(() => { show('channels'); loadChannels(); }, 300);
    } finally {
      authSubmit.disabled = false;
    }
  });

  // 4.3 Channels.
  const channelList = document.getElementById('channelList');
  const channelEmpty = document.getElementById('channelEmpty');
  const chanSearch = document.getElementById('chanSearch');
  let allChannels = [];

  async function loadChannels() {
    if (!Store.token) { show('onboarding'); return; }
    const r = await Api.channels();
    if (r.status === 401 || r.status === 403) {
      // Stale token after server reset / DB wipe / logout — wipe local
      // state and force back to onboarding so user gets a clean login.
      Store.clear();
      try { localStorage.removeItem('helen.serverUrl'); } catch {}
      disconnectSocket();
      toast('Session expired — please sign in again', 'err');
      setTimeout(() => show('onboarding'), 600);
      return;
    }
    if (!r.ok) {
      toast('Failed to load chats', 'err');
      return;
    }
    // Show "logged in as X" header so user always knows which account is active.
    try {
      let lbl = document.getElementById('whoAmIBadge');
      if (!lbl) {
        lbl = document.createElement('div');
        lbl.id = 'whoAmIBadge';
        lbl.style.cssText =
          'padding:6px 16px; font-size:12px; color:var(--ios-label-2,#8ea2c5); ' +
          'border-bottom:0.5px solid rgba(120,120,128,0.28);';
        const stack = document.querySelector('[data-screen="channels"] .scroll-body');
        if (stack && stack.firstChild) stack.insertBefore(lbl, stack.firstChild);
        else if (stack) stack.appendChild(lbl);
      }
      const u = Store.user || {};
      lbl.textContent = 'Signed in as ' + (u.display_name || u.username || '?') +
                       ' ' + handleShort(u);
    } catch {}
    const raw = (r.data && r.data.channels) || r.data || [];
    const hidden = Store.hiddenChannels;
    allChannels = raw.filter((c) => !hidden.has(c.id));
    renderChannels(allChannels);
  }
  function renderChannels(list) {
    channelList.innerHTML = '';
    if (!list.length) {
      channelEmpty.hidden = false;
      return;
    }
    channelEmpty.hidden = true;
    const frag = document.createDocumentFragment();
    for (const c of list) {
      const li = document.createElement('li');
      li.dataset.id = c.id;
      const badges = [];
      if (_activeChannelCalls.has(c.id)) {
        badges.push('<span class="ch-badge live" title="Live call — tap to join">🟢 Live</span>');
      }
      if (c.pinned)  badges.push('<span class="ch-badge">📌</span>');
      if (c.muted)   badges.push('<span class="ch-badge">🔕</span>');
      if (c.unread_count) badges.push('<span class="ch-badge unread">' + c.unread_count + '</span>');
      // For DMs the channel has no `name` — derive a display name from
      // the *other* participant. Otherwise the operator sees nothing
      // but a list of "(unnamed)" rows for every conversation.
      const displayName = channelDisplayName(c);
      li.innerHTML = '<div class="avatar-sm">' + initials(displayName) + '</div>' +
                     '<div class="row-body">' +
                       '<div class="row-title">' + escape(displayName) + '</div>' +
                       '<div class="row-sub">' + escape(c.type || 'chat') + '</div>' +
                     '</div>' +
                     '<div class="ch-meta">' + badges.join('') +
                       '<span class="row-time">' + formatTime(c.updated_at || c.created_at) + '</span>' +
                     '</div>' +
                     '<button class="ch-more-btn" type="button" aria-label="More" data-more-ch>' +
                       '<span></span><span></span><span></span>' +
                     '</button>';
      li.addEventListener('click', (ev) => {
        if (ev.target.closest('[data-more-ch]')) return;
        openChannel(c);
      });
      li.querySelector('[data-more-ch]').addEventListener('click', (ev) => {
        ev.stopPropagation();
        openChannelSheet(c);
      });
      _attachChannelLongPress(li, c);
      frag.appendChild(li);
    }
    channelList.appendChild(frag);
  }

  // ── Long-press still supported as backup ──────────────────────
  let _chLpTimer = null;
  function _attachChannelLongPress(node, c) {
    const start = () => {
      _chLpTimer = setTimeout(() => openChannelSheet(c), 500);
    };
    const cancel = () => { if (_chLpTimer) { clearTimeout(_chLpTimer); _chLpTimer = null; } };
    node.addEventListener('pointerdown', start);
    node.addEventListener('pointerup', cancel);
    node.addEventListener('pointerleave', cancel);
    node.addEventListener('pointermove', cancel);
    node.addEventListener('contextmenu', (ev) => {
      ev.preventDefault(); openChannelSheet(c);
    });
  }

  // ── iOS-style bottom action sheet for channel ─────────────────
  function openActionSheet(title, items) {
    closeActionSheet();
    const sheet = document.createElement('div');
    sheet.id = 'actionSheet';
    sheet.className = 'action-sheet-backdrop';
    let html = '<div class="action-sheet">';
    if (title) html += '<div class="action-sheet-title">' + escape(title) + '</div>';
    html += '<div class="action-sheet-body">';
    items.forEach((item, idx) => {
      if (item.divider) { html += '<div class="action-sheet-divider"></div>'; return; }
      html += '<button data-idx="' + idx + '" class="action-sheet-btn' +
              (item.danger ? ' danger' : '') +
              (item.bold ? ' bold' : '') + '">' +
              (item.icon ? '<span class="ash-icon">' + item.icon + '</span>' : '') +
              '<span class="ash-text">' +
                '<span class="ash-label">' + escape(item.label) + '</span>' +
                (item.sub ? '<span class="ash-sub">' + escape(item.sub) + '</span>' : '') +
              '</span></button>';
    });
    html += '</div></div>';
    html += '<button class="action-sheet-cancel" type="button">Cancel</button>';
    sheet.innerHTML = html;
    document.body.appendChild(sheet);
    sheet.querySelectorAll('.action-sheet-btn').forEach(b => {
      b.addEventListener('click', () => {
        const idx = parseInt(b.dataset.idx, 10);
        const item = items[idx];
        closeActionSheet();
        if (item && item.fn) item.fn();
      });
    });
    sheet.querySelector('.action-sheet-cancel').addEventListener('click', closeActionSheet);
    sheet.addEventListener('click', (ev) => {
      if (ev.target === sheet) closeActionSheet();
    });
  }
  function closeActionSheet() {
    const s = document.getElementById('actionSheet');
    if (s) { s.classList.add('out'); setTimeout(() => s.remove(), 220); }
  }

  function openChannelSheet(c) {
    const isDm = (c.type || '').toLowerCase() === 'dm';
    const items = [
      { icon:'💬', label:'Open chat',            fn: () => openChannel(c) },
      { divider: true },
      { icon: c.pinned ? '📌' : '📍', label: c.pinned ? 'Unpin chat' : 'Pin chat',
        sub: c.pinned ? 'Currently pinned to top' : 'Keep this chat at the top',
        fn: () => togglePin(c) },
      { icon: c.muted ? '🔔' : '🔕', label: c.muted ? 'Unmute notifications' : 'Mute notifications',
        sub: c.muted ? 'Notifications enabled' : 'Silence alerts from this chat',
        fn: () => toggleMute(c) },
      { icon:'✓',  label:'Mark all as read',      fn: () => markChannelRead(c) },
      { icon:'🔍', label:'Search in chat',         fn: () => searchInChannel(c) },
      { icon:'📋', label:'Copy share link',        sub: c.id, fn: () => copyText(c.id) },
      { divider: true },
      { icon:'🧹', label:'Clear messages',
        sub: 'Delete all messages but keep the chat',
        fn: () => clearMessages(c), danger: true },
      { icon:'📥', label:'Archive',
        sub: 'Hide from list — restore later',
        fn: () => archiveChannel(c, true) },
      { icon:'🚪', label: isDm ? 'Delete conversation' : 'Leave & delete',
        sub: isDm ? 'Removes the DM from your list' : 'You will leave the group',
        fn: () => deleteConversation(c), danger: true, bold: true },
    ];
    openActionSheet(c.name || c.type || 'Chat', items);
  }

  async function markChannelRead(c) {
    const r = await Api.request('PUT', '/api/channels/' + encodeURIComponent(c.id) + '/read');
    if (r.ok) { toast('Marked as read', 'ok'); loadChannels(); }
    else toast(extractError(r), 'err');
  }

  function searchInChannel(c) {
    openChannel(c);
    setTimeout(() => {
      const q = prompt('Search messages in ' + (c.name || 'this chat') + ':');
      if (!q) return;
      // Simple client-side filter on currently rendered messages
      const ql = q.toLowerCase();
      let hits = 0;
      for (const li of chatLog.querySelectorAll('.chat-msg')) {
        const t = (li.querySelector('.chat-body')?.textContent || '').toLowerCase();
        if (t.includes(ql)) { li.classList.add('search-hit'); hits++; }
        else li.classList.remove('search-hit');
      }
      toast(hits + ' match' + (hits===1?'':'es'), hits ? 'ok' : 'err');
    }, 300);
  }

  async function clearMessages(c) {
    if (!confirm('Delete every message in this chat? The chat itself stays.')) return;
    // Client-side clear since server doesn't expose bulk-clear: mark each message via DELETE
    setText(document.createElement('div'), '');
    const r = await Api.messages(c.id, 200);
    if (!r.ok) { toast('Failed to load messages', 'err'); return; }
    const list = (r.data && r.data.messages) || r.data || [];
    let ok = 0;
    for (const m of list) {
      if (m.sender_id !== (Store.user && Store.user.id)) continue;  // can only delete own
      const dr = await Api.deleteMessage(m.id);
      if (dr.ok || dr.status === 204) ok++;
    }
    toast('Cleared ' + ok + ' message(s) you sent', ok ? 'ok' : 'err');
    if (currentChannel && currentChannel.id === c.id) {
      // Refresh current chat view
      openChannel(c);
    }
  }
  async function togglePin(c) {
    const r = await Api.pinChannel(c.id, !c.pinned);
    if (r.ok) { toast(c.pinned ? 'Unpinned' : 'Pinned', 'ok'); loadChannels(); }
    else toast(extractError(r), 'err');
  }
  async function toggleMute(c) {
    const r = await Api.muteChannel(c.id, !c.muted);
    if (r.ok) { toast(c.muted ? 'Unmuted' : 'Muted', 'ok'); loadChannels(); }
    else toast(extractError(r), 'err');
  }
  async function archiveChannel(c, on = true) {
    const r = await Api.archiveChannel(c.id, on);
    if (r.ok) { toast('Archived', 'ok'); loadChannels(); }
    else toast(extractError(r), 'err');
  }
  async function deleteConversation(c) {
    if (!confirm('Delete this conversation? It will be removed from your list.')) return;
    if (!Store.user) return;

    // Hide locally first so the row vanishes immediately — the user's
    // expectation of "delete" is that the chat is gone NOW, not after
    // a round-trip.
    Store.hideChannel(c.id);
    if (currentChannel && currentChannel.id === c.id) {
      currentChannel = null; show('channels');
    }
    loadChannels();
    toast('Conversation removed', 'ok');

    // Mirror to the server: groups support leave-the-channel; DMs cannot
    // be left (server enforces) so we archive instead. The server-side
    // state ensures the channel stays hidden if the user reinstalls.
    const leave = await Api.removeMember(c.id, Store.user.id);
    if (!leave.ok && leave.status !== 204) {
      await Api.archiveChannel(c.id, true);
    }
  }
  chanSearch.addEventListener('input', () => {
    const q = chanSearch.value.trim().toLowerCase();
    if (!q) { renderChannels(allChannels); return; }
    renderChannels(allChannels.filter((c) =>
      (c.name || '').toLowerCase().includes(q)
    ));
  });
  document.getElementById('btnRefresh').addEventListener('click', () => {
    toast('Refreshing…');
    loadChannels();
    refreshNetwork();
    haptic();
  });
  document.getElementById('btnNewChat').addEventListener('click', () => {
    openNewChatSheet();
  });
  document.getElementById('emptyNewChat').addEventListener('click', () => {
    openNewChatSheet();
  });

  // ── New-chat action sheet ───────────────────────────────────────
  const _newChatSheet = document.getElementById('newChatSheet');
  const _codeSheet = document.getElementById('codeSheet');

  function openNewChatSheet() {
    _newChatSheet.hidden = false;
  }
  function closeSheets() {
    _newChatSheet.hidden = true;
    _codeSheet.hidden = true;
  }
  _newChatSheet.addEventListener('click', (ev) => {
    if (ev.target === _newChatSheet) { closeSheets(); return; }
    const opt = ev.target.closest('.sheet-option');
    if (!opt) return;
    const action = opt.dataset.action;
    closeSheets();
    if (action === 'dm')      openContacts('dm');
    else if (action === 'group') openNewGroup();
    else if (action === 'bycode') { _codeSheet.hidden = false;
                                    document.getElementById('codeInput').value = '';
                                    setTimeout(() => document.getElementById('codeInput').focus(), 100); }
  });
  _codeSheet.addEventListener('click', async (ev) => {
    if (ev.target === _codeSheet) { closeSheets(); return; }
    const b = ev.target.closest('[data-code-go]');
    if (!b) return;
    if (b.dataset.codeGo === 'cancel') { closeSheets(); return; }
    const code = document.getElementById('codeInput').value.trim();
    if (!code) return;
    closeSheets();
    const r = await Api.userByCode(code);
    if (!r.ok) { toast(extractError(r) || 'User not found', 'err'); return; }
    const user = r.data;
    const cr = await Api.createDM(user.id);
    if (cr.ok) {
      toast('Started chat with ' + (user.display_name || user.username), 'ok');
      loadChannels();
      openChannel(cr.data);
    } else {
      toast(extractError(cr), 'err');
    }
  });

  // ── Contacts picker (dual-mode: dm or group) ────────────────────
  const _contactList    = document.getElementById('contactList');
  const _contactSearch  = document.getElementById('contactSearch');
  const _contactsDoneBtn = document.getElementById('contactsDone');
  const _contactsTitle  = document.getElementById('contactsTitle');
  const _contactChips   = document.getElementById('contactChips');
  const _contactHint    = document.getElementById('contactHint');

  let _contactMode = 'dm';          // 'dm' | 'group'
  let _contactReturn = 'channels';  // where back-button goes
  let _selectedIds = new Map();     // id → user object (group mode only)
  let _lastContacts = [];           // last response for re-render on search

  function openContacts(mode, opts) {
    _contactMode = mode;
    _selectedIds = new Map();
    _contactSearch.value = '';
    _contactChips.hidden = (mode !== 'group');
    _contactChips.innerHTML = '';
    _contactsDoneBtn.hidden = (mode !== 'group');
    _contactReturn = (opts && opts.back) || 'channels';
    _contactsTitle.textContent =
      mode === 'group' ? 'Add members' : 'New chat';
    setText(_contactHint, '');
    // Pre-select if opts.selected.
    if (opts && opts.selected) {
      opts.selected.forEach((u) => _selectedIds.set(u.id, u));
      renderChips();
    }
    show('contacts');
    searchContacts('');
  }
  async function searchContacts(q) {
    const r = await Api.searchUsers(q);
    if (!r.ok) {
      _contactList.innerHTML = '';
      setText(_contactHint, extractError(r) || 'Search failed', 'err');
      return;
    }
    const list = (r.data && r.data.users) || r.data || [];
    _lastContacts = list;
    renderContacts(list);
    setText(_contactHint,
      list.length ? '' : 'No users found.');
  }
  function renderContacts(list) {
    _contactList.innerHTML = '';
    const frag = document.createDocumentFragment();
    const me = Store.user && Store.user.id;
    for (const u of list) {
      if (u.id === me) continue;          // skip self
      const row = document.createElement('li');
      row.className = 'contact-row';
      if (_contactMode === 'group' && _selectedIds.has(u.id)) {
        row.classList.add('selected');
      }
      row.innerHTML =
        (_contactMode === 'group'
          ? '<span class="check">✓</span>' : '') +
        '<div class="avatar-sm">' + initials(u.display_name || u.username) + '</div>' +
        '<div class="contact-body">' +
          '<div class="contact-name">' + escape(u.display_name || u.username) + '</div>' +
          '<div class="contact-handle" title="' + escape(handleFull(u)) +
            '">' + escape(handleShort(u)) + '</div>' +
          '<div class="contact-presence">' + presenceHtml(u) + '</div>' +
        '</div>';
      row.addEventListener('click', () => onContactTap(u, row));
      frag.appendChild(row);
    }
    _contactList.appendChild(frag);
  }
  function onContactTap(u, row) {
    if (_contactMode === 'dm') {
      Api.createDM(u.id).then((cr) => {
        if (cr.ok) {
          toast('Chat started', 'ok');
          loadChannels();
          openChannel(cr.data);
        } else {
          toast(extractError(cr), 'err');
        }
      });
      return;
    }
    // Group mode: toggle.
    if (_selectedIds.has(u.id)) {
      _selectedIds.delete(u.id);
      row.classList.remove('selected');
    } else {
      _selectedIds.set(u.id, u);
      row.classList.add('selected');
    }
    renderChips();
  }
  function renderChips() {
    _contactChips.innerHTML = '';
    _selectedIds.forEach((u) => {
      const c = document.createElement('span');
      c.className = 'chip';
      c.innerHTML = escape(u.display_name || u.username) +
                    ' <span class="chip-x" data-rm="' + escape(u.id) + '">×</span>';
      _contactChips.appendChild(c);
    });
  }
  _contactChips.addEventListener('click', (ev) => {
    const x = ev.target.closest('[data-rm]');
    if (!x) return;
    const id = x.dataset.rm;
    _selectedIds.delete(id);
    renderChips();
    renderContacts(_lastContacts);
  });
  _contactSearch.addEventListener('input', () => {
    const q = _contactSearch.value.trim();
    clearTimeout(_contactSearch._t);
    _contactSearch._t = setTimeout(() => searchContacts(q), 220);
  });
  _contactsDoneBtn.addEventListener('click', () => {
    // Group picker mode: return to newGroup screen with selection.
    if (_contactReturn === 'newGroup') {
      _pendingGroupMembers = Array.from(_selectedIds.values());
      renderPendingGroupMembers();
      show('newGroup');
    } else {
      show(_contactReturn);
    }
  });

  // ── New group builder ───────────────────────────────────────────
  const _groupName = document.getElementById('groupName');
  const _groupDesc = document.getElementById('groupDesc');
  const _groupMembersLbl = document.getElementById('groupMembersLbl');
  const _groupChips = document.getElementById('groupChips');
  const _groupStatus = document.getElementById('groupStatus');
  let _pendingGroupMembers = [];

  function openNewGroup() {
    _groupName.value = '';
    _groupDesc.value = '';
    _pendingGroupMembers = [];
    renderPendingGroupMembers();
    setText(_groupStatus, '');
    show('newGroup');
  }
  function renderPendingGroupMembers() {
    _groupMembersLbl.textContent =
      _pendingGroupMembers.length
        ? 'Add more (' + _pendingGroupMembers.length + ')'
        : 'Add members';
    _groupChips.innerHTML = '';
    _pendingGroupMembers.forEach((u) => {
      const c = document.createElement('span');
      c.className = 'chip';
      c.innerHTML = escape(u.display_name || u.username) +
                    ' <span class="chip-x" data-rm="' + escape(u.id) + '">×</span>';
      _groupChips.appendChild(c);
    });
  }
  _groupChips.addEventListener('click', (ev) => {
    const x = ev.target.closest('[data-rm]');
    if (!x) return;
    _pendingGroupMembers = _pendingGroupMembers.filter((u) => u.id !== x.dataset.rm);
    renderPendingGroupMembers();
  });
  document.getElementById('groupPickBtn').addEventListener('click', () => {
    openContacts('group', {
      back: 'newGroup',
      selected: _pendingGroupMembers,
    });
  });
  document.getElementById('groupCreate').addEventListener('click', async () => {
    const name = _groupName.value.trim();
    if (!name) { setText(_groupStatus, 'Group needs a name.', 'err'); return; }
    if (!_pendingGroupMembers.length) {
      setText(_groupStatus, 'Add at least one member.', 'err'); return;
    }
    setText(_groupStatus, 'Creating…');
    const r = await Api.createGroup(name, _groupDesc.value.trim(),
                                    _pendingGroupMembers.map((u) => u.id));
    if (!r.ok) {
      setText(_groupStatus, extractError(r) || 'Create failed', 'err');
      return;
    }
    toast('Group created', 'ok');
    loadChannels();
    openChannel(r.data);
  });

  // ── Channel info (members + leave + add) ────────────────────────
  const _memberList = document.getElementById('memberList');
  const _channelInfoHero = document.getElementById('channelInfoHero');
  const _channelInfoTitle = document.getElementById('channelInfoTitle');
  const _channelInfoLabel = document.getElementById('channelInfoLabel');
  const _channelInfoStatus = document.getElementById('channelInfoStatus');

  document.getElementById('chatInfoBtn').addEventListener('click', () => {
    if (!currentChannel) return;
    openChannelInfo(currentChannel);
  });
  document.getElementById('chatMoreBtn').addEventListener('click', () => {
    if (!currentChannel) return;
    openChannelSheet(currentChannel);
  });

  // ── Call buttons ──────────────────────────────────────────
  //
  // Server-side signaling uses `v2_call_initiate` / `v2_call_hangup`
  // (snake_case events) with `{target_id, media_type}` for DMs and
  // `v2_call_join_group` for groups. The previous code emitted
  // `v2_call:invite` (colon-separated) which the server didn't handle,
  // so the callee never got notified.
  //
  // Web sim is signaling-only — the actual A/V stream needs the desktop
  // client. But at least both sides now SEE the call attempt: the
  // ringing overlay on the caller, the incoming sheet on the callee.
  document.getElementById('chatVoiceBtn').addEventListener('click', () => startCall('audio'));
  document.getElementById('chatVideoBtn').addEventListener('click', () => startCall('video'));

  let _activeCall = null;       // outgoing OR accepted incoming

  // ── Real WebRTC audio for the iOS web sim ───────────────────────
  //
  // The web sim used to be signaling-only — it would emit
  // `v2_call_initiate` and show a "Connected" overlay, but no actual
  // audio ever flowed. The browser already has everything we need:
  // `getUserMedia` for the microphone, `RTCPeerConnection` for the
  // peer link, and a hidden `<audio autoplay>` element to play the
  // remote stream.
  //
  // The signaling channel is the existing `signal_offer` /
  // `signal_answer` / `signal_ice_candidate` socket events on the
  // server — same protocol the desktop client uses, so the iOS web
  // sim can call the desktop client and vice-versa.
  // Channels with an active call — keyed by channel_id so the row
  // renderer can stamp a "Live" badge regardless of whether *this*
  // user is in the call yet. Cleared on `call_hangup` / `call_ended`.
  const _activeChannelCalls = new Map();

  // Soft ring tone used both for incoming calls and for "someone
  // started a group call" notifications. Extracted into a function so
  // every entry point sounds identical.
  function _ringTone() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const o = ctx.createOscillator(); o.frequency.value = 880;
      const g = ctx.createGain(); g.gain.value = 0.05;
      o.connect(g).connect(ctx.destination);
      o.start(); o.stop(ctx.currentTime + 0.18);
      setTimeout(() => ctx.close(), 400);
    } catch { /* audio unavailable — silent ring */ }
  }

  /**
   * Add or remove the pulsing "📞 Live" badge on a channel row.
   * Idempotent — safe to call repeatedly with the same state.
   */
  function _updateChannelLiveBadge(channelId, live) {
    const li = channelList?.querySelector(`li[data-id="${channelId}"]`);
    if (!li) return;
    const meta = li.querySelector('.ch-meta');
    if (!meta) return;
    let badge = meta.querySelector('.ch-badge.live');
    if (live) {
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'ch-badge live';
        badge.title = 'Live call — tap to join';
        badge.textContent = '🟢 Live';
        meta.insertBefore(badge, meta.firstChild);
      }
    } else if (badge) {
      badge.remove();
    }
  }

  // Mesh topology — one RTCPeerConnection per remote participant. For
  // 1-to-1 DMs the map has a single entry; for group calls it grows
  // as `call_participant_joined` events arrive. Each peer is fully
  // independent: own SDP negotiation, own ICE candidates, own video
  // tile. This scales cleanly up to ~6-8 people in a mesh; beyond
  // that an SFU is required (the desktop client uses mediasoup; this
  // sim doesn't).
  const _peers = new Map();      // userId → { pc, videoEl, label }
  let _localStream  = null;
  let _localVideoEl = null;
  let _iceServers   = null;      // cached per call

  /**
   * Get or create the grid container that hosts every participant's
   * video tile — including the local camera. Treating the local user
   * as just another tile (instead of a fixed PiP corner) is what
   * makes 4 people split into 4 equal squares the way WhatsApp and
   * FaceTime do, instead of 3 squares + a floating pip.
   */
  function _ensureRemoteGrid() {
    let grid = document.getElementById('remoteVideoGrid');
    if (grid) return grid;
    grid = document.createElement('div');
    grid.id = 'remoteVideoGrid';
    (document.getElementById('callingOverlay') || document.body).appendChild(grid);
    return grid;
  }

  /**
   * Build the standard tile DOM — same shape for local and remote.
   * Including the avatar layer up-front means a tile renders cleanly
   * before its `<video>` arrives (or when a peer has no camera).
   */
  function _buildTile(id, isLocal) {
    const tile = document.createElement('div');
    tile.id = 'peerTile-' + id;
    tile.className = 'peer-tile' + (isLocal ? ' is-local' : '');
    tile.innerHTML = `
      <div class="avatar"></div>
      <div class="strip">
        <svg class="ico-mic-muted" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M19 11h-1.7c0 .74-.16 1.43-.43 2.05L18.18 14.32C18.7 13.34
                   19 12.21 19 11zM15 6.7l-.01-.7c0-1.66-1.34-3-3-3-1.16 0-2.16.66
                   -2.66 1.62l1.04 1.04A1.99 1.99 0 0 1 12 5c1.1 0 2 .9 2 2v.7l1
                   1zM4.27 3 3 4.27l6.01 6.01V11c0 1.66 1.34 3 3 3 .22 0 .44-.03
                   .65-.07L14.54 15.6c-.78.4-1.65.6-2.55.6-3.07 0-5.91-2.32-6.36
                   -5.36L4 11c0 4.08 3.06 7.46 7 7.93V22h2v-3.07l1.78 1.78L19
                   22.27 20.27 21 4.27 3z"/>
        </svg>
        <svg class="ico-cam-off" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M21 6.5l-4 4V7c0-1.1-.9-2-2-2H6.83l9.42 9.42L21 19V6.5zM3.27
                   2 2 3.27 4.73 6H4c-1.1 0-2 .9-2 2v9c0 1.1.9 2 2 2h12.73l3 3L21
                   20.73 3.27 2z"/>
        </svg>
        <span class="name"></span>
      </div>
    `;
    return tile;
  }

  /** Stable per-id colour for the tile gradient when video isn't up yet. */
  function _tileColor(id) {
    let h = 0;
    for (const ch of String(id)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    const hue = h % 360;
    return { accent: `hsl(${hue} 55% 36%)`, accentDeep: `hsl(${hue} 60% 14%)` };
  }

  /** Set the tile's avatar text + gradient + label. */
  function _decorateTile(tile, { id, name, isLocal }) {
    const c = _tileColor(id);
    tile.style.setProperty('--accent',      c.accent);
    tile.style.setProperty('--accent-deep', c.accentDeep);
    tile.querySelector('.avatar').textContent = initials(name || '?');
    tile.querySelector('.strip .name').textContent =
      isLocal ? `${name || 'You'} (you)` : (name || 'Someone');
  }

  /**
   * Local participant — same tile shape as remotes (no PiP corner).
   * That's what gives an N-way call N equal squares.
   */
  /**
   * Wire a `<video>` element into a tile and arrange its z-stacking
   * so it sits *above* the avatar layer (otherwise the absolute-
   * positioned avatar would cover the camera frame). When the stream
   * has at least one playing frame, mark the tile `.has-video` so
   * the avatar fades out.
   */
  function _bindVideoToTile(tile, vid, stream) {
    if (vid.srcObject !== stream) vid.srcObject = stream;
    const stamp = () => tile.classList.add('has-video');
    if (vid.readyState >= 2) stamp();
    vid.addEventListener('loadedmetadata', stamp);
    vid.addEventListener('playing',         stamp);
  }

  function _ensureLocalVideo() {
    if (_localVideoEl && _localVideoEl.isConnected) return _localVideoEl;
    const grid = _ensureRemoteGrid();
    const me   = Store.user || {};
    let tile = document.getElementById('peerTile-self');
    if (!tile) {
      tile = _buildTile('self', true);
      grid.appendChild(tile);
    }
    _decorateTile(tile, {
      id:      me.id || 'self',
      name:    me.display_name || me.username || 'You',
      isLocal: true,
    });
    let vid = tile.querySelector('video');
    if (!vid) {
      vid = document.createElement('video');
      vid.autoplay = true; vid.playsInline = true; vid.muted = true;
      // Append (not insertBefore) so the video paints on top of the
      // absolute-positioned avatar instead of being covered by it.
      tile.appendChild(vid);
    }
    _localVideoEl = vid;
    _refreshGridLayout();
    return _localVideoEl;
  }

  /**
   * Remote participant tile — built from the same primitives so every
   * tile in the grid looks identical regardless of who it represents.
   */
  function _attachRemoteTile(userId, stream, label) {
    const grid = _ensureRemoteGrid();
    let tile = document.getElementById('peerTile-' + userId);
    if (!tile) {
      tile = _buildTile(userId, false);
      grid.appendChild(tile);
    }
    _decorateTile(tile, { id: userId, name: label, isLocal: false });
    let vid = tile.querySelector('video');
    if (!vid) {
      vid = document.createElement('video');
      vid.autoplay = true; vid.playsInline = true;
      tile.appendChild(vid);
    }
    _bindVideoToTile(tile, vid, stream);
    _refreshGridLayout();
    return tile;
  }

  function _detachRemoteTile(userId) {
    const tile = document.getElementById('peerTile-' + userId);
    if (!tile) return;
    tile.classList.add('removing');
    tile.addEventListener('animationend', () => {
      tile.remove();
      _refreshGridLayout();
    }, { once: true });
  }

  /** Find a display name for a userId among the cached channel members. */
  function _resolveUserLabel(userId) {
    for (const c of (allChannels || [])) {
      const m = (c.members || []).find((mm) => mm.user_id === userId);
      if (m) return m.display_name || m.username || userId.slice(0, 6);
    }
    return userId.slice(0, 6);
  }

  /**
   * Lazy floating PiP container that hosts the *local* tile when the
   * call is in the special "1-on-1" mode. Empty otherwise.
   */
  function _ensureLocalPip() {
    let pip = document.getElementById('localPipBox');
    if (pip) return pip;
    pip = document.createElement('div');
    pip.id = 'localPipBox';
    (document.getElementById('callingOverlay') || document.body).appendChild(pip);
    return pip;
  }

  /**
   * Adaptive layout — same convention WhatsApp / FaceTime use:
   *
   *   N = 1  (just me, waiting):   me full-screen
   *   N = 2  (1-on-1):              the other person full-screen,
   *                                 I float in a small corner PiP tile
   *   N = 3:                        3 equal tiles (1 col × 3 rows or
   *                                 a row of 3 stripes — we use the
   *                                 row of 3 to match WhatsApp)
   *   N = 4:                        2 × 2 equal grid
   *   N = 5-6:                      3 × 2
   *   N = 7-9:                      3 × 3
   *   N = 10-16:                    4 × ⌈n/4⌉
   *   N > 16:                       ⌈√n⌉ columns (fallback)
   *
   * The local tile moves between two homes:
   *   - inside the grid as a regular `.peer-tile.is-local`,
   *   - inside `#localPipBox` (a free-floating overlay) as `.as-pip`.
   * Moving instead of duplicating means the camera stream keeps
   * flowing without re-attaching `srcObject`, so there's no flash.
   */
  function _refreshGridLayout() {
    const grid  = document.getElementById('remoteVideoGrid');
    if (!grid) return;
    const local = document.getElementById('peerTile-self');
    const remotes = grid.querySelectorAll(
      '.peer-tile:not(.is-local):not(.removing)'
    ).length;
    const total = remotes + (local ? 1 : 0);
    const pip   = _ensureLocalPip();

    if (total === 2 && local) {
      // 1-on-1: pull the local tile *out* of the grid into the PiP
      // box. The grid then has exactly one cell — the remote — and
      // it fills the screen.
      if (local.parentNode !== pip) pip.appendChild(local);
      local.classList.add('as-pip');
      grid.style.gridTemplateColumns = '1fr';
      pip.style.display = 'block';
    } else {
      // Everyone-equal grid (or just me when N=1). Make sure local
      // tile is back inside the grid.
      if (local && local.parentNode !== grid) grid.appendChild(local);
      if (local) local.classList.remove('as-pip');
      pip.style.display = 'none';

      const n = total;
      let cols;
      if (n <= 1)        cols = 1;
      else if (n === 3)  cols = 3;          // row of three for 3-way
      else if (n === 4)  cols = 2;
      else if (n <= 6)   cols = 3;
      else if (n <= 9)   cols = 3;
      else if (n <= 16)  cols = 4;
      else               cols = Math.ceil(Math.sqrt(n));
      grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
    }

    grid.dataset.peers = String(total);
  }

  /**
   * Acquire mic (and camera, when video) with graceful fallback:
   *   1. Try the user's saved preferred device IDs (exact match).
   *   2. If that fails, drop the device constraint and try `ideal`.
   *   3. If video fails entirely (no camera, blocked), keep audio-only.
   *   4. Report the failing track so the user knows which one to fix.
   *
   * Works with: built-in webcam, USB external cameras, USB mics,
   * iPhone "Continuity Camera" (it appears as a regular camera once
   * paired), virtual cameras (OBS), Bluetooth headsets, etc.
   */
  async function _acquireLocalMedia(mediaType) {
    const wantVideo = mediaType === 'video';
    const audioId = localStorage.getItem('helen.preferredAudioInput') || null;
    const videoId = localStorage.getItem('helen.preferredVideoInput') || null;

    // Layered constraint plan: most specific → most permissive.
    const plans = [];
    if (wantVideo) {
      plans.push({
        name: 'preferred-device(video+audio)',
        constraints: {
          audio: audioId ? { deviceId: { exact: audioId } } : true,
          video: videoId
            ? { deviceId: { exact: videoId }, width: { ideal: 1280 }, height: { ideal: 720 } }
            : { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'user' },
        },
      });
      plans.push({
        name: 'any-device(video+audio, low-res)',
        constraints: { audio: true, video: { width: { ideal: 640 }, height: { ideal: 480 } } },
      });
      plans.push({
        name: 'audio-only-fallback',
        constraints: { audio: true, video: false },
      });
    } else {
      plans.push({
        name: 'preferred-device(audio)',
        constraints: { audio: audioId ? { deviceId: { exact: audioId } } : true, video: false },
      });
      plans.push({
        name: 'any-device(audio)',
        constraints: { audio: true, video: false },
      });
    }

    let lastErr = null;
    for (const plan of plans) {
      try {
        console.log('[rtc] tryGetUserMedia', plan.name, JSON.stringify(plan.constraints));
        const stream = await navigator.mediaDevices.getUserMedia(plan.constraints);
        if (plan.name === 'audio-only-fallback' && wantVideo) {
          toast('No camera available — continuing audio-only', 'err');
        }
        return stream;
      } catch (err) {
        lastErr = err;
        console.warn('[rtc] plan failed:', plan.name, err.name, err.message);
      }
    }

    // Every plan failed — surface the specific reason. The most
    // common culprit when local-testing with multiple Chrome windows
    // on the same laptop is `NotReadableError`: only one process at
    // a time can open the physical webcam. Launch Chrome with
    // `--use-fake-device-for-media-stream` so every profile gets its
    // own synthetic feed, or test from separate physical devices.
    const reason = lastErr
      ? (lastErr.name === 'NotAllowedError'  ? 'permission denied'
       : lastErr.name === 'NotFoundError'    ? 'no camera/mic detected'
       : lastErr.name === 'NotReadableError' ? 'device busy in another app/tab'
       : lastErr.name === 'OverconstrainedError' ? 'no device matches the saved choice'
       : lastErr.message || lastErr.name)
      : 'unknown';
    console.error('[rtc] _acquireLocalMedia exhausted — last err:', lastErr);
    toast('Cannot access mic/camera — ' + reason, 'err');
    return null;
  }

  /**
   * Enumerate input devices for the device picker. Labels are only
   * populated after the user has granted permission at least once —
   * Chrome hides them otherwise to prevent fingerprinting.
   */
  async function listMediaDevices() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
      return { audio: [], video: [] };
    }
    try {
      const all = await navigator.mediaDevices.enumerateDevices();
      return {
        audio: all.filter((d) => d.kind === 'audioinput'),
        video: all.filter((d) => d.kind === 'videoinput'),
      };
    } catch (err) {
      console.warn('[rtc] enumerateDevices failed', err);
      return { audio: [], video: [] };
    }
  }

  /** Pull short-lived TURN/STUN credentials from the server. */
  async function _fetchIceServers() {
    return new Promise((res) => {
      try {
        sock.emit('call_get_ice_servers', {}, (r) => {
          if (r && Array.isArray(r.ice_servers)) res(r.ice_servers);
          else res([{ urls: 'stun:stun.l.google.com:19302' }]);
        });
        setTimeout(() => res([{ urls: 'stun:stun.l.google.com:19302' }]), 1500);
      } catch {
        res([{ urls: 'stun:stun.l.google.com:19302' }]);
      }
    });
  }

  /**
   * Acquire local media + ICE servers. Run once per call, before any
   * peers are added. Subsequent peers reuse the same stream + ICE
   * servers — that's what makes a mesh efficient.
   */
  async function _setupLocalMedia(mediaType) {
    if (mediaType === 'video') _ensureLocalVideo();
    _localStream = await _acquireLocalMedia(mediaType);
    if (_localStream && _localVideoEl) {
      const tile = document.getElementById('peerTile-self');
      // Use the binding helper so the avatar layer fades out as soon
      // as the first camera frame is decoded — same UX as remote tiles.
      if (tile) _bindVideoToTile(tile, _localVideoEl, _localStream);
      else      _localVideoEl.srcObject = _localStream;
    }
    _iceServers = await _fetchIceServers();
  }

  /**
   * Create a per-peer RTCPeerConnection. `sendOffer=true` when *we*
   * just joined and need to invite an existing participant; false when
   * the remote user joined after us — they'll send the offer first.
   * Either way, signaling flows over the existing socket events.
   */
  async function _addPeer(userId, sendOffer, label) {
    if (_peers.has(userId)) return _peers.get(userId).pc;

    // Resolve a friendly label even when the caller didn't pass one —
    // look up the user in any cached channel's `members[]`.
    if (!label) label = _resolveUserLabel(userId);

    const pc = new RTCPeerConnection({ iceServers: _iceServers || [] });
    if (_localStream) {
      for (const t of _localStream.getTracks()) pc.addTrack(t, _localStream);
    }

    pc.addEventListener('track', (ev) => {
      const stream = ev.streams && ev.streams[0];
      if (!stream) return;
      _attachRemoteTile(userId, stream, label);
      console.log('[rtc] track from', userId.slice(0,6),
        stream.getTracks().map((t) => t.kind).join(','));
    });
    pc.addEventListener('icecandidate', (ev) => {
      if (ev.candidate) {
        sock.emit('signal_ice_candidate', {
          target_id: userId, candidate: ev.candidate,
        });
      }
    });
    pc.addEventListener('connectionstatechange', () => {
      console.log('[rtc]', userId.slice(0,6), 'state =', pc.connectionState);
      if (pc.connectionState === 'connected') {
        document.getElementById('callingOverlay')?.classList.add('in-call');
        if (!_callTimerHandle) _startCallTimer();
        _ensureInCallControls();
      }
      if (pc.connectionState === 'failed' || pc.connectionState === 'closed') {
        // Don't tear down the whole call — just this peer.
        if (_peers.has(userId)) _removePeer(userId);
      }
    });

    _peers.set(userId, { pc, label });

    if (sendOffer) {
      try {
        const offer = await pc.createOffer({
          offerToReceiveAudio: true,
          offerToReceiveVideo: true,
        });
        await pc.setLocalDescription(offer);
        sock.emit('signal_offer', { target_id: userId, sdp: offer });
      } catch (err) {
        console.error('[rtc] offer to', userId.slice(0,6), 'failed', err);
      }
    }
    return pc;
  }

  function _removePeer(userId) {
    const p = _peers.get(userId);
    if (!p) return;
    try { p.pc.close(); } catch {}
    _peers.delete(userId);
    _detachRemoteTile(userId);
  }

  // ── Call duration timer ─────────────────────────────────────────
  //
  // We start counting the moment the peer connection enters
  // `connected` (not when the call was placed) so the displayed time
  // matches the duration of *useful* media — same convention as
  // FaceTime, WhatsApp, and the Helen desktop client.
  let _callStartedAt = 0;
  let _callTimerHandle = null;
  function _startCallTimer() {
    _callStartedAt = Date.now();
    _stopCallTimer();
    const tick = () => {
      const el = document.querySelector('.calling-status');
      if (!el) return;
      el.textContent = _formatCallDuration(Date.now() - _callStartedAt);
    };
    tick();
    _callTimerHandle = setInterval(tick, 1000);
  }
  function _stopCallTimer() {
    if (_callTimerHandle) clearInterval(_callTimerHandle);
    _callTimerHandle = null;
  }
  function _formatCallDuration(ms) {
    const total = Math.max(0, Math.floor(ms / 1000));
    const days  = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const mins  = Math.floor((total % 3600) / 60);
    const secs  = total % 60;
    const pad   = (n) => String(n).padStart(2, '0');
    if (days)  return `${days}d ${pad(hours)}:${pad(mins)}:${pad(secs)}`;
    if (hours) return `${pad(hours)}:${pad(mins)}:${pad(secs)}`;
    return `${pad(mins)}:${pad(secs)}`;
  }

  /**
   * Once a call is connected, swap the ringing/cancel button row for
   * the WhatsApp-style in-call toolbar: Mute, Camera, End.
   * Toggles update both the local MediaStreamTrack (so peers actually
   * receive silence / a black frame) and the local tile's classes
   * (so the operator sees the icons appear over their own avatar).
   */
  function _ensureInCallControls() {
    const host = document.getElementById('callingOverlay');
    if (!host) return;
    const actions = host.querySelector('.calling-actions');
    if (!actions || actions.dataset.mode === 'in-call') return;
    actions.dataset.mode = 'in-call';
    actions.innerHTML = `
      <button class="ic-btn" data-act="mute"  aria-label="Toggle microphone">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/>
          <path d="M19 10v1a7 7 0 0 1-14 0v-1"/>
          <line x1="12" y1="18" x2="12" y2="22"/>
          <line x1="8"  y1="22" x2="16" y2="22"/>
        </svg>
      </button>
      <button class="ic-btn" data-act="cam"   aria-label="Toggle camera">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round">
          <polygon points="23 7 16 12 23 17 23 7"/>
          <rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
        </svg>
      </button>
      <button class="ic-btn end" data-act="end" aria-label="End call">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07
                   19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3
                   a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11
                   L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45
                   12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/>
        </svg>
      </button>
    `;
    actions.querySelector('[data-act="mute"]').addEventListener('click', () => {
      _toggleLocalMuted();
    });
    actions.querySelector('[data-act="cam"]').addEventListener('click', () => {
      _toggleLocalCamOff();
    });
    actions.querySelector('[data-act="end"]').addEventListener('click', () => {
      const cid = _activeCall && _activeCall.call_id;
      if (cid) sock && sock.emit('v2_call_hangup', { call_id: cid });
      _activeCall = null;
      _teardownWebRTC();
      hideCallingOverlay();
    });
  }

  function _toggleLocalMuted() {
    if (!_localStream) return;
    const tracks = _localStream.getAudioTracks();
    if (!tracks.length) return;
    const next = !tracks[0].enabled;     // current state inverted
    for (const t of tracks) t.enabled = next ? false : true;
    const muted = !next === false ? false : true;
    // tracks[0].enabled === true means NOT muted.
    const isMuted = !tracks[0].enabled;
    document.getElementById('peerTile-self')?.classList.toggle('muted', isMuted);
    document.querySelector('.calling-actions [data-act="mute"]')
      ?.classList.toggle('on', isMuted);
  }
  function _toggleLocalCamOff() {
    if (!_localStream) return;
    const tracks = _localStream.getVideoTracks();
    if (!tracks.length) return;
    for (const t of tracks) t.enabled = !t.enabled;
    const camOff = !tracks[0].enabled;
    document.getElementById('peerTile-self')?.classList.toggle('cam-off', camOff);
    document.querySelector('.calling-actions [data-act="cam"]')
      ?.classList.toggle('on', camOff);
  }

  function _teardownWebRTC() {
    for (const [uid] of _peers) _removePeer(uid);
    _peers.clear();
    if (_localStream) {
      for (const t of _localStream.getTracks()) try { t.stop(); } catch {}
    }
    _localStream = null;
    if (_localVideoEl) _localVideoEl.srcObject = null;
    document.getElementById('remoteVideoGrid')?.replaceChildren();
    _stopCallTimer();
    document.getElementById('callingOverlay')?.classList.remove('in-call');
  }

  function startCall(mediaType) {
    if (!currentChannel) return;
    if (!sock || !sockReady) { toast('Not connected', 'err'); return; }
    if (_activeCall)            { toast('Already in a call', 'err'); return; }

    const me = Store.user && Store.user.id;

    if (currentChannel.type === 'dm') {
      const other = (currentChannel.members || []).find((m) => m.user_id !== me);
      if (!other) { toast('No callee found', 'err'); return; }
      sock.emit('v2_call_initiate',
        { target_id: other.user_id, media_type: mediaType },
        (resp) => {
          if (resp && resp.error) {
            toast('Call failed: ' + resp.error, 'err');
            hideCallingOverlay(); _activeCall = null; return;
          }
          _activeCall = {
            call_id:      resp && resp.call_id,
            mediaType,
            peerName:     other.display_name || other.username,
            remoteUserId: other.user_id,    // needed by the offer flow
            isInitiator:  true,
          };
        });
      showCallingOverlay({
        name: other.display_name || other.username, mediaType, dir: 'outgoing',
      });
    } else {
      // Group: announce by joining; server fans out per-member events.
      // After the ack we set up our local media once and offer to
      // every *existing* participant. Newcomers who arrive after us
      // will offer to us via `call_participant_joined`.
      sock.emit('v2_call_join_group',
        { channel_id: currentChannel.id, media_type: mediaType },
        async (resp) => {
          if (resp && resp.error) {
            toast('Call failed: ' + resp.error, 'err');
            hideCallingOverlay(); _activeCall = null; return;
          }
          _activeCall = {
            call_id:    resp && resp.call_id,
            mediaType,
            peerName:   currentChannel.name || 'Group call',
            channelId:  currentChannel.id,
            isInitiator: true,
            isGroup:    true,
          };
          try {
            await _setupLocalMedia(mediaType);
            const others = ((resp && resp.participants) || [])
              .map((p) => p.user_id)
              .filter((uid) => uid && uid !== me);
            for (const uid of others) {
              await _addPeer(uid, /*sendOffer*/ true);
            }
            // Mark live for our own row too.
            _activeChannelCalls.set(currentChannel.id, {
              call_id: _activeCall.call_id,
              mediaType,
            });
            _updateChannelLiveBadge(currentChannel.id, true);
          } catch (err) {
            console.error('[rtc] group setup failed', err);
            toast('Could not start group call: ' + err.message, 'err');
          }
        });
      showCallingOverlay({
        name: currentChannel.name || 'Group call', mediaType,
        dir: 'outgoing', isGroup: true, channelId: currentChannel.id,
      });
    }
  }

  function showCallingOverlay({
    name, username, shareCode, mediaType, dir, callId, callerId,
    subtitle, isGroup, channelId,
  }) {
    let host = document.getElementById('callingOverlay');
    if (!host) {
      host = document.createElement('div');
      host.id = 'callingOverlay';
      host.className = 'calling-overlay';
      document.body.appendChild(host);
    }
    const isIncoming = dir === 'incoming';
    const icon       = (isGroup ? '👥 ' : '') +
                       (mediaType === 'video' ? '📹 Video call' : '📞 Voice call');
    const statusText = isIncoming ? 'Incoming…' : 'Calling…';

    // Two-line identity block — for DMs, line 2 is the @handle. For
    // group calls, line 2 names the user who started the call.
    let identityHtml = '<div class="calling-name">' + escape(name || 'Unknown') + '</div>';
    if (subtitle) {
      identityHtml += '<div class="calling-subtitle">' + escape(subtitle) + '</div>';
    } else if (username || shareCode) {
      const handleShort = username
        ? '@' + username
        : (shareCode ? '@' + shareCode.slice(0,8) + '…' + shareCode.slice(-4) : '');
      const handleFull = shareCode ? '@' + shareCode : (username ? '@' + username : '');
      identityHtml += '<div class="calling-handle" title="' + escape(handleFull) + '">' +
                        escape(handleShort) +
                      '</div>';
    }

    host.innerHTML =
      '<div class="ring-circle"></div>' +
      '<div class="calling-status">' + statusText + '</div>' +
      identityHtml +
      '<div class="calling-kind">'   + icon + '</div>' +
      '<div class="calling-note">⚠ Web client signals only — full ' +
        'audio/video needs the desktop app on both ends</div>' +
      '<div class="calling-actions">' +
        (isIncoming
          ? '<button class="calling-accept" type="button">Accept</button>' +
            '<button class="calling-decline" type="button">Decline</button>'
          : '<button class="calling-cancel" type="button">End</button>') +
      '</div>';
    host.style.display = 'flex';

    const decline = host.querySelector('.calling-decline');
    if (decline) decline.addEventListener('click', () => {
      sock && sock.emit('v2_call_reject', { call_id: callId, caller_id: callerId });
      hideCallingOverlay();
    });

    const cancel = host.querySelector('.calling-cancel');
    if (cancel) cancel.addEventListener('click', () => {
      const cid = callId || (_activeCall && _activeCall.call_id);
      if (cid) sock && sock.emit('v2_call_hangup', { call_id: cid });
      _activeCall = null;
      hideCallingOverlay();
    });

    const accept = host.querySelector('.calling-accept');
    if (accept) accept.addEventListener('click', async () => {
      _activeCall = {
        call_id: callId, mediaType, peerName: name,
        channelId, isGroup, isInitiator: false,
      };
      const s = host.querySelector('.calling-status');
      if (s) s.textContent = isGroup ? 'Joining…' : 'Connected';
      const a = host.querySelector('.calling-actions');
      if (a) {
        a.innerHTML = '<button class="calling-cancel" type="button">End</button>';
        a.querySelector('.calling-cancel').addEventListener('click', () => {
          sock && sock.emit('v2_call_hangup', { call_id: callId });
          _activeCall = null;
          _teardownWebRTC();
          hideCallingOverlay();
        });
      }

      if (isGroup && channelId) {
        // Accepting a group call = joining via the same endpoint
        // initiators use. Server returns the existing participants;
        // we offer to each one in turn, building a full mesh.
        sock.emit('v2_call_join_group',
          { channel_id: channelId, media_type: mediaType },
          async (resp) => {
            if (resp && resp.error) {
              toast('Could not join: ' + resp.error, 'err');
              _activeCall = null; hideCallingOverlay(); return;
            }
            try {
              await _setupLocalMedia(mediaType);
              const me = Store.user && Store.user.id;
              const others = ((resp && resp.participants) || [])
                .map((p) => p.user_id)
                .filter((uid) => uid && uid !== me);
              for (const uid of others) {
                await _addPeer(uid, /*sendOffer*/ true);
              }
            } catch (err) {
              console.error('[rtc] join group setup failed', err);
              toast('Group call setup failed: ' + err.message, 'err');
            }
          });
        return;
      }

      // DM accept: server still drives signaling via `call_accepted`
      // → we'll create the offer in that handler.
      sock && sock.emit('v2_call_accept', {
        call_id: callId, caller_id: callerId,
      });
    });
  }
  function hideCallingOverlay() {
    const host = document.getElementById('callingOverlay');
    if (host) host.style.display = 'none';
  }

  // ── Composer + button: attach menu (Photo / File) ─────────
  const composerPlus = document.querySelector('.composer-plus');
  if (composerPlus) {
    composerPlus.addEventListener('click', (ev) => {
      ev.preventDefault();
      openAttachMenu(ev);
    });
  }
  function openAttachMenu(ev) {
    const existing = document.getElementById('chatCtxMenu');
    if (existing) existing.remove();
    const menu = document.createElement('div');
    menu.id = 'chatCtxMenu';
    menu.className = 'ctx-menu';
    const buttons = [
      { id:'photo', label:'🖼 Photo or video', accept: 'image/*,video/*' },
      { id:'file',  label:'📎 Document / file', accept: '*/*' },
    ];
    menu.innerHTML = buttons.map(b =>
      '<button data-act="'+b.id+'">'+b.label+'</button>'
    ).join('');
    document.body.appendChild(menu);
    const x = ev.clientX || window.innerWidth/2;
    const y = ev.clientY || window.innerHeight - 200;
    const rect = menu.getBoundingClientRect();
    menu.style.left = Math.max(8, Math.min(window.innerWidth - rect.width - 8, x - rect.width/2)) + 'px';
    menu.style.top  = Math.max(8, y - rect.height - 8) + 'px';
    menu.querySelectorAll('button').forEach(b => {
      b.addEventListener('click', () => {
        const cfg = buttons.find(x => x.id === b.dataset.act);
        menu.remove();
        if (cfg) pickAndUpload(cfg.accept);
      });
    });
    // Auto-dismiss only on taps *outside* the menu. The previous one-shot
    // handler removed the menu on the very first pointerdown anywhere —
    // including inside the menu — so the option's click never landed and
    // the file picker never opened.
    const dismissOutside = (ev) => {
      if (ev.target.closest('#chatCtxMenu')) return;
      menu.remove();
      document.removeEventListener('pointerdown', dismissOutside, true);
    };
    setTimeout(
      () => document.addEventListener('pointerdown', dismissOutside, true),
      0,
    );
  }
  function pickAndUpload(accept) {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = accept;
    input.style.display = 'none';
    document.body.appendChild(input);
    input.addEventListener('change', async () => {
      const f = input.files && input.files[0];
      input.remove();
      if (!f) return;
      await uploadAndSend(f);
    });
    input.click();
  }
  async function uploadAndSend(file) {
    if (!currentChannel) return;
    const optimisticId = 'cid_' + Date.now() + '_upload';
    appendMessage({
      id: optimisticId,
      content: '⏳ Uploading ' + file.name + ' (' + Math.round(file.size/1024) + ' KB)…',
      sender_id: Store.user && Store.user.id,
      created_at: new Date().toISOString(),
    });
    chatLog.scrollTop = chatLog.scrollHeight;

    const r = await Api.uploadFile(file, currentChannel.id);
    const li = _renderedMessages.get(optimisticId);
    if (!r.ok) {
      if (li) {
        li.classList.add('err');
        li.querySelector('.chat-body').textContent = '⚠ Upload failed: ' + (r.data?.detail || r.status);
      }
      toast('Upload failed', 'err');
      return;
    }
    if (li) li.remove();
    _renderedMessages.delete(optimisticId);
    // Now send a chat message referencing the uploaded file
    const fileId = r.data.id;
    const isImg = (file.type || '').startsWith('image/');
    const cid = 'cid_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
    sock.emit('v2_chat_send_message', {
      channel_id: currentChannel.id,
      content: isImg ? '🖼 ' + file.name : '📎 ' + file.name,
      type: isImg ? 'image' : 'file',
      file_id: fileId,
      client_id: cid,
    });
    // Optimistic local render with file preview
    appendMessage({
      id: cid,
      content: isImg ? '🖼 ' + file.name : '📎 ' + file.name,
      sender_id: Store.user && Store.user.id,
      created_at: new Date().toISOString(),
      file_id: fileId,
      type: isImg ? 'image' : 'file',
      file_name: file.name,
      file_size: file.size,
      mime_type: file.type,
    });
    chatLog.scrollTop = chatLog.scrollHeight;
    toast('Sent', 'ok');
  }
  async function openChannelInfo(c) {
    _channelInfoTitle.textContent = c.type === 'dm' ? 'Chat info' : 'Group info';
    _channelInfoHero.innerHTML =
      '<div class="hero-icon" style="background:linear-gradient(135deg,#4cc2ff,#7c5cff);' +
        'width:80px;height:80px;border-radius:40px;display:flex;' +
        'align-items:center;justify-content:center;font-size:32px;' +
        'font-weight:700;color:#fff;margin:0 auto 10px;">' +
        escape(initials(c.name || c.type || '?')) + '</div>' +
      '<h2 style="margin:6px 0 2px; font-size:22px;">' +
        escape(c.name || (c.type === 'dm' ? 'Direct message' : 'Group')) + '</h2>' +
      (c.description
        ? '<p class="hero-sub">' + escape(c.description) + '</p>'
        : '');
    setText(_channelInfoStatus, '');
    show('channelInfo');
    _memberList.innerHTML = '<li class="empty-state">Loading…</li>';
    const r = await Api.channelDetail(c.id);
    if (!r.ok) {
      _memberList.innerHTML = '';
      setText(_channelInfoStatus, extractError(r) || 'Cannot load members', 'err');
      return;
    }
    // Channel detail includes inline `members[]` array.
    const members = (r.data && r.data.members) || [];
    _channelInfoLabel.textContent = 'Members · ' + members.length;
    _memberList.innerHTML = '';
    const me = Store.user && Store.user.id;
    const frag = document.createDocumentFragment();
    for (const m of members) {
      const uid = m.user_id || m.id;
      const row = document.createElement('div');
      row.className = 'member-row';
      const role = (m.role || 'member').toLowerCase();
      row.innerHTML =
        '<div class="avatar-sm">' + initials(m.display_name || m.username) + '</div>' +
        '<div class="contact-body">' +
          '<div class="contact-name">' + escape(m.display_name || m.username) +
            (uid === me ? ' <span style="color:#8ea2c5;">(you)</span>' : '') +
          '</div>' +
          '<div class="contact-handle" title="' + escape(handleFull(m)) +
            '">' + escape(handleShort(m)) + '</div>' +
          '<div class="member-presence">' + presenceHtml(m) + '</div>' +
        '</div>' +
        '<span class="member-tag ' + role + '">' + role + '</span>' +
        (uid !== me && c.type !== 'dm'
          ? '<button class="member-kick" data-kick="' + escape(uid) + '" title="Remove">✕</button>'
          : '');
      frag.appendChild(row);
    }
    _memberList.appendChild(frag);
  }
  _memberList.addEventListener('click', async (ev) => {
    const b = ev.target.closest('[data-kick]');
    if (!b || !currentChannel) return;
    if (!confirm('Remove this member?')) return;
    const r = await Api.removeMember(currentChannel.id, b.dataset.kick);
    if (r.ok) { toast('Member removed', 'ok'); openChannelInfo(currentChannel); }
    else toast(extractError(r), 'err');
  });
  document.getElementById('addMemberBtn').addEventListener('click', () => {
    if (!currentChannel) return;
    if (currentChannel.type === 'dm') {
      toast("Can't add to a DM — create a group instead.", 'err');
      return;
    }
    // Reuse contacts picker in group mode; after Done, add them one by one.
    openContacts('group', { back: 'channelInfo', selected: [] });
    // Override done handler for this round.
    const handler = async () => {
      const picked = Array.from(_selectedIds.values());
      _contactsDoneBtn.removeEventListener('click', handler);
      setText(_channelInfoStatus, 'Adding ' + picked.length + '…');
      let added = 0;
      for (const u of picked) {
        const r = await Api.addMember(currentChannel.id, u.id);
        if (r.ok) added++;
      }
      setText(_channelInfoStatus,
        'Added ' + added + ' of ' + picked.length,
        added === picked.length ? 'ok' : 'err');
      openChannelInfo(currentChannel);
    };
    _contactsDoneBtn.addEventListener('click', handler, { once: true });
  });
  document.getElementById('leaveChannelBtn').addEventListener('click', async () => {
    if (!currentChannel || !Store.user) return;
    if (!confirm('Leave this channel?')) return;
    const r = await Api.removeMember(currentChannel.id, Store.user.id);
    if (r.ok) {
      toast('Left the channel', 'ok');
      currentChannel = null;
      Store.activeChannel = '';
      loadChannels();
      show('channels');
    } else toast(extractError(r), 'err');
  });

  // ── 4.3.x Media & camera settings ───────────────────────────────
  const MEDIA_PRESETS = {
    auto:   { video: { width: { ideal: 1280 }, height: { ideal: 720 } } },
    low:    { video: { width: { ideal: 640  }, height: { ideal: 480  },
                       frameRate: { ideal: 15, max: 20 } } },
    medium: { video: { width: { ideal: 1280 }, height: { ideal: 720  },
                       frameRate: { ideal: 30, max: 30 } } },
    high:   { video: { width: { ideal: 1920 }, height: { ideal: 1080 },
                       frameRate: { ideal: 30, max: 30 } } },
    max:    { video: { width: { ideal: 3840 }, height: { ideal: 2160 },
                       frameRate: { ideal: 60, max: 60 } } },
  };
  const MEDIA_PREFS_KEY = 'helen.media.prefs';
  function loadMediaPrefs() {
    try { return JSON.parse(localStorage.getItem(MEDIA_PREFS_KEY) || '{}'); }
    catch { return {}; }
  }
  function saveMediaPrefs(p) {
    localStorage.setItem(MEDIA_PREFS_KEY, JSON.stringify(p));
  }
  let _mediaPrefs = Object.assign(
    { mode: 'video', quality: 'auto', cameraId: '', micId: '' },
    loadMediaPrefs()
  );
  let _mediaStream = null;

  function summarizeMediaPrefs(p) {
    if (p.mode === 'text')  return 'Text only';
    if (p.mode === 'audio') return 'Audio only · mic';
    return 'Video · ' + (p.quality || 'auto');
  }

  async function enumerateMediaDevices() {
    try {
      const devs = await navigator.mediaDevices.enumerateDevices();
      const cams = devs.filter((d) => d.kind === 'videoinput');
      const mics = devs.filter((d) => d.kind === 'audioinput');
      fillSelect('cameraSelect', cams, 'Camera', _mediaPrefs.cameraId);
      fillSelect('micSelect',    mics, 'Microphone', _mediaPrefs.micId);
      return { cams, mics };
    } catch (e) {
      return { cams: [], mics: [] };
    }
  }
  function fillSelect(id, devs, kind, selectedId) {
    const sel = document.getElementById(id);
    if (!sel) return;
    sel.innerHTML = '';
    if (!devs.length) {
      sel.innerHTML = '<option value="">— no ' + kind.toLowerCase() + 's detected —</option>';
      return;
    }
    for (let i = 0; i < devs.length; i++) {
      const d = devs[i];
      const opt = document.createElement('option');
      opt.value = d.deviceId;
      opt.textContent = d.label || (kind + ' ' + (i + 1));
      if (d.deviceId === selectedId) opt.selected = true;
      sel.appendChild(opt);
    }
  }

  function stopPreview() {
    if (_mediaStream) {
      _mediaStream.getTracks().forEach((t) => t.stop());
      _mediaStream = null;
    }
    const v = document.getElementById('mediaVideo');
    if (v) { v.srcObject = null; }
    document.getElementById('btnPreviewStart').disabled = false;
    document.getElementById('btnPreviewStop').disabled = true;
  }

  async function startPreview() {
    stopPreview();
    const hint = document.getElementById('mediaHint');
    setText(hint, '');
    const preset = MEDIA_PRESETS[_mediaPrefs.quality] || MEDIA_PRESETS.auto;
    const constraints = { audio: false, video: false };
    if (_mediaPrefs.mode === 'text') {
      setText(hint, "Mode is text-only — nothing to preview.", 'err');
      return;
    }
    if (_mediaPrefs.mode === 'video') {
      constraints.video = Object.assign({}, preset.video);
      if (_mediaPrefs.cameraId) constraints.video.deviceId = { exact: _mediaPrefs.cameraId };
    }
    if (_mediaPrefs.mode !== 'text') {
      constraints.audio = {};
      if (_mediaPrefs.micId) constraints.audio.deviceId = { exact: _mediaPrefs.micId };
    }
    try {
      _mediaStream = await navigator.mediaDevices.getUserMedia(constraints);
    } catch (e) {
      setText(hint, 'Cannot access camera/mic: ' + e.message, 'err');
      return;
    }
    const v = document.getElementById('mediaVideo');
    v.srcObject = _mediaStream;
    document.getElementById('btnPreviewStart').disabled = true;
    document.getElementById('btnPreviewStop').disabled  = false;
    // Show actual negotiated resolution.
    await enumerateMediaDevices();     // re-run to pick up labels now that permission is granted
    const vt = _mediaStream.getVideoTracks()[0];
    const at = _mediaStream.getAudioTracks()[0];
    const caps = document.getElementById('mediaCaps');
    let html = '';
    if (vt) {
      const s = vt.getSettings();
      html += '<div class="stat-row"><span class="stat-label">Video</span>' +
              '<span class="stat-value mono">' +
                (s.width || '?') + '×' + (s.height || '?') +
                ' @ ' + (s.frameRate ? Math.round(s.frameRate) + 'fps' : '?') +
              '</span></div>';
      html += '<div class="stat-row"><span class="stat-label">Device</span>' +
              '<span class="stat-value">' + escape(vt.label || 'camera') + '</span></div>';
    }
    if (at) {
      html += '<div class="stat-row"><span class="stat-label">Audio</span>' +
              '<span class="stat-value">' + escape(at.label || 'mic') + '</span></div>';
    }
    if (!html) {
      html = '<div class="stat-row"><span class="stat-label">Status</span>' +
             '<span class="stat-value">no tracks</span></div>';
    }
    caps.innerHTML = html;
    const s = vt ? vt.getSettings() : {};
    const m = document.getElementById('mediaPreviewMeta');
    if (m) m.textContent = (s.width || '?') + '×' + (s.height || '?') +
                           '  ·  ' + (_mediaPrefs.quality || 'auto');
    setText(hint, 'Preview running. Your browser chose the closest match to the selected preset.', 'ok');
  }

  function wireMediaPrefsUi() {
    // Apply persisted selection.
    const modeRadios    = document.querySelectorAll('input[name="mediaMode"]');
    const qualityRadios = document.querySelectorAll('input[name="quality"]');
    modeRadios.forEach((r) => { r.checked = (r.value === _mediaPrefs.mode); });
    qualityRadios.forEach((r) => { r.checked = (r.value === _mediaPrefs.quality); });

    modeRadios.forEach((r) => r.addEventListener('change', () => {
      if (r.checked) {
        _mediaPrefs.mode = r.value;
        saveMediaPrefs(_mediaPrefs);
        updateMediaSummary();
      }
    }));
    qualityRadios.forEach((r) => r.addEventListener('change', () => {
      if (r.checked) {
        _mediaPrefs.quality = r.value;
        saveMediaPrefs(_mediaPrefs);
        updateMediaSummary();
        if (_mediaStream) startPreview();     // re-negotiate with new preset
      }
    }));

    const cam = document.getElementById('cameraSelect');
    const mic = document.getElementById('micSelect');
    cam.addEventListener('change', () => {
      _mediaPrefs.cameraId = cam.value;
      saveMediaPrefs(_mediaPrefs);
      if (_mediaStream) startPreview();
    });
    mic.addEventListener('change', () => {
      _mediaPrefs.micId = mic.value;
      saveMediaPrefs(_mediaPrefs);
      if (_mediaStream) startPreview();
    });

    document.getElementById('btnPreviewStart').addEventListener('click', startPreview);
    document.getElementById('btnPreviewStop' ).addEventListener('click', stopPreview);
  }
  let _mediaUiWired = false;

  async function openMedia() {
    if (!_mediaUiWired) { wireMediaPrefsUi(); _mediaUiWired = true; }
    show('media');
    await enumerateMediaDevices();
  }
  function updateMediaSummary() {
    const el = document.getElementById('profileMediaSummary');
    if (el) el.textContent = summarizeMediaPrefs(_mediaPrefs);
  }

  document.getElementById('rowMedia').addEventListener('click', openMedia);

  // ── 4.3.y Storage + auto-cleanup ────────────────────────────────
  const CLEANUP_KEY = 'helen.cleanup.prefs';
  const CLEANUP_LOG = 'helen.cleanup.log';
  const AGE_TO_SEC = {
    never: 0,
    '1d':  86400,
    '1w':  7 * 86400,
    '1m':  30 * 86400,
    '3m':  90 * 86400,
    '1y':  365 * 86400,
  };

  function loadCleanupPrefs() {
    try {
      return Object.assign(
        { age: 'never',
          scopes: { group: true, dm: false, channel: false },
          types:  { image: true, video: true, audio: true, document: true, message: false } },
        JSON.parse(localStorage.getItem(CLEANUP_KEY) || '{}')
      );
    } catch { return null; }
  }
  function saveCleanupPrefs(p) {
    localStorage.setItem(CLEANUP_KEY, JSON.stringify(p));
  }
  function loadCleanupLog() {
    try { return JSON.parse(localStorage.getItem(CLEANUP_LOG) || '{}'); }
    catch { return {}; }
  }
  function saveCleanupLog(l) {
    localStorage.setItem(CLEANUP_LOG, JSON.stringify(l));
  }
  let _cleanupPrefs = loadCleanupPrefs();

  function cleanupSummary(p) {
    if (p.age === 'never' || !enabledScopeCount(p) || !enabledTypeCount(p)) {
      return 'Off';
    }
    const ageLabel = ({ '1d':'1 day','1w':'1 week','1m':'1 month',
                        '3m':'3 months','1y':'1 year' })[p.age] || p.age;
    return ageLabel;
  }
  function enabledScopeCount(p) {
    return Object.values(p.scopes || {}).filter(Boolean).length;
  }
  function enabledTypeCount(p) {
    return Object.values(p.types || {}).filter(Boolean).length;
  }
  function updateCleanupSummary() {
    const el = document.getElementById('profileCleanupSummary');
    if (el) el.textContent = cleanupSummary(_cleanupPrefs);
  }

  // Aggregator: walks user's channels, pulls /api/media/channel/{id} per
  // channel, accumulates by media_type. Caches per-session so reloading
  // the storage tab doesn't re-scan on every open.
  let _filesCache = null;      // Array of media items (cross-channel)
  let _channelsById = null;    // Map for scope lookup

  async function scanStorage(force) {
    if (!force && _filesCache) return _filesCache;
    // Fetch channels first.
    const ch = await Api.channels();
    if (!ch.ok) throw new Error(extractError(ch));
    const channels = (ch.data && (ch.data.channels || ch.data.items)) || [];
    _channelsById = new Map(channels.map((c) => [c.id, c]));
    const me = (Store.user && Store.user.id) || '';
    const all = [];
    // Concurrency: 3 channel fetches at a time to avoid hammering server.
    const queue = channels.slice();
    async function worker() {
      while (queue.length) {
        const c = queue.shift();
        const r = await Api.request('GET',
          '/api/media/channel/' + encodeURIComponent(c.id) +
          '?per_page=200&uploader_id=' + encodeURIComponent(me));
        if (!r.ok) continue;
        const items = (r.data && (r.data.items || r.data.media || r.data.results)) ||
                      (Array.isArray(r.data) ? r.data : []);
        for (const it of items) {
          it.__channel_type = c.type;
          it.__channel_name = c.name || c.type;
          all.push(it);
        }
      }
    }
    await Promise.all([worker(), worker(), worker()]);
    _filesCache = all;
    return all;
  }

  let _fileFilterCat = 'all';
  let _fileFilterScope = 'all';
  let _fileSelected = new Set();

  function matchesFilter(it) {
    if (_fileFilterCat !== 'all' && it.media_type !== _fileFilterCat) return false;
    if (_fileFilterScope !== 'all' && it.__channel_type !== _fileFilterScope) return false;
    return true;
  }

  function renderStorage() {
    const items = _filesCache || [];
    // Totals by category.
    const totals = { image: 0, video: 0, audio: 0, document: 0 };
    const counts = { image: 0, video: 0, audio: 0, document: 0 };
    let grand = 0;
    for (const it of items) {
      const t = it.media_type || 'document';
      const sz = it.file_size || it.size_bytes || 0;
      if (totals[t] == null) continue;
      totals[t] += sz; counts[t] += 1; grand += sz;
    }
    document.getElementById('totalUsageText').textContent = fmtBytes(grand);
    // Segmented bar.
    const bar = document.getElementById('usageBar');
    bar.innerHTML = '';
    const cats = ['image', 'video', 'audio', 'document'];
    const legend = document.getElementById('usageLegend');
    legend.innerHTML = '';
    cats.forEach((c) => {
      const frac = grand > 0 ? totals[c] / grand : 0;
      if (frac > 0) {
        const seg = document.createElement('div');
        seg.className = 'seg seg-' + c;
        seg.style.flexBasis = (frac * 100).toFixed(1) + '%';
        bar.appendChild(seg);
      }
      const lg = document.createElement('span');
      lg.className = 'leg';
      const labels = { image:'Images', video:'Videos', audio:'Audio', document:'Docs' };
      lg.innerHTML = '<span class="dot" style="background:var(--seg-color-' + c + ')"></span>' +
                     ' ' + labels[c] + ' ' + fmtBytes(totals[c]);
      legend.appendChild(lg);
    });
    // KPI tiles.
    cats.forEach((c) => {
      const tile = document.querySelector('[data-cat="' + c + '"].kpi-tile');
      if (!tile) return;
      tile.querySelector('[data-slot="value"]').textContent = fmtBytes(totals[c]);
      tile.querySelector('[data-slot="sub"]').textContent =
        counts[c] + ' ' + (counts[c] === 1 ? 'file' : 'files');
    });
    // File list (filtered).
    const visible = items.filter(matchesFilter)
      .sort((a, b) =>
        new Date(b.created_at || 0).getTime() -
        new Date(a.created_at || 0).getTime());
    const list = document.getElementById('fileList');
    list.innerHTML = '';
    if (!visible.length) {
      list.innerHTML = '<li class="empty-state" style="padding:20px;">No files match this filter.</li>';
      setText(document.getElementById('storageHint'), '');
      return;
    }
    const frag = document.createDocumentFragment();
    for (const it of visible) {
      const t = it.media_type || 'document';
      const icon = { image:'🖼', video:'🎬', audio:'🎵', document:'📄' }[t];
      const li = document.createElement('li');
      li.className = 'file-row';
      if (_fileSelected.has(it.id)) li.classList.add('selected');
      li.innerHTML =
        '<div class="file-thumb">' +
          (it.thumbnail_url
            ? '<img src="' + escape(Store.serverUrl + it.thumbnail_url) + '" alt="">'
            : icon) +
        '</div>' +
        '<div class="file-body">' +
          '<div class="file-name">' + escape(it.filename || 'file') + '</div>' +
          '<div class="file-meta">' +
            fmtBytes(it.file_size || it.size_bytes || 0) +
            ' · ' + escape(it.__channel_name || '') +
            ' · ' + formatTime(it.created_at) +
          '</div>' +
        '</div>' +
        '<button class="file-delete" data-del="' + escape(it.id) + '">✕</button>';
      li.addEventListener('click', (ev) => {
        if (ev.target.matches('[data-del]')) return;
        if (_fileSelected.has(it.id)) _fileSelected.delete(it.id);
        else _fileSelected.add(it.id);
        renderStorage();
      });
      frag.appendChild(li);
    }
    list.appendChild(frag);
    setText(document.getElementById('storageHint'),
      visible.length + ' items · ' + _fileSelected.size + ' selected',
      _fileSelected.size ? 'ok' : '');
  }

  async function deleteFile(id) {
    return Api.request('DELETE', '/api/files/' + encodeURIComponent(id));
  }

  async function openStorage(force) {
    show('storage');
    setText(document.getElementById('storageHint'), 'Scanning…');
    try {
      await scanStorage(force);
      updateStorageSummary();
      renderStorage();
      updateLocalStoragePanel();
    } catch (e) {
      setText(document.getElementById('storageHint'), e.message, 'err');
    }
  }
  function updateStorageSummary() {
    const items = _filesCache || [];
    const bytes = items.reduce((a, it) =>
      a + (it.file_size || it.size_bytes || 0), 0);
    const el = document.getElementById('profileStorageSummary');
    if (el) el.textContent = fmtBytes(bytes) + ' · ' + items.length + ' files';
  }
  function updateLocalStoragePanel() {
    // Browser localStorage used by the app.
    let cacheBytes = 0, prefsBytes = 0;
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        const v = localStorage.getItem(k) || '';
        const sz = (k.length + v.length) * 2;   // UTF-16
        if (k.startsWith('helen.cache') || k.startsWith('helen.msg')) {
          cacheBytes += sz;
        } else {
          prefsBytes += sz;
        }
      }
    } catch { /* ignore */ }
    const c = document.getElementById('localCacheSize');
    const p = document.getElementById('localPrefsSize');
    if (c) c.textContent = fmtBytes(cacheBytes);
    if (p) p.textContent = fmtBytes(prefsBytes);
  }

  document.getElementById('rowStorage').addEventListener('click',
    () => openStorage(true));
  document.getElementById('btnStorageRefresh').addEventListener('click',
    () => openStorage(true));

  // Filter-chip wiring (scope + type chips).
  document.querySelectorAll('[data-scope]').forEach((btn) => {
    if (btn.tagName !== 'BUTTON') return;
    btn.addEventListener('click', () => {
      document.querySelectorAll('button[data-scope]').forEach((b) =>
        b.classList.toggle('seg-active', b === btn));
      _fileFilterScope = btn.dataset.scope;
      renderStorage();
    });
  });
  document.querySelectorAll('.filter-chip[data-cat]').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.filter-chip[data-cat]').forEach((b) =>
        b.classList.toggle('seg-active', b === btn));
      _fileFilterCat = btn.dataset.cat;
      renderStorage();
    });
  });
  document.getElementById('btnSelectAll').addEventListener('click', () => {
    const visible = (_filesCache || []).filter(matchesFilter);
    const allAlready = visible.every((it) => _fileSelected.has(it.id));
    if (allAlready) _fileSelected.clear();
    else visible.forEach((it) => _fileSelected.add(it.id));
    renderStorage();
  });
  document.getElementById('btnDeleteFiltered').addEventListener('click', async () => {
    const ids = Array.from(_fileSelected);
    if (!ids.length) { toast('Nothing selected', 'err'); return; }
    if (!confirm('Delete ' + ids.length + ' file(s)? This cannot be undone.')) return;
    setText(document.getElementById('storageHint'), 'Deleting ' + ids.length + '…');
    let ok = 0, fail = 0;
    for (const id of ids) {
      const r = await deleteFile(id);
      if (r.ok || (r.status === 204)) ok++;
      else fail++;
    }
    _fileSelected.clear();
    toast(ok + ' deleted' + (fail ? ', ' + fail + ' failed' : ''),
          fail ? 'err' : 'ok');
    await openStorage(true);
  });
  document.getElementById('fileList').addEventListener('click', async (ev) => {
    const b = ev.target.closest('[data-del]');
    if (!b) return;
    ev.stopPropagation();
    if (!confirm('Delete this file?')) return;
    const r = await deleteFile(b.dataset.del);
    if (r.ok || r.status === 204) {
      toast('Deleted', 'ok');
      await openStorage(true);
    } else {
      toast(extractError(r), 'err');
    }
  });
  document.getElementById('btnClearLocal').addEventListener('click', () => {
    if (!confirm('Clear all cached messages on this device?')) return;
    const keep = {};
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && !k.startsWith('helen.cache') && !k.startsWith('helen.msg')) {
        keep[k] = localStorage.getItem(k);
      }
    }
    localStorage.clear();
    for (const [k, v] of Object.entries(keep)) localStorage.setItem(k, v);
    updateLocalStoragePanel();
    toast('Cache cleared', 'ok');
  });

  // ── Auto-cleanup screen ─────────────────────────────────────────
  function wireCleanupUi() {
    document.querySelectorAll('input[name="cleanupAge"]').forEach((r) => {
      r.checked = (r.value === _cleanupPrefs.age);
      r.addEventListener('change', () => {
        if (r.checked) {
          _cleanupPrefs.age = r.value;
          saveCleanupPrefs(_cleanupPrefs);
          updateCleanupSummary();
        }
      });
    });
    document.querySelectorAll('input[type="checkbox"][data-scope]').forEach((c) => {
      c.checked = !!_cleanupPrefs.scopes[c.dataset.scope];
      c.addEventListener('change', () => {
        _cleanupPrefs.scopes[c.dataset.scope] = c.checked;
        saveCleanupPrefs(_cleanupPrefs);
        updateCleanupSummary();
      });
    });
    document.querySelectorAll('input[type="checkbox"][data-type]').forEach((c) => {
      c.checked = !!_cleanupPrefs.types[c.dataset.type];
      c.addEventListener('change', () => {
        _cleanupPrefs.types[c.dataset.type] = c.checked;
        saveCleanupPrefs(_cleanupPrefs);
        updateCleanupSummary();
      });
    });
    document.getElementById('btnRunCleanupNow').addEventListener('click',
      () => runCleanup(true));
  }
  let _cleanupUiWired = false;
  function openCleanup() {
    if (!_cleanupUiWired) { wireCleanupUi(); _cleanupUiWired = true; }
    renderCleanupLog();
    show('autoCleanup');
  }
  function renderCleanupLog() {
    const log = loadCleanupLog();
    document.getElementById('cleanupLastRun').textContent =
      log.at ? formatTime(log.at) + ' · ' + new Date(log.at).toDateString() : 'never';
    document.getElementById('cleanupLastCount').textContent =
      log.count != null ? String(log.count) : '—';
  }
  document.getElementById('rowAutoCleanup').addEventListener('click', openCleanup);

  async function runCleanup(interactive) {
    if (_cleanupPrefs.age === 'never') {
      if (interactive) {
        setText(document.getElementById('cleanupStatus'),
          'Auto-cleanup is off. Pick a retention period first.', 'err');
      }
      return { count: 0, reason: 'off' };
    }
    if (!enabledScopeCount(_cleanupPrefs) || !enabledTypeCount(_cleanupPrefs)) {
      if (interactive) {
        setText(document.getElementById('cleanupStatus'),
          'Pick at least one scope and one type.', 'err');
      }
      return { count: 0, reason: 'empty_prefs' };
    }
    const ageSec = AGE_TO_SEC[_cleanupPrefs.age] || 0;
    const cutoff = Date.now() - ageSec * 1000;
    if (interactive) {
      setText(document.getElementById('cleanupStatus'), 'Scanning files…');
    }
    try {
      await scanStorage(true);
    } catch (e) {
      if (interactive) {
        setText(document.getElementById('cleanupStatus'), e.message, 'err');
      }
      return { count: 0, reason: 'scan_failed' };
    }
    const victims = (_filesCache || []).filter((it) => {
      const t = it.media_type || 'document';
      if (!_cleanupPrefs.types[t]) return false;
      if (!_cleanupPrefs.scopes[it.__channel_type]) return false;
      const when = new Date(it.created_at || 0).getTime();
      if (!when || isNaN(when)) return false;
      return when < cutoff;
    });
    if (interactive && victims.length) {
      if (!confirm('Auto-cleanup will delete ' + victims.length +
                   ' file(s) older than ' + _cleanupPrefs.age + '. Continue?')) {
        setText(document.getElementById('cleanupStatus'), 'Cancelled.');
        return { count: 0, reason: 'cancelled' };
      }
    }
    let ok = 0, fail = 0;
    for (const it of victims) {
      if (interactive) {
        setText(document.getElementById('cleanupStatus'),
          'Deleting ' + (ok + fail + 1) + ' / ' + victims.length + '…');
      }
      const r = await deleteFile(it.id);
      if (r.ok || r.status === 204) ok++;
      else fail++;
    }
    // Also purge local message cache if enabled.
    let localPurged = 0;
    if (_cleanupPrefs.types.message) {
      try {
        for (let i = localStorage.length - 1; i >= 0; i--) {
          const k = localStorage.key(i);
          if (!k || (!k.startsWith('helen.cache') && !k.startsWith('helen.msg'))) continue;
          try {
            const o = JSON.parse(localStorage.getItem(k));
            const when = new Date(o && o.created_at).getTime();
            if (when && when < cutoff) { localStorage.removeItem(k); localPurged++; }
          } catch { /* skip */ }
        }
      } catch { /* ignore */ }
    }
    saveCleanupLog({ at: Date.now(), count: ok, failed: fail, local: localPurged });
    if (interactive) {
      setText(document.getElementById('cleanupStatus'),
        'Done · ' + ok + ' deleted' + (fail ? ', ' + fail + ' failed' : '') +
        (localPurged ? ' · ' + localPurged + ' local' : ''), 'ok');
      renderCleanupLog();
    }
    return { count: ok, failed: fail };
  }

  // fmtBytes — shared helper. Re-declare if not already present.
  function fmtBytes(n) {
    if (n == null) return '—';
    if (n === 0) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + ' ' + u[i];
  }

  // Set CSS vars for legend dot colors to match .seg-X.
  document.documentElement.style.setProperty('--seg-color-image', '#4cc2ff');
  document.documentElement.style.setProperty('--seg-color-video', '#7c5cff');
  document.documentElement.style.setProperty('--seg-color-audio', '#5cd7a5');
  document.documentElement.style.setProperty('--seg-color-document', '#f5a524');

  // On startup / login: if auto-cleanup is enabled, run it silently.
  async function maybeRunAutoCleanupOnStartup() {
    if (!Store.token) return;
    if (_cleanupPrefs.age === 'never') return;
    if (!enabledScopeCount(_cleanupPrefs) || !enabledTypeCount(_cleanupPrefs)) return;
    const log = loadCleanupLog();
    // Don't re-run more often than once every 6 hours.
    if (log.at && (Date.now() - log.at) < 6 * 3600 * 1000) return;
    try { await runCleanup(false); } catch { /* silent */ }
  }
  // Kick off after a short delay to avoid blocking the first render.
  setTimeout(maybeRunAutoCleanupOnStartup, 4000);

  // Stop preview whenever we leave the media screen.
  const _showOrig = show;
  show = function (name, opts) {
    if (name !== 'media') stopPreview();
    return _showOrig(name, opts);
  };

  // 4.4 Chat.
  const chatLog = document.getElementById('chatLog');
  const chatTitle = document.getElementById('chatTitle');
  const composerForm = document.getElementById('composerForm');
  const composerInput = document.getElementById('composerInput');

  let currentChannel = null;
  let _replyTo = null;        // {id, content, sender_id, sender_name}
  let _editTarget = null;     // {id, oldContent}
  const _renderedMessages = new Map();  // msg_id → li element
  let _lastDividerKey = '';

  function _dayKey(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toISOString().slice(0, 10);
  }
  function _dayLabel(iso) {
    const k = _dayKey(iso);
    if (!k) return '';
    const today = new Date().toISOString().slice(0, 10);
    const ydate = new Date(); ydate.setDate(ydate.getDate() - 1);
    const yest = ydate.toISOString().slice(0, 10);
    if (k === today) return 'Today';
    if (k === yest)  return 'Yesterday';
    const d = new Date(iso);
    return d.toLocaleDateString([], {weekday:'short', month:'short', day:'numeric'});
  }
  function _hhmm(iso) {
    if (!iso) return '';
    try {
      return new Date(iso).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    } catch { return ''; }
  }

  function openChannel(c) {
    currentChannel = c;
    Store.activeChannel = c.id;
    chatTitle.textContent = channelDisplayName(c);
    // Clear the unread badge — we're about to read every queued
    // message in this channel.
    if (c.unread_count) c.unread_count = 0;
    const li = channelList?.querySelector(`li[data-id="${c.id}"]`);
    const badge = li?.querySelector('.ch-badge.unread');
    if (badge) badge.remove();
    show('chat');
    chatLog.innerHTML = '<li class="chat-msg them">Loading messages…</li>';
    _renderedMessages.clear();
    _lastDividerKey = '';
    cancelReply(); cancelEdit();
    Api.messages(c.id, 50).then((r) => {
      chatLog.innerHTML = '';
      if (!r.ok) {
        chatLog.innerHTML = '<li class="chat-msg them">Could not load history</li>';
        return;
      }
      const msgs = (r.data && r.data.messages) || r.data || [];
      // Server returns newest-first; we render oldest-first.
      const list = Array.isArray(msgs) ? msgs.slice().reverse() : [];
      for (const m of list) appendMessage(m);
      chatLog.scrollTop = chatLog.scrollHeight;
    });
  }

  function appendMessage(m) {
    if (!m || !m.id && !m.client_message_id) {
      // Optimistic local — minimal render
      const li = document.createElement('li');
      const me = Store.user && m.sender_id === Store.user.id;
      li.className = 'chat-msg ' + (me ? 'me' : 'them');
      li.textContent = m.content || m.body || '';
      chatLog.appendChild(li);
      return;
    }
    const id = m.id || m.client_message_id;
    if (_renderedMessages.has(id)) {
      // Update existing (e.g. edited)
      _updateMessageNode(_renderedMessages.get(id), m);
      return;
    }
    // Day divider
    const dk = _dayKey(m.created_at);
    if (dk && dk !== _lastDividerKey) {
      const div = document.createElement('li');
      div.className = 'chat-divider';
      div.textContent = _dayLabel(m.created_at);
      chatLog.appendChild(div);
      _lastDividerKey = dk;
    }
    const li = document.createElement('li');
    const me = Store.user && m.sender_id === Store.user.id;
    li.className = 'chat-msg ' + (me ? 'me' : 'them');
    li.dataset.msgId = id;
    li.dataset.senderId = m.sender_id || '';
    _updateMessageNode(li, m);
    _attachLongPress(li, m);
    chatLog.appendChild(li);
    _renderedMessages.set(id, li);
  }

  function _updateMessageNode(li, m) {
    const reply = m.reply_to || (m.reply_to_id ? { id: m.reply_to_id } : null);
    const reactions = m.reactions || {};
    const editedAt = m.edited_at;
    const content  = m.content || m.body || '';

    // Build reply quote if any
    let replyHtml = '';
    if (reply && (reply.content || reply.id)) {
      replyHtml = '<div class="chat-reply-quote">' +
        '<span class="rq-author">' + escape(reply.sender_name || 'reply') + '</span>' +
        '<span class="rq-text">' + escape((reply.content || '...').slice(0, 80)) + '</span>' +
      '</div>';
    }

    // Build reactions row
    let reactionsHtml = '';
    const rEntries = Object.entries(reactions);
    if (rEntries.length) {
      reactionsHtml = '<div class="chat-reactions">' +
        rEntries.map(([emoji, info]) => {
          const count = (info && info.count) || (Array.isArray(info) ? info.length : 1);
          return '<span class="reaction-chip" data-emoji="' + escape(emoji) + '">' +
                 escape(emoji) + ' <em>' + count + '</em></span>';
        }).join('') +
      '</div>';
    }

    // Footer: time + edited mark + read state (single-tick if me)
    const me = Store.user && m.sender_id === Store.user.id;
    let foot = '<span class="chat-time">' + escape(_hhmm(m.created_at)) + '</span>';
    if (editedAt) foot += '<span class="chat-edited">edited</span>';
    if (me && m.status) foot += '<span class="chat-tick">' + (m.status === 'read' ? '✓✓' : '✓') + '</span>';

    // File attachment rendering — image preview or file chip
    let fileHtml = '';
    if (m.file_id) {
      const isImage = (m.type === 'image') ||
                      (m.mime_type && m.mime_type.startsWith('image/'));
      if (isImage) {
        fileHtml = '<a class="chat-image-wrap" href="' + escape(Api.fileUrl(m.file_id)) +
                   '" target="_blank" rel="noopener">' +
                   '<img class="chat-image" src="' + escape(Api.thumbUrl(m.file_id)) +
                   '" alt="' + escape(m.file_name || '') + '" />' +
                   '</a>';
      } else {
        const sz = m.file_size ? Math.round(m.file_size/1024) + ' KB' : '';
        fileHtml = '<a class="chat-file-chip" href="' + escape(Api.fileUrl(m.file_id)) +
                   '" download target="_blank" rel="noopener">' +
                   '<span class="chat-file-icon">📎</span>' +
                   '<span class="chat-file-meta">' +
                     '<span class="chat-file-name">' + escape(m.file_name || 'file') + '</span>' +
                     '<span class="chat-file-size">' + sz + '</span>' +
                   '</span>' +
                   '</a>';
      }
    }

    li.innerHTML =
      replyHtml +
      fileHtml +
      (content ? '<div class="chat-body">' + escape(content) + '</div>' : '') +
      reactionsHtml +
      '<div class="chat-foot">' + foot + '</div>';
  }

  // ── Long-press context menu ─────────────────────────────────
  let _lpTimer = null;
  function _attachLongPress(node, m) {
    const start = (ev) => {
      _lpTimer = setTimeout(() => openMessageMenu(m, ev), 500);
    };
    const cancel = () => { if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; } };
    node.addEventListener('pointerdown', start);
    node.addEventListener('pointerup', cancel);
    node.addEventListener('pointerleave', cancel);
    node.addEventListener('pointermove', cancel);
    node.addEventListener('contextmenu', (ev) => {
      ev.preventDefault();
      openMessageMenu(m, ev);
    });
    // Tap-to-react quick: double-tap on bubble adds 👍
    let lastTap = 0;
    node.addEventListener('click', () => {
      const now = Date.now();
      if (now - lastTap < 350) reactMessage(m, '👍');
      lastTap = now;
    });
  }

  function openMessageMenu(m, ev) {
    closeMessageMenu();
    const me = Store.user && m.sender_id === Store.user.id;
    const menu = document.createElement('div');
    menu.id = 'chatCtxMenu';
    menu.className = 'ctx-menu';
    const buttons = [
      { id:'reply',   label:'↩ Reply',  fn: () => beginReply(m) },
      { id:'react',   label:'😀 React', fn: () => openReactionPicker(m) },
      { id:'copy',    label:'📋 Copy',  fn: () => copyText(m.content || '') },
      { id:'forward', label:'➤ Forward',fn: () => forwardMessage(m) },
      { id:'pin',     label:'📌 Pin',    fn: () => pinMessage(m) },
    ];
    if (me) {
      buttons.push({ id:'edit',   label:'✎ Edit',   fn: () => beginEdit(m) });
      buttons.push({ id:'delete', label:'🗑 Delete', fn: () => deleteMessage(m), danger: true });
    }
    menu.innerHTML = buttons.map(b =>
      '<button data-act="'+b.id+'"'+(b.danger?' class="danger"':'')+'>'+b.label+'</button>'
    ).join('');
    document.body.appendChild(menu);
    // Position near pointer or center
    const x = ev && ev.clientX ? ev.clientX : window.innerWidth/2;
    const y = ev && ev.clientY ? ev.clientY : window.innerHeight/2;
    const rect = menu.getBoundingClientRect();
    menu.style.left = Math.max(8, Math.min(window.innerWidth - rect.width - 8, x - rect.width/2)) + 'px';
    menu.style.top  = Math.max(8, Math.min(window.innerHeight - rect.height - 8, y - rect.height - 4)) + 'px';
    menu.querySelectorAll('button').forEach(b => {
      b.addEventListener('click', () => {
        const fn = (buttons.find(x => x.id === b.dataset.act) || {}).fn;
        closeMessageMenu();
        if (fn) fn();
      });
    });
    // Click outside to close
    setTimeout(() => {
      document.addEventListener('pointerdown', closeMessageMenu, { once: true });
    }, 0);
  }
  function closeMessageMenu() {
    const m = document.getElementById('chatCtxMenu');
    if (m) m.remove();
  }

  function copyText(text) {
    try { navigator.clipboard.writeText(text); toast('Copied', 'ok'); } catch {}
  }

  function beginReply(m) {
    _replyTo = {
      id: m.id, content: m.content || '',
      sender_id: m.sender_id, sender_name: m.sender_name || 'message',
    };
    _renderReplyPreview();
    composerInput.focus();
  }
  function cancelReply() {
    _replyTo = null;
    _renderReplyPreview();
  }
  function _renderReplyPreview() {
    let host = document.getElementById('replyPreview');
    if (!host) {
      host = document.createElement('div');
      host.id = 'replyPreview';
      host.className = 'reply-preview';
      composerForm.parentNode.insertBefore(host, composerForm);
    }
    if (!_replyTo) { host.innerHTML = ''; host.style.display = 'none'; return; }
    host.style.display = 'flex';
    host.innerHTML =
      '<div class="rp-line"></div>' +
      '<div class="rp-body">' +
        '<div class="rp-author">↩ ' + escape(_replyTo.sender_name) + '</div>' +
        '<div class="rp-text">' + escape((_replyTo.content || '').slice(0, 100)) + '</div>' +
      '</div>' +
      '<button class="rp-close" type="button">✕</button>';
    host.querySelector('.rp-close').addEventListener('click', cancelReply);
  }

  function beginEdit(m) {
    _editTarget = { id: m.id, oldContent: m.content || '' };
    composerInput.value = m.content || '';
    composerInput.focus();
    let host = document.getElementById('editBanner');
    if (!host) {
      host = document.createElement('div');
      host.id = 'editBanner';
      host.className = 'edit-banner';
      composerForm.parentNode.insertBefore(host, composerForm);
    }
    host.style.display = 'flex';
    host.innerHTML = '<span>✎ Editing message — press Send to save</span>' +
                     '<button class="rp-close" type="button">✕</button>';
    host.querySelector('.rp-close').addEventListener('click', cancelEdit);
  }
  function cancelEdit() {
    _editTarget = null;
    const host = document.getElementById('editBanner');
    if (host) { host.style.display = 'none'; host.innerHTML = ''; }
    composerInput.value = '';
  }

  async function deleteMessage(m) {
    if (!confirm('Delete this message?')) return;
    const li = _renderedMessages.get(m.id);
    const r = await Api.deleteMessage(m.id);
    if (r.ok || r.status === 204) {
      if (li) li.remove();
      _renderedMessages.delete(m.id);
      toast('Deleted', 'ok');
    } else {
      toast(extractError(r) || 'Delete failed', 'err');
    }
  }

  async function reactMessage(m, emoji) {
    const r = await Api.reactMessage(m.id, emoji);
    if (r.ok) {
      const li = _renderedMessages.get(m.id);
      if (li) {
        // Optimistic local: increment that emoji count in DOM
        const reactions = m.reactions || (m.reactions = {});
        const cur = reactions[emoji];
        const cnt = cur ? ((cur.count || 0) + 1) : 1;
        reactions[emoji] = { count: cnt };
        _updateMessageNode(li, m);
      }
      haptic();
    } else {
      toast(extractError(r), 'err');
    }
  }

  function openReactionPicker(m) {
    const picker = document.createElement('div');
    picker.className = 'ctx-menu reaction-picker';
    const emojis = ['👍','❤️','😂','😮','😢','🔥','🎉','👏'];
    picker.innerHTML = emojis.map(e =>
      '<button data-e="'+e+'">'+e+'</button>'
    ).join('');
    document.body.appendChild(picker);
    picker.style.left = Math.max(8, window.innerWidth/2 - picker.offsetWidth/2) + 'px';
    picker.style.top  = Math.max(8, window.innerHeight/2 - picker.offsetHeight/2) + 'px';
    picker.querySelectorAll('button').forEach(b => {
      b.addEventListener('click', () => {
        reactMessage(m, b.dataset.e);
        picker.remove();
      });
    });
    setTimeout(() => document.addEventListener('pointerdown',
      () => picker.remove(), { once: true }), 0);
  }

  async function pinMessage(m) {
    const r = await Api.pinMessage(m.id);
    if (r.ok) toast('Pinned', 'ok'); else toast(extractError(r), 'err');
  }

  async function forwardMessage(m) {
    const target = prompt('Forward to channel ID:');
    if (!target) return;
    const r = await Api.forwardMessage(m.id, target.trim());
    if (r.ok) toast('Forwarded', 'ok'); else toast(extractError(r), 'err');
  }

  /**
   * One pipeline for every incoming message — whether the user is
   * looking at the chat right now or not. Keeps the previous "render
   * inline" behavior, then layers system-level signals (badge,
   * sound, browser notification, toast) for chats the user isn't
   * actively viewing. Without this, messages received while the user
   * was on the Chats list, the Profile screen, or in a different chat
   * just disappeared silently.
   */
  function onIncomingMessage(m) {
    if (!m || !m.channel_id) return;

    const me = Store.user && Store.user.id;
    const fromMe = m.sender_id === me;

    // Always render inline if we're looking at this chat.
    if (currentChannel && m.channel_id === currentChannel.id) {
      appendMessage(m);
      chatLog.scrollTop = chatLog.scrollHeight;
      haptic();
      return;
    }

    // Skip echoes of our own messages — server fans them out for
    // multi-device sync, but we don't want to "notify ourselves".
    if (fromMe) return;

    // Resolve the sender's display name. The message payload usually
    // includes `sender_name`; if not, fall back to a member lookup
    // against the cached channels list.
    const sender = resolveSenderName(m);
    const channel = (allChannels || []).find((c) => c.id === m.channel_id);
    const channelName = channel ? channelDisplayName(channel) : 'New chat';
    const titleLine = (channel && channel.type !== 'dm')
        ? `${sender} · ${channelName}`
        : sender;

    const preview = previewForMessage(m);

    // 1. Native browser notification — the only thing the OS will
    //    surface when the page is unfocused.
    showSystemNotification(titleLine, preview, m.channel_id);

    // 2. Audible cue.
    playMessageBeep();

    // 3. In-page toast for users that haven't granted permission yet.
    toast(`${titleLine}: ${preview}`);

    // 4. Bump the channel-row badge on the Chats list.
    bumpChannelUnread(m.channel_id);

    haptic();
  }

  function resolveSenderName(m) {
    if (m.sender_name) return m.sender_name;
    const channels = allChannels || [];
    for (const c of channels) {
      if (c.id !== m.channel_id) continue;
      const sender = (c.members || []).find((mem) => mem.user_id === m.sender_id);
      if (sender) return sender.display_name || sender.username || 'Someone';
    }
    return 'Someone';
  }

  function previewForMessage(m) {
    if (m.type === 'image' || (m.file_id && /image|photo/i.test(m.content || ''))) return '🖼 Photo';
    if (m.type === 'file'  || m.file_id) return '📎 File';
    const text = String(m.content || '');
    return text.length > 80 ? text.slice(0, 80) + '…' : text;
  }

  let _notifyPermissionAsked = false;
  function showSystemNotification(title, body, channelId) {
    if (typeof Notification === 'undefined') return;
    if (Notification.permission === 'granted') {
      try {
        const n = new Notification(title, {
          body,
          tag: 'helen:msg:' + channelId,   // collapse repeats per chat
          renotify: false,
        });
        n.onclick = () => {
          window.focus();
          // Jump straight into the chat the message came from.
          const c = (allChannels || []).find((x) => x.id === channelId);
          if (c) openChannel(c);
          n.close();
        };
      } catch { /* some browsers block constructor on insecure origins */ }
      return;
    }
    if (Notification.permission === 'default' && !_notifyPermissionAsked) {
      _notifyPermissionAsked = true;
      Notification.requestPermission().catch(() => {});
    }
  }

  // Soft beep — same primitive as the incoming-call ring, but quieter
  // and shorter so a barrage of messages doesn't become annoying.
  function playMessageBeep() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const o = ctx.createOscillator(); o.frequency.value = 660;
      const g = ctx.createGain(); g.gain.value = 0.04;
      o.connect(g).connect(ctx.destination);
      o.start(); o.stop(ctx.currentTime + 0.1);
      setTimeout(() => ctx.close(), 250);
    } catch { /* audio unavailable */ }
  }

  // Bump the unread badge on the Chats list row so the user sees the
  // chat needs attention without waiting for the next /api/channels
  // poll. Safe no-op if the row isn't currently in the DOM.
  function bumpChannelUnread(channelId) {
    const c = (allChannels || []).find((x) => x.id === channelId);
    if (c) c.unread_count = (c.unread_count || 0) + 1;
    const li = channelList?.querySelector(`li[data-id="${channelId}"]`);
    if (!li) return;
    let badge = li.querySelector('.ch-badge.unread');
    if (!badge) {
      const meta = li.querySelector('.ch-meta');
      if (!meta) return;
      badge = document.createElement('span');
      badge.className = 'ch-badge unread';
      meta.insertBefore(badge, meta.firstChild);
    }
    badge.textContent = c ? c.unread_count : (parseInt(badge.textContent || '0', 10) + 1);
  }

  // ── Send wiring ──────────────────────────────────────────
  //
  // Headless repro proved that neither the button's native `click`
  // → form-submit chain NOR `pointerup`-on-button → requestSubmit()
  // were firing the submit listener (despite `requestSubmit()` working
  // when called from outside the IIFE). The most likely culprit is the
  // chat-screen flex layout intercepting the click somewhere.
  //
  // Bypass the form-submit dance entirely: call a single `_sendNow()`
  // function directly from the button click, the input's Enter
  // keydown, and (as a final safety net) the form's submit event.
  // No more guesswork.
  async function _sendNow() {
    if (!currentChannel) {
      toast('No channel open', 'err');
      console.warn('[send] no currentChannel');
      return;
    }
    const text = composerInput.value.trim();
    if (!text) {
      console.warn('[send] empty input');
      return;
    }
    return _runSend(text);
  }

  document.querySelector('.composer-send')?.addEventListener('click', (ev) => {
    ev.preventDefault();
    _sendNow();
  });
  composerInput.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
      ev.preventDefault();
      _sendNow();
    }
  });
  composerForm.addEventListener('submit', (ev) => {
    ev.preventDefault();
    _sendNow();
  });

  async function _runSend(text) {
    // Edit mode — patch existing message and bail out before the
    // socket-send path. The composer is reused for editing; we
    // wouldn't want both behaviours to run.
    if (_editTarget) {
      const target = _editTarget;
      composerInput.value = '';
      const r = await Api.editMessage(target.id, text);
      if (r.ok) {
        const li = _renderedMessages.get(target.id);
        if (li) _updateMessageNode(li, {
          ...r.data, content: text, edited_at: new Date().toISOString(),
          sender_id: Store.user && Store.user.id,
        });
        toast('Edited', 'ok');
      } else {
        toast(extractError(r) || 'Edit failed', 'err');
        composerInput.value = text;
      }
      cancelEdit();
      return;
    }

    if (!sock || !sockReady) {
      toast('Not connected — reconnecting…', 'err');
      console.warn('[send] socket not ready', {sock: !!sock, sockReady});
      try { connectSocket(); } catch {}
      return;
    }

    // Server expects `client_id` (not `client_message_id`) for
    // deduplication, and `reply_to` (not `reply_to_id`).
    console.log('[send] →', currentChannel.id.slice(0, 8), text.slice(0, 40));
    const cid = 'cid_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
    const payload = {
      channel_id: currentChannel.id,
      content: text,
      client_id: cid,
    };
    if (_replyTo) payload.reply_to = _replyTo.id;
    const replyForOptimistic = _replyTo;
    composerInput.value = '';
    cancelReply();

    sock.emit('v2_chat_send_message', payload, (ack) => {
      if (ack && ack.error) {
        toast(ack.error, 'err');
        const li = _renderedMessages.get(cid);
        if (li) { li.remove(); _renderedMessages.delete(cid); }
        return;
      }
      // Rekey the optimistic node to the real server id so the broadcast
      // (which arrives via `v2_chat:new_message`) updates the same node
      // instead of inserting a duplicate.
      if (ack && ack.message_id) {
        const li = _renderedMessages.get(cid);
        if (li) {
          li.dataset.msgId = ack.message_id;
          _renderedMessages.delete(cid);
          _renderedMessages.set(ack.message_id, li);
        }
      }
    });
    appendMessage({
      id: cid,
      content: text,
      sender_id: Store.user && Store.user.id,
      created_at: new Date().toISOString(),
      reply_to: replyForOptimistic,
    });
    chatLog.scrollTop = chatLog.scrollHeight;
    haptic();
  }

  // 4.5 Network.
  const netServerUrl = document.getElementById('netServerUrl');
  const netStatus = document.getElementById('netStatus');
  const netLatency = document.getElementById('netLatency');
  const netTransport = document.getElementById('netTransport');
  const netPresence = document.getElementById('netPresence');
  const pathList = document.getElementById('pathList');
  const bridgeList = document.getElementById('bridgeList');

  async function refreshNetwork() {
    netServerUrl.textContent = Store.serverUrl;
    const t0 = performance.now();
    const r = await Api.health();
    const rtt = Math.round(performance.now() - t0);
    if (r.ok) {
      netStatus.textContent = 'online — ' + (r.data.service || 'Helen');
      netPresence.className = 'presence-dot ok';
      netLatency.textContent = rtt + ' ms';
    } else {
      netStatus.textContent = 'unreachable';
      netPresence.className = 'presence-dot bad';
      netLatency.textContent = '—';
    }
    netTransport.textContent = sockReady ? 'Socket.IO / WebSocket'
                                           : (sock ? 'reconnecting' : 'idle');
    // Path info from /api/discovery (includes LAN IP + HTTPS URL if on)
    const d = await Api.discovery();
    if (d.ok) {
      const lines = [];
      lines.push(row('LAN IP', d.data.host));
      if (d.data.port) lines.push(row('HTTP port', d.data.port));
      if (d.data.https_url) lines.push(row('HTTPS URL', d.data.https_url));
      if (d.data.server_id) lines.push(row('Server ID', shorten(d.data.server_id, 20)));
      pathList.innerHTML = lines.join('');
    } else {
      pathList.innerHTML = '<p class="muted">No discovery info available.</p>';
    }
    // Federation bridges panel (admin-only)
    const b = await Api.request('GET', '/api/admin/federation/bridges');
    if (b.ok) {
      const rows = (b.data && b.data.bridges) || [];
      if (!rows.length) bridgeList.innerHTML = '<p class="muted">No bridges yet.</p>';
      else bridgeList.innerHTML = rows.map((br) =>
        row(br.name || br.server_id.slice(0, 12),
            'in=' + br.emits_received + ' / out=' + br.emits_sent)
      ).join('');
    } else {
      bridgeList.innerHTML = '<p class="muted">Admin role required.</p>';
    }
  }
  function row(k, v) {
    return '<div class="stat-row"><span class="stat-label">' + escape(k) +
           '</span><span class="stat-value mono">' + escape(v != null ? v : '—') +
           '</span></div>';
  }

  // 4.6 Profile.
  function renderProfile() {
    const u = Store.user || {};
    document.getElementById('profileName').textContent = u.display_name || u.username || '—';
    document.getElementById('profileDisplay').textContent = u.display_name || '—';
    document.getElementById('profileRole').textContent = u.role || 'user';
    document.getElementById('profileId').textContent = shorten(u.id || '—', 20);
    document.getElementById('profileServer').textContent = Store.serverUrl;
    document.getElementById('profileAvatar').textContent = '';
    updateMediaSummary();
    updateStorageSummary();
    updateCleanupSummary();
  }
  document.getElementById('btnLogout').addEventListener('click', () => {
    if (!confirm('Sign out?')) return;
    disconnectSocket();
    Store.clear();
    toast('Signed out');
    show('onboarding');
  });
  // Edit profile — two prompts (display name, username). Both optional;
  // pressing Cancel on either keeps that field unchanged. We call the
  // server's PATCH /api/users/me with only the fields the user actually
  // changed, then update local Store + the rendered profile fields so
  // the new identity is visible immediately without a reload.
  // Camera & microphone picker. Lists every input the OS exposes
  // (built-in webcam, USB cameras, iPhone Continuity Camera, OBS
  // virtual cam, Bluetooth headsets, USB mics, etc.). The chosen
  // deviceId is persisted in localStorage so the next call uses it
  // automatically.
  //
  // Permission gotcha: device labels are blank until the user has
  // granted camera/mic at least once in this origin. We trigger a
  // throwaway getUserMedia first if labels look empty.
  document.getElementById('btnPickDevices').addEventListener('click', async () => {
    let { audio, video } = await listMediaDevices();
    const needsPermission = (audio.length || video.length) &&
      [...audio, ...video].every((d) => !d.label);
    if (needsPermission) {
      try {
        const probe = await navigator.mediaDevices.getUserMedia({
          audio: true, video: true,
        });
        for (const t of probe.getTracks()) t.stop();
        ({ audio, video } = await listMediaDevices());
      } catch {
        toast('Allow camera + mic so devices can be listed', 'err');
      }
    }
    const curA = localStorage.getItem('helen.preferredAudioInput') || '';
    const curV = localStorage.getItem('helen.preferredVideoInput') || '';

    const buildPrompt = (kind, list, current) => {
      if (!list.length) return null;
      const lines = list.map((d, i) =>
        `${i + 1}. ${d.deviceId === current ? '✓ ' : '  '}${d.label || '(unlabeled)'}`
      );
      return `Pick ${kind} (1–${list.length}, blank=keep, 0=auto):\n` + lines.join('\n');
    };

    if (video.length) {
      const ans = prompt(buildPrompt('camera', video, curV));
      if (ans !== null && ans !== '') {
        const idx = parseInt(ans, 10);
        if (idx === 0) localStorage.removeItem('helen.preferredVideoInput');
        else if (idx >= 1 && idx <= video.length) {
          localStorage.setItem('helen.preferredVideoInput', video[idx - 1].deviceId);
        }
      }
    }
    if (audio.length) {
      const ans = prompt(buildPrompt('microphone', audio, curA));
      if (ans !== null && ans !== '') {
        const idx = parseInt(ans, 10);
        if (idx === 0) localStorage.removeItem('helen.preferredAudioInput');
        else if (idx >= 1 && idx <= audio.length) {
          localStorage.setItem('helen.preferredAudioInput', audio[idx - 1].deviceId);
        }
      }
    }
    toast('Devices saved — used on next call', 'ok');
  });

  document.getElementById('btnEditProfile').addEventListener('click', async () => {
    const u = Store.user || {};
    const newDisplay = prompt('Display name:', u.display_name || '');
    if (newDisplay === null) return;
    const newUsername = prompt(
      'Username (lowercase letters, numbers, "._-"):',
      u.username || '',
    );
    if (newUsername === null) return;

    const fields = {};
    const trimDisplay = (newDisplay || '').trim();
    const trimUser    = (newUsername || '').trim();
    if (trimDisplay && trimDisplay !== u.display_name) fields.display_name = trimDisplay;
    if (trimUser && trimUser    !== u.username)        fields.username     = trimUser;
    if (Object.keys(fields).length === 0) {
      toast('Nothing changed', 'ok');
      return;
    }
    if (fields.username && !/^[a-zA-Z0-9_.\-]{3,64}$/.test(fields.username)) {
      toast('Username must be 3–64 chars (letters/numbers/._-)', 'err');
      return;
    }
    const r = await Api.updateMe(fields);
    if (r.ok) {
      // Merge response into local Store so the next render sees the new
      // identity without a roundtrip.
      const updated = (r.data && r.data.user) || r.data || {};
      Store.user = { ...u, ...fields, ...updated };
      toast('Profile updated', 'ok');
      // Refresh visible fields right now.
      try { renderProfile(); } catch {}
      // And redraw the chats list header that shows "Signed in as …".
      try { loadChannels(); } catch {}
    } else {
      toast(extractError(r) || 'Update failed', 'err');
    }
  });
  document.getElementById('btnChangePassword').addEventListener('click', async () => {
    // Two-prompt flow keeps the markup minimal — no extra screen needed.
    // The server still demands the current password, so a stolen token
    // alone can't rotate the credential.
    const cur = prompt('Current password:');
    if (cur === null) return;
    const next = prompt('New password (at least 8 characters):');
    if (next === null) return;
    if (!next || next.length < 8) {
      toast('Password must be at least 8 characters', 'err');
      return;
    }
    const r = await Api.request('POST', '/api/auth/change-password', {
      current_password: cur,
      new_password:     next,
    });
    if (r.ok || r.status === 204) {
      toast('Password updated', 'ok');
    } else if (r.status === 401) {
      toast('Current password is incorrect', 'err');
    } else {
      toast(extractError(r) || 'Failed to update password', 'err');
    }
  });

  // ── 5. Small helpers ──────────────────────────────────────────

  function escape(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function shorten(s, n) {
    s = String(s || '');
    return s.length > n ? s.slice(0, n) + '…' : s;
  }
  /**
   * Channel display name. For groups: the explicit name. For DMs: the
   * other participant's display_name / username. Falls back to the
   * channel id if both are missing (extreme edge case).
   */
  function channelDisplayName(c) {
    if (!c) return 'Chat';
    if (c.name) return c.name;
    if (c.type === 'dm' && Array.isArray(c.members)) {
      const me = Store.user && Store.user.id;
      const other = c.members.find((m) => m.user_id !== me);
      if (other) return other.display_name || other.username || 'Direct chat';
    }
    return c.type === 'dm' ? 'Direct chat' : (c.id ? shorten(c.id, 8) : 'Chat');
  }
  /**
   * Full @handle for a user. Prefers the 64-char share_code (mathematically
   * unique, alphanumeric) over the short username. Falls back to username
   * when share_code isn't returned by an endpoint (e.g. channel members).
   */
  /**
   * "X min/h/d ago" — matches the formatter in the desktop client and
   * the admin panel so every UI surface phrases offline duration the
   * same way.
   */
  function fmtLastSeen(iso) {
    if (!iso) return 'a while ago';
    const t = Date.parse(iso); if (Number.isNaN(t)) return 'a while ago';
    const s = Math.max(1, Math.floor((Date.now() - t) / 1000));
    if (s < 60)      return 'just now';
    if (s < 3600)    return Math.floor(s / 60)    + ' min ago';
    if (s < 86400)   return Math.floor(s / 3600)  + ' h ago';
    if (s < 604800)  return Math.floor(s / 86400) + ' d ago';
    return new Date(t).toLocaleDateString(undefined, { month:'short', day:'numeric' });
  }
  /**
   * Returns the inline HTML for a green/red presence dot + caption.
   *
   * Presence is decided **only** by the live socket roster — being in
   * `_liveOnline` means the user has an active connection right now.
   * The DB-cached `u.status` field is ignored on purpose: it can stay
   * "online" for minutes after a client crashes or loses network.
   *
   * `last_seen` is still read from the API response (it's only used
   * when the user is offline), and cached so subsequent re-renders
   * after socket events keep showing the same timestamp.
   */
  function presenceHtml(u) {
    if (!u) return '';
    const id = String(u.id || u.user_id || '');
    if (id && u.last_seen) _lastSeenCache.set(id, u.last_seen);
    const online = id ? _liveOnline.has(id) : false;
    const lastSeen = id ? _lastSeenCache.get(id) : u.last_seen;
    const text = online ? 'Online' : 'Last seen ' + fmtLastSeen(lastSeen);
    return '<span class="presence-line ' + (online ? 'ok' : 'bad') + '" '
         + 'title="' + escape(text) + '" data-presence-uid="' + escape(id) + '">'
         + '<i class="presence-pip"></i>' + escape(text) + '</span>';
  }
  // Live-update every rendered presence-line in the DOM whenever the
  // online roster changes — avoids needing to re-render the whole list.
  _presenceSubs.add(() => {
    document.querySelectorAll('[data-presence-uid]').forEach((el) => {
      const id = el.getAttribute('data-presence-uid') || '';
      const online = id ? _liveOnline.has(id) : false;
      const lastSeen = id ? _lastSeenCache.get(id) : null;
      const text = online ? 'Online' : 'Last seen ' + fmtLastSeen(lastSeen);
      el.className = 'presence-line ' + (online ? 'ok' : 'bad');
      el.setAttribute('title', text);
      // Replace the trailing text node (after the <i> pip) without nuking it.
      const pip = el.querySelector('i');
      el.textContent = '';
      if (pip) el.appendChild(pip); else el.innerHTML = '<i class="presence-pip"></i>';
      el.appendChild(document.createTextNode(text));
    });
  });
  function handleFull(u) {
    if (!u) return '';
    const code = u.share_code || u.code || u.share || '';
    if (code) return '@' + code;
    return '@' + (u.username || '');
  }
  /**
   * Compact preview of `handleFull` for places where 64 chars don't fit.
   * Shows `@<first8>…<last4>` for codes, `@username` untouched for short
   * fallbacks. The full string is preserved on the element's `title=` so
   * hover/long-press reveals it.
   */
  function handleShort(u) {
    const full = handleFull(u);            // includes leading '@'
    const body = full.slice(1);
    if (body.length <= 14) return full;    // short enough — show as-is
    return '@' + body.slice(0, 8) + '…' + body.slice(-4);
  }
  function initials(s) {
    const p = (s || '').trim().split(/\s+/).slice(0, 2);
    return p.map((w) => w[0] || '').join('').toUpperCase() || '?';
  }
  function formatTime(ts) {
    if (!ts) return '';
    try {
      const d = new Date(ts);
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch { return ''; }
  }
  function setText(el, msg, cls) {
    el.className = 'hint' + (cls ? ' ' + cls : '');
    el.textContent = msg || '';
  }
  function extractError(r) {
    if (!r) return 'unknown error';
    if (typeof r.data === 'string') return r.data;
    if (r.data && r.data.detail) return typeof r.data.detail === 'string'
      ? r.data.detail : JSON.stringify(r.data.detail);
    return 'HTTP ' + r.status;
  }
  function toast(msg, cls) {
    const t = document.createElement('div');
    t.className = 'toast ' + (cls || '');
    t.textContent = msg;
    const region = document.getElementById('toastRegion');
    region.appendChild(t);
    setTimeout(() => { t.remove(); }, 2600);
  }
  function haptic() {
    if (navigator.vibrate) navigator.vibrate(8);
  }
  function setPresenceDot(cls) {
    const el = document.getElementById('chatPresence');
    if (el) el.className = 'presence-dot ' + (cls || '');
  }
  function updateNetworkStatus(state, detail) {
    // Light-touch: leave the Network tab's fields to refreshNetwork.
    // Just flip the header presence dot on the chat screen.
    setPresenceDot(state === 'online' ? 'ok' : state === 'reconnecting' ? 'warn' : 'bad');
  }

  // ── 6. Boot ───────────────────────────────────────────────────

  function boot() {
    // Live status-bar clock.
    const clock = document.getElementById('statusTime');
    function tick() {
      const d = new Date();
      clock.textContent = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
    }
    tick(); setInterval(tick, 30000);

    // Default URL hint.
    onboardUrl.value = Store.serverUrl;

    // Optional ?demo=SCREEN query param to preview a specific screen
    // without walking the whole flow. Used for design reviews and
    // screenshot harvesting — the app still works normally when the
    // param is absent.
    const qs = new URLSearchParams(location.search);
    const demo = qs.get('demo');
    if (demo === 'auth') {
      show('auth', { onEnter: enterAuth });
      return;
    }
    if (demo === 'channels') {
      // Seed a few fake channels so the list has content in a
      // screenshot even if no Helen is reachable.
      allChannels = [
        { id: '1', name: 'Ahmed Saleh', type: 'dm',
          updated_at: new Date().toISOString() },
        { id: '2', name: 'Design Team', type: 'group',
          updated_at: new Date(Date.now() - 3.6e6).toISOString() },
        { id: '3', name: 'Ops bridge', type: 'group',
          updated_at: new Date(Date.now() - 2.4e7).toISOString() },
      ];
      show('channels');
      renderChannels(allChannels);
      return;
    }
    if (demo === 'chat') {
      currentChannel = { id: '1', name: 'Ahmed Saleh' };
      chatTitle.textContent = 'Ahmed Saleh';
      show('chat');
      const fakeMe = Store.user ? Store.user.id : 'me-id';
      Store.user = Store.user || { id: fakeMe, username: 'me' };
      const msgs = [
        { sender_id: 'other', content: 'Hey, the server\'s up on LAN?' },
        { sender_id: fakeMe,  content: 'Yep, 192.168.1.132:3000 — helen.local works too' },
        { sender_id: 'other', content: 'Perfect. I\'ll join in a sec.' },
        { sender_id: fakeMe,  content: '👍' },
      ];
      chatLog.innerHTML = '';
      for (const m of msgs) appendMessage(m);
      return;
    }
    if (demo === 'network') {
      show('network');
      netServerUrl.textContent = 'http://192.168.1.132:3000';
      netStatus.textContent = 'online — Helen Server';
      netPresence.className = 'presence-dot ok';
      netLatency.textContent = '3 ms';
      netTransport.textContent = 'Socket.IO / WebSocket';
      pathList.innerHTML =
        row('LAN IP', '192.168.1.132') +
        row('HTTP port', '3000') +
        row('HTTPS URL', 'https://192.168.1.132:3443') +
        row('Server ID', '6xxTjmjZmg0FHP948Dyi9…');
      bridgeList.innerHTML =
        row('Helen-Bravo',  'in=12 / out=4') +
        row('Helen-Charlie','in=0 / out=7') +
        row('Helen-Delta',  'in=5 / out=5');
      return;
    }
    if (demo === 'profile') {
      Store.user = Store.user || {
        id: 'f4e2c1a00000000000000000000000000000000000000000000000000000demo',
        username: 'yousef', display_name: 'Yousef', role: 'admin',
      };
      show('profile');
      renderProfile();
      return;
    }

    // Optional ?method=bridge|manual flips the onboarding pane on
    // load. Used by the screenshot harness; absent for normal users.
    const m = qs.get('method');
    if (m === 'bridge' || m === 'manual') {
      const btn = document.querySelector('.seg-btn[data-method="' + m + '"]');
      if (btn) btn.click();
      show('onboarding');
      return;
    }
    // ?auto=wifi triggers the scan automatically — used by the
    // screenshot harness to capture the "found servers" state.
    if (qs.get('auto') === 'wifi') {
      show('onboarding');
      setTimeout(() => wifiScanBtn.click(), 200);
      return;
    }

    if (Store.token && Store.user) {
      connectSocket();
      show('channels');
      loadChannels();
    } else if (location.pathname.indexOf('/mobile/') === 0) {
      // Page came from Helen itself — the server URL is already known
      // (location.origin). Skip onboarding; jump straight to auth.
      if (!localStorage.getItem('helen.serverUrl')) {
        Store.serverUrl = location.origin;
      }
      show('auth', { onEnter: enterAuth });
    } else {
      show('onboarding');
    }
  }
  boot();
})();
