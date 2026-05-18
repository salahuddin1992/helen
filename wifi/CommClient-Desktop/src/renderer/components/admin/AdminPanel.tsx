/**
 * AdminPanel — operator console for users with role === 'admin'.
 *
 * Closes the gap where the desktop client only exposed a server-name editor.
 * Mirrors the surface the browser admin (admin/index.html) and iOS-Admin
 * web simulator already cover, but native to the Electron renderer so an
 * admin doesn't need a second tool to operate the server.
 *
 * Tabs (12):
 *   Dashboard · Users · Connected · ActiveCalls · Audit · DLQ ·
 *   Backups · Federation · Peers · Connectivity · ServerConfig · Diagnostics
 */
import React, { useEffect, useMemo, useState, useCallback } from 'react';
import {
  LayoutDashboard, Users as UsersIcon, Wifi, Phone, ScrollText, AlertTriangle,
  HardDrive, GitBranch, Network, Activity, Settings as SettingsIcon, Stethoscope,
  RefreshCw, Trash2, Play, Ban, KeyRound, ShieldCheck, ShieldOff, ShieldAlert,
  UserMinus, UserPlus, Crown, Download,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { api } from '@/services/api.client';
import { useAuthStore } from '@/stores/auth.store';
import { t as i18n } from '@/i18n';
// ── New advanced admin panels (2026.05) ────────────────────────────────
// 11 enterprise modules served by Helen-Server under /admin/modules/*.html.
// We surface each as an embedded iframe panel. See
// components/admin/panels/AdminPanelRegistry.ts for the source of truth.
import {
  ADMIN_PANELS,
  CATEGORY_ORDER,
  CATEGORY_LABELS,
  type AdminPanelSlug,
  type AdminPanelMeta,
} from './panels/AdminPanelRegistry';
import EmbeddedAdminPanel from './panels/EmbeddedAdminPanel';
import AdminNotifications from './AdminNotifications';
import { useAdminStore } from '@/stores/adminStore';

// Legacy native tabs (existing) PLUS new embedded panels (each slug becomes
// `panel:<slug>` so the union stays exhaustive without collision).
type LegacyTabId =
  | 'dashboard' | 'users' | 'connected' | 'calls' | 'audit' | 'dlq'
  | 'backups' | 'federation' | 'peers' | 'connectivity' | 'config' | 'diagnostics'
  | 'crashes' | 'auditchain' | 'transports';

type EmbeddedTabId = `panel:${AdminPanelSlug}`;

type TabId = LegacyTabId | EmbeddedTabId;

const isEmbeddedTab = (id: TabId): id is EmbeddedTabId =>
  typeof id === 'string' && id.startsWith('panel:');

const slugFromTab = (id: EmbeddedTabId): AdminPanelSlug =>
  id.slice('panel:'.length) as AdminPanelSlug;

// Build TABS lazily so the i18n strings are evaluated at render time —
// otherwise switching language wouldn't relabel the sidebar until a
// full reload. (The translation function is called per render of the
// AdminPanel component below.)
function getTabs(): Array<{ id: TabId; label: string; icon: LucideIcon }> {
  return [
    { id: 'dashboard',    label: i18n('admin.dashboard'),    icon: LayoutDashboard },
    { id: 'users',        label: i18n('admin.users'),        icon: UsersIcon },
    { id: 'connected',    label: i18n('admin.connected'),    icon: Wifi },
    { id: 'calls',        label: i18n('admin.calls'),        icon: Phone },
    { id: 'audit',        label: i18n('admin.audit'),        icon: ScrollText },
    { id: 'dlq',          label: i18n('admin.dlq'),          icon: AlertTriangle },
    { id: 'backups',      label: i18n('admin.backups'),      icon: HardDrive },
    { id: 'federation',   label: i18n('admin.federation'),   icon: GitBranch },
    { id: 'peers',        label: i18n('admin.peers'),        icon: Network },
    { id: 'connectivity', label: i18n('admin.connectivity'), icon: Activity },
    { id: 'config',       label: i18n('admin.config'),       icon: SettingsIcon },
    { id: 'diagnostics',  label: i18n('admin.diagnostics'),  icon: Stethoscope },
    { id: 'crashes',      label: 'Crashes',                  icon: AlertTriangle },
    { id: 'auditchain',   label: 'Audit chain',              icon: ScrollText },
    { id: 'transports',   label: 'Transport backends',       icon: Network },
  ];
}

const useAutoRefresh = (fn: () => void, intervalMs: number, deps: any[] = []) => {
  useEffect(() => {
    fn();
    const id = setInterval(fn, intervalMs);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
};

const fmtBytes = (n?: number) => {
  if (n == null) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
  return `${(n / 1073741824).toFixed(2)} GB`;
};

const fmtPct = (n?: number) => (n == null ? '—' : `${n.toFixed(1)}%`);

const fmtRel = (iso?: string | null) => {
  if (!iso) return '—';
  try {
    const ms = Date.now() - new Date(iso.endsWith('Z') ? iso : iso + 'Z').getTime();
    const s = Math.max(0, Math.floor(ms / 1000));
    if (s < 60) return `قبل ${s} ث`;
    if (s < 3600) return `قبل ${Math.floor(s / 60)} د`;
    if (s < 86400) return `قبل ${Math.floor(s / 3600)} س`;
    return `قبل ${Math.floor(s / 86400)} يوم`;
  } catch {
    return iso;
  }
};

// ── Toast helper ─────────────────────────────────────────────────
type Toast = { id: number; kind: 'ok' | 'err' | 'info'; text: string };
let _toastSeq = 0;
const useToasts = () => {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((kind: Toast['kind'], text: string) => {
    const id = ++_toastSeq;
    setToasts((prev) => [...prev, { id, kind, text }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 3500);
  }, []);
  const ok = (t: string) => push('ok', t);
  const err = (t: string) => push('err', t);
  const info = (t: string) => push('info', t);
  return { toasts, ok, err, info };
};

const ToastHost: React.FC<{ toasts: Toast[] }> = ({ toasts }) => (
  <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2">
    {toasts.map((t) => (
      <div
        key={t.id}
        className={`px-3 py-2 rounded-md text-xs shadow-lg max-w-sm ${
          t.kind === 'ok' ? 'bg-green-700 text-green-50' :
          t.kind === 'err' ? 'bg-red-700 text-red-50' :
          'bg-zinc-800 text-zinc-100'
        }`}
      >
        {t.text}
      </div>
    ))}
  </div>
);

// ── Section primitives ──────────────────────────────────────────
const Card: React.FC<{ title?: string; right?: React.ReactNode; children: React.ReactNode }> = ({ title, right, children }) => (
  <div className="bg-surface-800 border border-surface-700 rounded-lg overflow-hidden">
    {(title || right) && (
      <div className="px-4 py-3 border-b border-surface-700 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-white">{title}</h3>
        {right}
      </div>
    )}
    <div className="p-4">{children}</div>
  </div>
);

const KPI: React.FC<{ label: string; value: React.ReactNode; sub?: string }> = ({ label, value, sub }) => (
  <div className="bg-surface-800 border border-surface-700 rounded-lg p-4">
    <div className="text-xs text-gray-400 mb-1">{label}</div>
    <div className="text-2xl font-bold text-white">{value}</div>
    {sub && <div className="text-xs text-gray-500 mt-1">{sub}</div>}
  </div>
);

const Btn: React.FC<{
  onClick: () => void; children: React.ReactNode; tone?: 'default' | 'danger' | 'warn' | 'success' | 'primary';
  disabled?: boolean; size?: 'sm' | 'md'; title?: string;
}> = ({ onClick, children, tone = 'default', disabled, size = 'sm', title }) => {
  const tones: Record<string, string> = {
    default: 'bg-surface-700 hover:bg-surface-600 text-zinc-100',
    danger:  'bg-red-700 hover:bg-red-600 text-red-50',
    warn:    'bg-yellow-700 hover:bg-yellow-600 text-yellow-50',
    success: 'bg-green-700 hover:bg-green-600 text-green-50',
    primary: 'bg-blue-700 hover:bg-blue-600 text-blue-50',
  };
  const sizes = { sm: 'px-2 py-1 text-xs', md: 'px-3 py-1.5 text-sm' };
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center gap-1 ${tones[tone]} ${sizes[size]}`}
    >
      {children}
    </button>
  );
};

// ── Dashboard panel ─────────────────────────────────────────────
const DashboardPanel: React.FC = () => {
  const [stats, setStats] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  useAutoRefresh(async () => {
    try { setStats(await api.admin.stats()); setErr(null); }
    catch (e: any) { setErr(e?.message || 'فشل التحميل'); }
  }, 5000, []);

  if (err) return <div className="text-red-400 text-sm">{err}</div>;
  if (!stats) return <div className="text-gray-400 text-sm">جارِ التحميل...</div>;

  const sys = stats.system || {};
  const counts = stats.counts || {};

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <KPI label="إجمالي المستخدمين" value={counts.total_users ?? '—'} sub={`نشط: ${counts.active_users ?? 0}`} />
        <KPI label="المتصلون الآن" value={counts.online_users ?? 0} />
        <KPI label="القنوات" value={counts.total_channels ?? 0} />
        <KPI label="الرسائل" value={counts.total_messages ?? 0} />
      </div>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <KPI label="CPU" value={fmtPct(sys.cpu_percent)} />
        <KPI label="الذاكرة" value={fmtPct(sys.memory_percent)} sub={fmtBytes(sys.memory_used)} />
        <KPI label="حجم القاعدة" value={fmtBytes(sys.db_size_bytes)} />
        <KPI label="مدة التشغيل" value={`${Math.floor((sys.uptime_seconds ?? 0) / 60)} د`} />
      </div>
      <Card title="معلومات النظام">
        <pre className="text-xs text-gray-300 overflow-auto whitespace-pre-wrap">{JSON.stringify(sys, null, 2)}</pre>
      </Card>
    </div>
  );
};

// ── Users panel ─────────────────────────────────────────────────
const UsersPanel: React.FC = () => {
  const [users, setUsers] = useState<any[]>([]);
  const [search, setSearch] = useState('');
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<any | null>(null);
  const [sessions, setSessions] = useState<any[]>([]);
  const t = useToasts();
  const me = useAuthStore((s) => s.user);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const res = await api.admin.listUsers({ search: search || undefined, limit: 100 });
      setUsers(Array.isArray(res) ? res : (res.users || res.items || []));
    } catch (e: any) { t.err(e?.message || 'فشل التحميل'); }
    finally { setBusy(false); }
  }, [search]);

  useEffect(() => { load(); }, [load]);

  const loadSessions = async (userId: string) => {
    try {
      const res = await api.admin.getUserSessions(userId);
      setSessions(res.sessions || res || []);
    } catch (e: any) { t.err(e?.message || 'فشل تحميل الجلسات'); }
  };

  const action = async (label: string, fn: () => Promise<any>) => {
    try { await fn(); t.ok(`تم: ${label}`); await load(); }
    catch (e: any) { t.err(`${label} فشل: ${e?.message || ''}`); }
  };

  return (
    <div className="space-y-3">
      <ToastHost toasts={t.toasts} />
      <div className="flex gap-2">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') load(); }}
          placeholder="بحث بالاسم..."
          className="flex-1 bg-surface-800 border border-surface-700 rounded px-3 py-1.5 text-sm text-white"
        />
        <Btn onClick={load} disabled={busy} tone="primary" size="md"><RefreshCw size={14} /> تحديث</Btn>
      </div>
      <Card>
        <div className="overflow-auto max-h-[60vh]">
          <table className="w-full text-xs text-zinc-200">
            <thead className="text-gray-400 bg-surface-900 sticky top-0">
              <tr>
                <th className="text-right py-2 px-2">الاسم</th>
                <th className="text-right py-2 px-2">الدور</th>
                <th className="text-right py-2 px-2">الحالة</th>
                <th className="text-right py-2 px-2">آخر ظهور</th>
                <th className="text-right py-2 px-2">إجراءات</th>
              </tr>
            </thead>
            <tbody>
              {users.length === 0 && (
                <tr><td colSpan={5} className="text-center py-4 text-gray-500">لا يوجد مستخدمون</td></tr>
              )}
              {users.map((u) => (
                <tr key={u.id} className="border-t border-surface-800 hover:bg-surface-900">
                  <td className="py-2 px-2">
                    <div className="font-medium">{u.display_name || u.username}</div>
                    <div className="text-gray-500 text-[10px]">@{u.username}</div>
                  </td>
                  <td className="py-2 px-2">
                    <span className={`px-1.5 py-0.5 rounded text-[10px] ${
                      u.role === 'admin' ? 'bg-purple-700 text-purple-100' :
                      u.role === 'moderator' ? 'bg-blue-700 text-blue-100' :
                      'bg-surface-700 text-zinc-300'
                    }`}>{u.role || 'user'}</span>
                  </td>
                  <td className="py-2 px-2">{u.status || (u.is_active === false ? 'محظور' : '—')}</td>
                  <td className="py-2 px-2 text-gray-500">{fmtRel(u.last_seen)}</td>
                  <td className="py-2 px-2">
                    <div className="flex flex-wrap gap-1">
                      <Btn onClick={() => action('طرد', () => api.admin.kick(u.id))} tone="warn" title="طرد">
                        <UserMinus size={12} />
                      </Btn>
                      {u.is_active === false ? (
                        <Btn onClick={() => action('فك حظر', () => api.admin.unban(u.id))} tone="success">
                          <UserPlus size={12} />
                        </Btn>
                      ) : (
                        <Btn onClick={() => {
                          if (u.id === me?.id) { t.err('لا يمكنك حظر نفسك'); return; }
                          if (window.confirm(`حظر ${u.username}؟`))
                            action('حظر', () => api.admin.ban(u.id));
                        }} tone="danger">
                          <Ban size={12} />
                        </Btn>
                      )}
                      <Btn onClick={() => {
                        const next = window.prompt(`تغيير الدور (admin / moderator / user) لـ ${u.username}:`, u.role || 'user');
                        if (next && next.trim()) action('تغيير الدور', () => api.admin.setRole(u.id, next.trim()));
                      }} tone="primary">
                        <Crown size={12} />
                      </Btn>
                      <Btn onClick={() => {
                        const pw = window.prompt(`كلمة سر جديدة لـ ${u.username} (8+ حرفاً):`);
                        if (pw && pw.length >= 8) action('إعادة تعيين كلمة السر', () => api.admin.resetPassword(u.id, pw));
                        else if (pw) t.err('8 أحرف على الأقل');
                      }} tone="default">
                        <KeyRound size={12} />
                      </Btn>
                      <Btn onClick={() => { setSelected(u); loadSessions(u.id); }} tone="default" title="الجلسات">
                        جلسات
                      </Btn>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      {selected && (
        <Card title={`جلسات ${selected.display_name}`} right={<Btn onClick={() => setSelected(null)}>إغلاق</Btn>}>
          <div className="flex justify-end mb-2">
            <Btn onClick={() => action('إبطال كل الجلسات', () =>
              api.admin.revokeAllUserSessions(selected.id).then(() => loadSessions(selected.id))
            )} tone="danger">إبطال كل الجلسات</Btn>
          </div>
          <div className="space-y-1 max-h-64 overflow-auto">
            {sessions.length === 0 && <div className="text-gray-500 text-xs">لا توجد جلسات.</div>}
            {sessions.map((s: any) => (
              <div key={s.id || s.session_id} className="flex justify-between items-center bg-surface-900 rounded px-2 py-1.5 text-xs">
                <div>
                  <div className="text-zinc-200">{s.device_name || s.user_agent?.slice(0, 40) || s.id}</div>
                  <div className="text-gray-500 text-[10px]">{fmtRel(s.created_at || s.started_at)}</div>
                </div>
                <Btn onClick={() => action('إبطال', () =>
                  api.admin.revokeUserSession(selected.id, s.id || s.session_id).then(() => loadSessions(selected.id))
                )} tone="warn">إبطال</Btn>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
};

// ── ConnectedClients panel ──────────────────────────────────────
const ConnectedPanel: React.FC = () => {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  useAutoRefresh(async () => {
    try { setData(await api.admin.connectedClients()); setErr(null); }
    catch (e: any) { setErr(e?.message); }
  }, 4000, []);

  if (err) return <div className="text-red-400 text-sm">{err}</div>;
  if (!data) return <div className="text-gray-400 text-sm">جارِ التحميل...</div>;

  const clients = data.clients || data.connected || data || [];
  const list = Array.isArray(clients) ? clients : (clients.clients || []);

  return (
    <Card title={`العملاء المتصلون (${list.length})`}>
      <div className="overflow-auto max-h-[60vh]">
        <table className="w-full text-xs text-zinc-200">
          <thead className="text-gray-400 bg-surface-900 sticky top-0">
            <tr>
              <th className="text-right py-2 px-2">المستخدم</th>
              <th className="text-right py-2 px-2">الجهاز</th>
              <th className="text-right py-2 px-2">IP</th>
              <th className="text-right py-2 px-2">SID</th>
              <th className="text-right py-2 px-2">منذ</th>
            </tr>
          </thead>
          <tbody>
            {list.length === 0 && <tr><td colSpan={5} className="text-center py-4 text-gray-500">لا يوجد متصلون</td></tr>}
            {list.map((c: any, i: number) => (
              <tr key={c.sid || i} className="border-t border-surface-800">
                <td className="py-2 px-2">{c.username || c.user_id?.slice(0, 8)}</td>
                <td className="py-2 px-2">{c.device_type || c.device_name || '—'}</td>
                <td className="py-2 px-2 font-mono text-[10px]">{c.remote_addr || c.ip || '—'}</td>
                <td className="py-2 px-2 font-mono text-[10px]">{(c.sid || '').slice(0, 16)}…</td>
                <td className="py-2 px-2 text-gray-500">{fmtRel(c.connected_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
};

// ── ActiveCalls panel ───────────────────────────────────────────
const CallsPanel: React.FC = () => {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  useAutoRefresh(async () => {
    try { setData(await api.admin.activeCalls()); setErr(null); }
    catch (e: any) { setErr(e?.message); }
  }, 5000, []);

  if (err) return <div className="text-red-400 text-sm">{err}</div>;
  if (!data) return <div className="text-gray-400 text-sm">جارِ التحميل...</div>;
  const calls = data.calls || [];

  return (
    <Card title={`المكالمات النشطة (${calls.length})`}>
      {calls.length === 0 && <div className="text-gray-500 text-sm py-4 text-center">لا توجد مكالمات نشطة</div>}
      <div className="space-y-2">
        {calls.map((c: any) => (
          <div key={c.call_id} className="bg-surface-900 rounded p-3 text-xs">
            <div className="flex justify-between mb-1">
              <span className="font-mono">{c.call_id?.slice(0, 12)}…</span>
              <span className={`px-1.5 rounded text-[10px] ${c.routing === 'mesh' ? 'bg-blue-700' : c.routing === 'sfu' ? 'bg-purple-700' : 'bg-surface-700'}`}>
                {c.routing} · {c.call_type}
              </span>
            </div>
            <div className="text-gray-400">المشاركون: {c.participant_count ?? c.participants?.length ?? 0}</div>
            <div className="text-gray-500">منذ {fmtRel(c.started_at)}</div>
          </div>
        ))}
      </div>
    </Card>
  );
};

// ── Audit panel ─────────────────────────────────────────────────
const AuditPanel: React.FC = () => {
  const [logs, setLogs] = useState<any[]>([]);
  const [filter, setFilter] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      const res = await api.admin.auditLogs({ limit: 100, event: filter || undefined });
      setLogs(res.logs || res.entries || res || []);
      setErr(null);
    } catch (e: any) { setErr(e?.message); }
  }, [filter]);
  useEffect(() => { load(); const id = setInterval(load, 8000); return () => clearInterval(id); }, [load]);
  return (
    <div className="space-y-3">
      <div className="flex gap-2">
        <input value={filter} onChange={(e) => setFilter(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && load()}
          placeholder="فلتر بالحدث..."
          className="flex-1 bg-surface-800 border border-surface-700 rounded px-3 py-1.5 text-sm text-white" />
        <Btn onClick={load} tone="primary" size="md"><RefreshCw size={14} /> تحديث</Btn>
      </div>
      {err && <div className="text-red-400 text-sm">{err}</div>}
      <Card>
        <div className="space-y-1 max-h-[60vh] overflow-auto text-xs">
          {logs.length === 0 && <div className="text-gray-500 text-center py-4">لا توجد إدخالات</div>}
          {logs.map((l: any, i: number) => (
            <div key={l.id || i} className="border-b border-surface-800 py-1.5 flex justify-between gap-2">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className={`w-2 h-2 rounded-full ${l.success ? 'bg-green-500' : 'bg-red-500'}`} />
                  <span className="font-mono text-zinc-200">{l.event || l.action}</span>
                </div>
                <div className="text-gray-500 text-[10px] truncate">
                  {l.user_id?.slice(0, 8) || 'system'} · {l.ip_address || '—'} · {l.details && JSON.stringify(l.details).slice(0, 80)}
                </div>
              </div>
              <div className="text-gray-500 text-[10px] shrink-0">{fmtRel(l.timestamp || l.created_at)}</div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
};

// ── DLQ panel ───────────────────────────────────────────────────
const DLQPanel: React.FC = () => {
  const [stats, setStats] = useState<any>(null);
  const [list, setList] = useState<any[]>([]);
  const t = useToasts();
  const load = async () => {
    try {
      setStats(await api.admin.dlqStats());
      const l = await api.admin.dlqList({ limit: 50 });
      setList(l.entries || l.items || l || []);
    } catch (e: any) { t.err(e?.message); }
  };
  useEffect(() => { load(); const id = setInterval(load, 8000); return () => clearInterval(id); }, []);
  const replay = async (id: string) => {
    try { await api.admin.dlqReplay(id); t.ok('تمت إعادة المحاولة'); await load(); }
    catch (e: any) { t.err(e?.message); }
  };
  return (
    <div className="space-y-3">
      <ToastHost toasts={t.toasts} />
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <KPI label="معلّق" value={stats?.pending ?? 0} />
        <KPI label="أعيدت" value={stats?.replayed ?? 0} />
        <KPI label="مهملة" value={stats?.abandoned ?? 0} />
        <KPI label="الإجمالي" value={stats?.total ?? 0} />
      </div>
      <Card title="الإدخالات الأخيرة">
        <div className="space-y-1 max-h-[50vh] overflow-auto text-xs">
          {list.length === 0 && <div className="text-gray-500 text-center py-4">لا يوجد</div>}
          {list.map((e: any) => (
            <div key={e.id} className="flex justify-between items-center bg-surface-900 rounded px-2 py-1.5">
              <div className="min-w-0 flex-1">
                <div className="font-mono text-zinc-200">{e.kind} · {e.id?.slice(0, 12)}</div>
                <div className="text-gray-500 text-[10px] truncate">{e.last_error || e.reason || '—'}</div>
              </div>
              <div className="flex gap-1 shrink-0">
                <span className="text-gray-500 text-[10px]">{e.attempt_count}/{e.max_attempts}</span>
                <Btn onClick={() => replay(e.id)} tone="primary"><Play size={11} /></Btn>
              </div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
};

// ── Backups panel ───────────────────────────────────────────────
const BackupsPanel: React.FC = () => {
  const [list, setList] = useState<any[]>([]);
  const [busy, setBusy] = useState(false);
  const t = useToasts();
  const load = async () => {
    try {
      const res = await api.admin.backupsList();
      setList(res.backups || res || []);
    } catch (e: any) { t.err(e?.message); }
  };
  useEffect(() => { load(); }, []);
  const action = async (label: string, fn: () => Promise<any>) => {
    setBusy(true);
    try { await fn(); t.ok(label); await load(); }
    catch (e: any) { t.err(`${label}: ${e?.message || ''}`); }
    finally { setBusy(false); }
  };
  return (
    <div className="space-y-3">
      <ToastHost toasts={t.toasts} />
      <div className="flex gap-2">
        <Btn onClick={() => action('تم إنشاء نسخة', () => api.admin.backupRunNow())}
             tone="primary" size="md" disabled={busy}>
          <Play size={14} /> إنشاء نسخة الآن
        </Btn>
        <Btn onClick={load} size="md" disabled={busy}><RefreshCw size={14} /> تحديث</Btn>
      </div>
      <Card title={`النسخ الاحتياطية (${list.length})`}>
        <div className="space-y-1 max-h-[55vh] overflow-auto text-xs">
          {list.length === 0 && <div className="text-gray-500 text-center py-4">لا توجد نسخ</div>}
          {list.map((b: any) => (
            <div key={b.name} className="flex justify-between items-center bg-surface-900 rounded px-3 py-2">
              <div>
                <div className="font-mono text-zinc-200">{b.name}</div>
                <div className="text-gray-500 text-[10px]">{fmtBytes(b.size_bytes)} · {fmtRel(b.created_at)}</div>
              </div>
              <div className="flex gap-1">
                <a href={api.admin.backupDownloadUrl(b.name)} target="_blank" rel="noreferrer"
                   className="px-2 py-1 text-xs rounded bg-surface-700 hover:bg-surface-600 text-zinc-100 inline-flex items-center gap-1">
                  <Download size={11} />
                </a>
                <Btn onClick={() => action('تحقق', () => api.admin.backupVerify(b.name))} tone="default"><ShieldCheck size={11} /></Btn>
                <Btn onClick={() => {
                  if (window.confirm(`استعادة ${b.name}؟ السيرفر سيعيد التشغيل.`))
                    action('استعادة', () => api.admin.backupRestore(b.name));
                }} tone="warn">استعادة</Btn>
                <Btn onClick={() => {
                  if (window.confirm(`حذف ${b.name}؟`))
                    action('حذف', () => api.admin.backupDelete(b.name));
                }} tone="danger"><Trash2 size={11} /></Btn>
              </div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
};

// ── Federation panel ────────────────────────────────────────────
const FederationPanel: React.FC = () => {
  const [status, setStatus] = useState<any>(null);
  const [metrics, setMetrics] = useState<any>(null);
  const [events, setEvents] = useState<any[]>([]);
  const [bridges, setBridges] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  useAutoRefresh(async () => {
    try {
      const [s, m, e, b] = await Promise.allSettled([
        api.admin.federationStatus(),
        api.admin.federationMetrics(),
        api.admin.federationEvents(20),
        api.admin.federationBridges(),
      ]);
      if (s.status === 'fulfilled') setStatus(s.value);
      if (m.status === 'fulfilled') setMetrics(m.value);
      if (e.status === 'fulfilled') setEvents((e.value as any).events || (e.value as any) || []);
      if (b.status === 'fulfilled') setBridges(b.value);
      setErr(null);
    } catch (e: any) { setErr(e?.message); }
  }, 6000, []);
  return (
    <div className="space-y-3">
      {err && <div className="text-red-400 text-sm">{err}</div>}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <KPI label="مفعّلة" value={status?.enabled ? 'نعم' : 'لا'} />
        <KPI label="عدد الأقران" value={status?.peer_count ?? bridges?.peers?.length ?? 0} />
        <KPI label="رسائل أُرسِلت" value={metrics?.events_sent ?? 0} />
        <KPI label="رسائل اُستُلمت" value={metrics?.events_received ?? 0} />
      </div>
      <Card title="الجسور (Bridges)">
        <pre className="text-xs text-gray-300 overflow-auto whitespace-pre-wrap max-h-48">{JSON.stringify(bridges, null, 2)}</pre>
      </Card>
      <Card title="آخر الأحداث">
        <div className="max-h-64 overflow-auto text-xs space-y-1">
          {events.length === 0 && <div className="text-gray-500 py-2 text-center">لا توجد أحداث</div>}
          {events.map((e: any, i: number) => (
            <div key={i} className="bg-surface-900 rounded px-2 py-1 flex justify-between">
              <span className="font-mono text-zinc-200">{e.event || e.type || '—'}</span>
              <span className="text-gray-500 text-[10px]">{fmtRel(e.timestamp || e.ts)}</span>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
};

// ── Peers panel ─────────────────────────────────────────────────
const PeersPanel: React.FC = () => {
  type Bucket = 'discovered' | 'pending' | 'approved' | 'rejected' | 'denied';
  const [bucket, setBucket] = useState<Bucket>('pending');
  const [peers, setPeers] = useState<any[]>([]);
  const t = useToasts();

  const load = useCallback(async () => {
    try {
      const fn = api.adminPeers[bucket];
      const res = await fn();
      setPeers(res.peers || []);
    } catch (e: any) { t.err(e?.message); }
  }, [bucket]);

  useEffect(() => { load(); const id = setInterval(load, 6000); return () => clearInterval(id); }, [load]);

  const action = async (label: string, fn: () => Promise<any>) => {
    try { await fn(); t.ok(label); await load(); }
    catch (e: any) { t.err(`${label}: ${e?.message || ''}`); }
  };

  return (
    <div className="space-y-3">
      <ToastHost toasts={t.toasts} />
      <div className="flex flex-wrap gap-1">
        {(['discovered', 'pending', 'approved', 'rejected', 'denied'] as Bucket[]).map((b) => (
          <button key={b} onClick={() => setBucket(b)}
            className={`px-3 py-1.5 text-xs rounded ${bucket === b ? 'bg-blue-700 text-white' : 'bg-surface-800 text-gray-300 hover:bg-surface-700'}`}>
            {b}
          </button>
        ))}
        <div className="flex-1" />
        <Btn onClick={load} size="md"><RefreshCw size={14} /></Btn>
      </div>
      <Card title={`الأقران: ${bucket} (${peers.length})`}>
        <div className="space-y-1 max-h-[55vh] overflow-auto text-xs">
          {peers.length === 0 && <div className="text-gray-500 text-center py-4">لا يوجد</div>}
          {peers.map((p: any) => (
            <div key={p.server_id} className="bg-surface-900 rounded px-3 py-2">
              <div className="flex justify-between items-start mb-1">
                <div>
                  <div className="font-mono text-zinc-200">{p.server_id?.slice(0, 16)}…</div>
                  <div className="text-gray-500 text-[10px]">{p.endpoint || '—'} · {p.region || '—'}</div>
                </div>
                <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                  p.approval_status === 'APPROVED' ? 'bg-green-700' :
                  p.approval_status === 'REJECTED_BY_ADMIN' ? 'bg-red-700' :
                  'bg-surface-700'
                }`}>{p.approval_status || p.runtime_status}</span>
              </div>
              <div className="flex flex-wrap gap-1 mt-1">
                {bucket !== 'approved' && (
                  <Btn onClick={() => action('قبول', () => api.adminPeers.approve(p.server_id))} tone="success">
                    <ShieldCheck size={11} /> قبول
                  </Btn>
                )}
                {bucket !== 'rejected' && (
                  <Btn onClick={() => {
                    const r = window.prompt('السبب:');
                    if (r) action('رفض', () => api.adminPeers.reject(p.server_id, r));
                  }} tone="warn">
                    <ShieldOff size={11} /> رفض
                  </Btn>
                )}
                {bucket !== 'denied' && (
                  <Btn onClick={() => {
                    const r = window.prompt('السبب:');
                    if (r) action('حظر', () => api.adminPeers.deny(p.server_id, r));
                  }} tone="danger">
                    <ShieldAlert size={11} /> حظر
                  </Btn>
                )}
                <Btn onClick={() => action('ثقة دائمة', () => api.adminPeers.trustPermanently(p.server_id))} tone="primary">
                  ثقة دائمة
                </Btn>
              </div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
};

// ── Connectivity panel ──────────────────────────────────────────
const ConnectivityPanel: React.FC = () => {
  const [status, setStatus] = useState<any>(null);
  const [tunnel, setTunnel] = useState({ ws_url: '', token: '', display_name: '' });
  const t = useToasts();
  const load = async () => {
    try { setStatus(await api.admin.connectivity()); }
    catch (e: any) { t.err(e?.message); }
  };
  useEffect(() => { load(); const id = setInterval(load, 6000); return () => clearInterval(id); }, []);
  const apply = async () => {
    if (!tunnel.ws_url || !tunnel.token) { t.err('املأ ws_url + token'); return; }
    try { await api.admin.tunnelConfigure(tunnel); t.ok('تم تكوين النفق'); await load(); }
    catch (e: any) { t.err(e?.message); }
  };
  const disable = async () => {
    if (!window.confirm('تعطيل النفق؟')) return;
    try { await api.admin.tunnelDisable(); t.ok('تم التعطيل'); await load(); }
    catch (e: any) { t.err(e?.message); }
  };
  return (
    <div className="space-y-3">
      <ToastHost toasts={t.toasts} />
      <Card title="حالة الاتصال الخارجي">
        <pre className="text-xs text-gray-300 overflow-auto whitespace-pre-wrap max-h-48">{JSON.stringify(status, null, 2)}</pre>
      </Card>
      <Card title="تكوين النفق (Reverse Tunnel)">
        <div className="space-y-2 text-xs">
          <input value={tunnel.ws_url} onChange={(e) => setTunnel({ ...tunnel, ws_url: e.target.value })}
            placeholder="ws://rendezvous.example/tunnel/register"
            className="w-full bg-surface-900 border border-surface-700 rounded px-2 py-1 text-zinc-100" />
          <input type="password" value={tunnel.token} onChange={(e) => setTunnel({ ...tunnel, token: e.target.value })}
            placeholder="token"
            className="w-full bg-surface-900 border border-surface-700 rounded px-2 py-1 text-zinc-100" />
          <input value={tunnel.display_name} onChange={(e) => setTunnel({ ...tunnel, display_name: e.target.value })}
            placeholder="اسم العرض (اختياري)"
            className="w-full bg-surface-900 border border-surface-700 rounded px-2 py-1 text-zinc-100" />
          <div className="flex gap-2">
            <Btn onClick={apply} tone="primary" size="md">تطبيق</Btn>
            <Btn onClick={disable} tone="danger" size="md">تعطيل</Btn>
          </div>
        </div>
      </Card>
    </div>
  );
};

// ── ServerConfig panel ──────────────────────────────────────────
const ConfigPanel: React.FC = () => {
  const [config, setConfig] = useState<any>(null);
  const [name, setName] = useState('');
  const t = useToasts();
  const load = async () => {
    try { const c = await api.admin.serverConfig(); setConfig(c); setName(c.server_name || ''); }
    catch (e: any) { t.err(e?.message); }
  };
  useEffect(() => { load(); }, []);
  const save = async () => {
    try { await api.admin.updateServerConfig({ server_name: name }); t.ok('تم الحفظ'); await load(); }
    catch (e: any) { t.err(e?.message); }
  };
  return (
    <div className="space-y-3">
      <ToastHost toasts={t.toasts} />
      <Card title="اسم الخادم">
        <div className="flex gap-2">
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={64}
            className="flex-1 bg-surface-900 border border-surface-700 rounded px-2 py-1.5 text-sm text-zinc-100" />
          <Btn onClick={save} tone="primary" size="md">حفظ</Btn>
        </div>
      </Card>
      <Card title="ملف التكوين الكامل">
        <pre className="text-xs text-gray-300 overflow-auto whitespace-pre-wrap max-h-96">{JSON.stringify(config, null, 2)}</pre>
      </Card>
    </div>
  );
};

// ── Diagnostics panel ───────────────────────────────────────────
const DiagnosticsPanel: React.FC = () => {
  const [data, setData] = useState<any>(null);
  const [sfu, setSfu] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const t = useToasts();
  const load = async () => {
    try {
      const [diag, sfuStatus] = await Promise.allSettled([
        api.admin.diagnosticsNetwork(),
        api.admin.sfuStatus(),
      ]);
      if (diag.status === 'fulfilled') setData(diag.value);
      if (sfuStatus.status === 'fulfilled') setSfu(sfuStatus.value);
      setErr(null);
    } catch (e: any) { setErr(e?.message); }
  };
  useEffect(() => { load(); const id = setInterval(load, 8000); return () => clearInterval(id); }, []);
  const cleanupSessions = async () => {
    try { const r = await api.admin.cleanupSessions(); t.ok(i18n('admin.diagnostics.cleanup_done').replace('{{n}}', String(r.removed ?? 0))); }
    catch (e: any) { t.err(e?.message); }
  };
  const cleanupFiles = async () => {
    try { const r = await api.admin.cleanupFiles(); t.ok(i18n('admin.diagnostics.cleanup_done').replace('{{n}}', String(r.removed ?? 0))); }
    catch (e: any) { t.err(e?.message); }
  };
  return (
    <div className="space-y-3">
      <ToastHost toasts={t.toasts} />
      {sfu && (
        <Card title="SFU (mediasoup) Status">
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-3">
            <KPI label="Enabled" value={sfu.enabled ? 'Yes' : 'No'} />
            <KPI label="Running" value={sfu.running ? 'Yes' : 'No'} />
            <KPI label="Healthy" value={sfu.healthy ? '✓' : '✗'}
              sub={sfu.last_error ? sfu.last_error.slice(0, 40) : undefined} />
            <KPI label="Restarts" value={sfu.restart_count ?? 0} />
          </div>
          <div className="text-xs text-gray-400">
            <div>Control: <span className="font-mono text-zinc-300">{sfu.control_host}:{sfu.control_port}</span></div>
            <div>Worker: <span className="font-mono text-zinc-300">{sfu.worker_root}</span></div>
            {sfu.pid && <div>PID: <span className="font-mono text-zinc-300">{sfu.pid}</span></div>}
          </div>
        </Card>
      )}
      <Card title={i18n('admin.diagnostics.title')} right={<Btn onClick={load} size="sm"><RefreshCw size={12} /></Btn>}>
        {err && <div className="text-red-400 text-sm mb-2">{err}</div>}
        <pre className="text-xs text-gray-300 overflow-auto whitespace-pre-wrap max-h-96">{JSON.stringify(data, null, 2)}</pre>
      </Card>
      <Card title={i18n('admin.diagnostics.actions')}>
        <div className="flex flex-wrap gap-2">
          <Btn onClick={cleanupSessions} tone="warn" size="md">{i18n('admin.diagnostics.cleanup_sessions')}</Btn>
          <Btn onClick={cleanupFiles} tone="warn" size="md">{i18n('admin.diagnostics.cleanup_files')}</Btn>
        </div>
      </Card>
    </div>
  );
};

// ── Crashes panel ────────────────────────────────────────────────
const CrashesPanel: React.FC = () => {
  const [events, setEvents] = useState<any[]>([]);
  const [installed, setInstalled] = useState<boolean>(true);
  const [level, setLevel] = useState<string>('');
  const [selected, setSelected] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const t = useToasts();
  const load = useCallback(async () => {
    try {
      const r = await api.adminCrashes.list({
        limit: 100, level: level || undefined,
      });
      setEvents(r.events || []);
      setInstalled(r.installed !== false);
      setErr(null);
    } catch (e: any) { setErr(e?.message); }
  }, [level]);
  useEffect(() => { load(); const id = setInterval(load, 15000); return () => clearInterval(id); }, [load]);
  const showDetail = async (id: string) => {
    try { setSelected(await api.adminCrashes.get(id)); }
    catch (e: any) { t.err(e?.message); }
  };
  const purge = async () => {
    const days = window.prompt('Purge crashes older than how many days?', '30');
    if (!days) return;
    const n = parseInt(days, 10);
    if (!Number.isFinite(n) || n < 1) { t.err('invalid number of days'); return; }
    try {
      const r = await api.adminCrashes.purgeOlderThan(n);
      t.ok(`Purged ${r.deleted} events older than ${r.days} days`);
      await load();
    } catch (e: any) { t.err(e?.message); }
  };
  return (
    <div className="space-y-3">
      <ToastHost toasts={t.toasts} />
      {!installed && (
        <Card>
          <div className="text-yellow-400 text-sm">
            Crash reporter is not installed on this server. Restart with the
            latest build to enable it.
          </div>
        </Card>
      )}
      <div className="flex gap-2">
        <select
          value={level}
          onChange={(e) => setLevel(e.target.value)}
          className="bg-surface-800 border border-surface-700 rounded px-2 py-1 text-sm text-white"
        >
          <option value="">All levels</option>
          <option value="crash">crash</option>
          <option value="error">error</option>
          <option value="warning">warning</option>
          <option value="info">info</option>
        </select>
        <Btn onClick={load} tone="primary" size="md"><RefreshCw size={14} /> Refresh</Btn>
        <Btn onClick={purge} tone="warn" size="md">Purge old</Btn>
      </div>
      {err && <div className="text-red-400 text-sm">{err}</div>}
      <Card>
        <div className="space-y-1 max-h-[60vh] overflow-auto text-xs">
          {events.length === 0 && <div className="text-gray-500 text-center py-4">No crash events</div>}
          {events.map((e: any) => (
            <div
              key={e.event_id}
              onClick={() => showDetail(e.event_id)}
              className="border-b border-surface-800 py-1.5 px-2 cursor-pointer hover:bg-surface-800"
            >
              <div className="flex items-center gap-2">
                <span className={`w-2 h-2 rounded-full ${
                  e.level === 'crash' ? 'bg-red-500'
                  : e.level === 'error' ? 'bg-orange-500'
                  : e.level === 'warning' ? 'bg-yellow-500'
                  : 'bg-blue-500'
                }`} />
                <span className="font-mono text-zinc-200">{e.type}</span>
                <span className="text-gray-500 truncate flex-1">{e.message}</span>
                <span className="text-gray-500 text-[10px] shrink-0">
                  {fmtRel(new Date(e.timestamp * 1000).toISOString())}
                </span>
              </div>
            </div>
          ))}
        </div>
      </Card>
      {selected && (
        <Card title={`Crash ${selected.event_id}`}
              right={<Btn onClick={() => setSelected(null)} size="sm">Close</Btn>}>
          <div className="text-xs space-y-2">
            <div><b>Type:</b> {selected.type} <b className="ml-3">Level:</b> {selected.level}</div>
            <div><b>Host:</b> {selected.hostname} <b className="ml-3">PID:</b> {selected.pid} <b className="ml-3">OS:</b> {selected.os}</div>
            <div><b>When:</b> {new Date(selected.timestamp * 1000).toLocaleString()}</div>
            <div><b>Message:</b> {selected.message}</div>
            {selected.stack_trace && (
              <pre className="bg-surface-900 p-2 rounded overflow-auto max-h-72 whitespace-pre-wrap">
                {selected.stack_trace}
              </pre>
            )}
            {selected.breadcrumbs?.length > 0 && (
              <div>
                <b>Breadcrumbs:</b>
                <ul className="ml-4 list-disc text-gray-400">
                  {selected.breadcrumbs.map((b: any, i: number) => (
                    <li key={i}>
                      [{b.category}] {b.message}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </Card>
      )}
    </div>
  );
};


// ── Audit chain panel ────────────────────────────────────────────
const AuditChainPanel: React.FC = () => {
  const [head, setHead] = useState<any>(null);
  const [entries, setEntries] = useState<any[]>([]);
  const [actorFilter, setActorFilter] = useState('');
  const [actionFilter, setActionFilter] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const t = useToasts();
  const load = useCallback(async () => {
    try {
      const [h, en] = await Promise.allSettled([
        api.adminAuditChain.head(),
        api.adminAuditChain.entries({
          actor: actorFilter || undefined,
          action: actionFilter || undefined,
          limit: 100,
        }),
      ]);
      if (h.status === 'fulfilled') setHead(h.value);
      if (en.status === 'fulfilled') setEntries(en.value.entries || []);
      setErr(null);
    } catch (e: any) { setErr(e?.message); }
  }, [actorFilter, actionFilter]);
  useEffect(() => { load(); const id = setInterval(load, 15000); return () => clearInterval(id); }, [load]);
  const verify = async () => {
    try {
      const r = await api.adminAuditChain.verify();
      if (r.ok) t.ok('Chain integrity verified ✓');
      else t.err(`Tamper detected at seq ${r.broken_at_seq}: ${r.message}`);
    } catch (e: any) { t.err(e?.message); }
  };
  return (
    <div className="space-y-3">
      <ToastHost toasts={t.toasts} />
      <Card title="Chain head" right={<Btn onClick={verify} tone="primary" size="sm">Verify integrity</Btn>}>
        {!head?.configured && (
          <div className="text-yellow-400 text-sm">
            Audit chain not configured on this server.
          </div>
        )}
        {head?.empty && <div className="text-gray-400 text-sm">Chain is empty.</div>}
        {head?.head && (
          <div className="text-xs space-y-1 font-mono">
            <div><b>Seq:</b> {head.head.seq}</div>
            <div><b>When:</b> {new Date(head.head.timestamp * 1000).toLocaleString()}</div>
            <div><b>Actor:</b> {head.head.actor}</div>
            <div><b>Action:</b> {head.head.action}</div>
            <div><b>Target:</b> {head.head.target || '—'}</div>
            <div><b>Hash:</b> <span className="text-gray-400">{head.head.chain_hash}</span></div>
          </div>
        )}
      </Card>
      <div className="flex gap-2">
        <input
          value={actorFilter}
          onChange={(e) => setActorFilter(e.target.value)}
          placeholder="filter by actor"
          className="bg-surface-800 border border-surface-700 rounded px-2 py-1 text-sm text-white"
        />
        <input
          value={actionFilter}
          onChange={(e) => setActionFilter(e.target.value)}
          placeholder="filter by action"
          className="bg-surface-800 border border-surface-700 rounded px-2 py-1 text-sm text-white"
        />
        <Btn onClick={load} tone="primary" size="md"><RefreshCw size={14} /> Refresh</Btn>
      </div>
      {err && <div className="text-red-400 text-sm">{err}</div>}
      <Card>
        <div className="space-y-1 max-h-[60vh] overflow-auto text-xs">
          {entries.length === 0 && <div className="text-gray-500 text-center py-4">No entries</div>}
          {entries.map((e: any) => (
            <div key={e.seq} className="border-b border-surface-800 py-1.5">
              <div className="flex items-center gap-2">
                <span className="font-mono text-gray-500">#{e.seq}</span>
                <span className="font-mono text-zinc-200">{e.action}</span>
                <span className="text-gray-500 truncate flex-1">
                  {e.actor} → {e.target || '—'}
                </span>
                <span className="text-gray-500 text-[10px] shrink-0">
                  {fmtRel(new Date(e.timestamp * 1000).toISOString())}
                </span>
              </div>
              <div className="text-[10px] text-gray-600 font-mono ml-6 truncate">
                {e.chain_hash}
              </div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
};


// ── Transports panel ─────────────────────────────────────────────
const TransportsPanel: React.FC = () => {
  const [summary, setSummary] = useState<any>(null);
  const [nats, setNats] = useState<any>(null);
  const [mqtt, setMqtt] = useState<any>(null);
  const [grpc, setGrpc] = useState<any>(null);
  const [wg, setWg] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const t = useToasts();

  const load = useCallback(async () => {
    try {
      const [s, n, m, g, w] = await Promise.allSettled([
        api.adminTransports.summary(),
        api.adminTransports.nats(),
        api.adminTransports.mqtt(),
        api.adminTransports.grpc(),
        api.adminTransports.wireguard(),
      ]);
      if (s.status === 'fulfilled') setSummary(s.value);
      if (n.status === 'fulfilled') setNats(n.value);
      if (m.status === 'fulfilled') setMqtt(m.value);
      if (g.status === 'fulfilled') setGrpc(g.value);
      if (w.status === 'fulfilled') setWg(w.value);
      setErr(null);
    } catch (e: any) { setErr(e?.message); }
  }, []);
  useEffect(() => { load(); const id = setInterval(load, 15000); return () => clearInterval(id); }, [load]);

  const Pill: React.FC<{ active: boolean; label: string }> = ({ active, label }) => (
    <span style={{
      padding: '2px 10px', borderRadius: 12, fontSize: 12,
      background: active ? '#1f6f3f' : '#444',
      color: active ? '#d9f7d9' : '#aaa',
    }}>{label}</span>
  );

  return (
    <div className="space-y-3">
      <ToastHost toasts={t.toasts} />
      {err && <div className="text-red-400 text-sm">{err}</div>}

      <Card title="Backend selection (env-driven)"
            right={<Btn onClick={load} size="sm"><RefreshCw size={12} /></Btn>}>
        {!summary && <div className="text-gray-500 text-sm">loading…</div>}
        {summary && (
          <div className="text-xs space-y-2">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <div className="text-gray-500 text-[10px] uppercase">Broker</div>
                <div className="font-mono text-zinc-200">{summary.broker_backend}</div>
              </div>
              <div>
                <div className="text-gray-500 text-[10px] uppercase">Federation</div>
                <div className="font-mono text-zinc-200">{summary.federation_backend}</div>
              </div>
              <div>
                <div className="text-gray-500 text-[10px] uppercase">VPN</div>
                <div className="font-mono text-zinc-200">{summary.vpn_backend || '<none>'}</div>
              </div>
              <div>
                <div className="text-gray-500 text-[10px] uppercase">Mesh topology</div>
                <div className="font-mono text-zinc-200">{summary.mesh_topology}</div>
              </div>
            </div>
            <div className="flex gap-2 mt-3">
              <Pill active={summary.active?.nats} label="NATS" />
              <Pill active={summary.active?.mqtt} label="MQTT" />
              <Pill active={summary.active?.grpc_federation} label="gRPC" />
              <Pill active={summary.active?.wireguard} label="WireGuard" />
            </div>
          </div>
        )}
      </Card>

      <Card title="NATS adapter">
        {!nats?.configured && <div className="text-gray-500 text-xs">not configured (default Redis backend in use)</div>}
        {nats?.configured && (
          <pre className="text-xs text-gray-300 overflow-auto whitespace-pre-wrap max-h-48">
            {JSON.stringify(nats, null, 2)}
          </pre>
        )}
      </Card>

      <Card title="MQTT adapter">
        {!mqtt?.configured && <div className="text-gray-500 text-xs">not configured</div>}
        {mqtt?.configured && (
          <pre className="text-xs text-gray-300 overflow-auto whitespace-pre-wrap max-h-48">
            {JSON.stringify(mqtt, null, 2)}
          </pre>
        )}
      </Card>

      <Card title="gRPC federation">
        {!grpc?.configured && <div className="text-gray-500 text-xs">not configured (default HMAC-JSON HTTP federation in use)</div>}
        {grpc?.configured && (
          <pre className="text-xs text-gray-300 overflow-auto whitespace-pre-wrap max-h-48">
            {JSON.stringify(grpc, null, 2)}
          </pre>
        )}
      </Card>

      <Card title="WireGuard mesh">
        {!wg?.configured && <div className="text-gray-500 text-xs">not configured (LAN runs cleartext between Helen-Servers — use Helen-Rendezvous TLS for cross-segment)</div>}
        {wg?.configured && (
          <pre className="text-xs text-gray-300 overflow-auto whitespace-pre-wrap max-h-48">
            {JSON.stringify(wg, null, 2)}
          </pre>
        )}
      </Card>
    </div>
  );
};


// ── Main ────────────────────────────────────────────────────────
const AdminPanel: React.FC = () => {
  const [tab, setTab] = useState<TabId>('dashboard');
  const me = useAuthStore((s) => s.user);
  const TABS = useMemo(() => getTabs(), [me?.id, tab]);
  const setStoreSlug = useAdminStore((s) => s.setSelectedSlug);

  // Keep the global admin store in sync with whichever tab is active so
  // notifications can deep-link back into the right panel.
  useEffect(() => {
    if (isEmbeddedTab(tab)) setStoreSlug(slugFromTab(tab));
    else setStoreSlug(null);
  }, [tab, setStoreSlug]);

  // Navigation helper used by the notifications drawer.
  const goToPanel = useCallback((slug: AdminPanelSlug) => {
    setTab(`panel:${slug}` as TabId);
  }, []);

  const Body = useMemo(() => {
    if (isEmbeddedTab(tab)) {
      return <EmbeddedAdminPanel slug={slugFromTab(tab)} />;
    }
    switch (tab as LegacyTabId) {
      case 'dashboard':    return <DashboardPanel />;
      case 'users':        return <UsersPanel />;
      case 'connected':    return <ConnectedPanel />;
      case 'calls':        return <CallsPanel />;
      case 'audit':        return <AuditPanel />;
      case 'dlq':          return <DLQPanel />;
      case 'backups':      return <BackupsPanel />;
      case 'federation':   return <FederationPanel />;
      case 'peers':        return <PeersPanel />;
      case 'connectivity': return <ConnectivityPanel />;
      case 'config':       return <ConfigPanel />;
      case 'diagnostics':  return <DiagnosticsPanel />;
      case 'crashes':      return <CrashesPanel />;
      case 'auditchain':   return <AuditChainPanel />;
      case 'transports':   return <TransportsPanel />;
    }
  }, [tab]);

  // Group the new advanced panels by category for the sidebar section.
  const grouped: Record<string, AdminPanelMeta[]> = useMemo(() => {
    const out: Record<string, AdminPanelMeta[]> = {};
    for (const p of ADMIN_PANELS) {
      (out[p.category] ||= []).push(p);
    }
    return out;
  }, []);

  const currentLabel = useMemo(() => {
    if (isEmbeddedTab(tab)) {
      const meta = ADMIN_PANELS.find((p) => p.slug === slugFromTab(tab));
      return meta?.labelAr || meta?.labelEn || tab;
    }
    return TABS.find((t) => t.id === (tab as LegacyTabId))?.label || tab;
  }, [tab, TABS]);

  // Defense in depth: server enforces role on every endpoint, but we also
  // gate the UI to avoid sending requests we know will 403.
  if (me?.role !== 'admin') {
    return (
      <div className="p-8 text-center">
        <div className="inline-flex items-center gap-2 text-yellow-400 mb-2">
          <ShieldAlert size={20} />
          <span className="font-semibold">{i18n('admin.role_required')}</span>
        </div>
        <p className="text-gray-400 text-sm">{i18n('admin.role_required_hint')}</p>
      </div>
    );
  }

  return (
    <div className="flex h-full">
      <aside className="w-60 bg-surface-950 border-l border-surface-800 overflow-y-auto shrink-0">
        <div className="p-3 border-b border-surface-800 flex items-center gap-2">
          <div className="flex-1 min-w-0">
            <div className="text-xs text-gray-400">{i18n('admin.title')}</div>
            <div className="text-sm font-semibold text-white truncate">{me.display_name || me.username}</div>
          </div>
          <AdminNotifications onNavigateToPanel={goToPanel} />
        </div>
        <nav className="p-2 space-y-0.5">
          {/* Legacy native tabs (keep at top — most-used operator surface). */}
          {TABS.map((t) => {
            const Icon = t.icon;
            const active = tab === t.id;
            return (
              <button key={t.id} onClick={() => setTab(t.id)}
                className={`w-full flex items-center gap-2 px-3 py-2 rounded text-xs text-right transition-colors ${
                  active ? 'bg-blue-600 text-white' : 'text-gray-400 hover:text-white hover:bg-surface-800'
                }`}>
                <Icon size={14} />
                <span className="flex-1 text-right">{t.label}</span>
              </button>
            );
          })}

          {/* New advanced panels, grouped by category. */}
          {CATEGORY_ORDER.map((cat) => {
            const items = grouped[cat];
            if (!items || items.length === 0) return null;
            return (
              <div key={cat} className="pt-3 first:pt-2">
                <div className="px-3 py-1 text-[10px] uppercase tracking-wide text-gray-500 font-semibold">
                  {CATEGORY_LABELS[cat].ar} · {CATEGORY_LABELS[cat].en}
                </div>
                {items.map((p) => {
                  const Icon = p.icon;
                  const tabId = `panel:${p.slug}` as TabId;
                  const active = tab === tabId;
                  return (
                    <button
                      key={p.slug}
                      onClick={() => setTab(tabId)}
                      title={`${p.labelEn} — ${p.requiresPermission}${p.hotkey ? ` · ${p.hotkey}` : ''}`}
                      className={`w-full flex items-center gap-2 px-3 py-2 rounded text-xs text-right transition-colors ${
                        active
                          ? 'bg-blue-600 text-white'
                          : `text-gray-400 hover:text-white hover:bg-surface-800`
                      }`}
                    >
                      <Icon size={14} className={active ? '' : p.colorClass} />
                      <span className="flex-1 text-right truncate">{p.labelAr}</span>
                      {p.hotkey && (
                        <span className="text-[9px] text-gray-600 hidden xl:inline">{p.hotkey.replace('CmdOrCtrl', '⌘')}</span>
                      )}
                    </button>
                  );
                })}
              </div>
            );
          })}
        </nav>
      </aside>
      <main className="flex-1 overflow-auto p-0">
        {/* Embedded panels manage their own header; native ones get the
            legacy h2 + padded body for consistency with the existing UX. */}
        {isEmbeddedTab(tab) ? (
          Body
        ) : (
          <div className="p-4">
            <h2 className="text-lg font-bold text-white mb-4">{currentLabel}</h2>
            {Body}
          </div>
        )}
      </main>
    </div>
  );
};

export default AdminPanel;
