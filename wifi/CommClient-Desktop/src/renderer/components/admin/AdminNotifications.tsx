/**
 * AdminNotifications — bell icon + slide-over drawer that surfaces real-time
 * operator-grade events from the AdminWebSocketManager:
 *   - Critical audit alerts (tamper / chain break / privileged action)
 *   - DR backup failures
 *   - License expiry warnings
 *   - Federation peer quarantines
 *   - QoS critical-quality calls (MOS < 2.5)
 *   - Plugin update available
 *
 * The component is mounted once at the AdminPanel level. It owns the WS
 * subscriptions for its lifetime and forwards parsed events into the
 * Zustand admin store so the bell badge stays in sync with the drawer.
 *
 * Lifetime
 * ────────
 * Mounting subscribes; unmounting unsubscribes. We deliberately do NOT
 * mount this globally at the App root — the desktop client is meant for
 * regular users too, and we shouldn't open admin WS connections for them.
 */

import React, { useEffect, useMemo, useCallback } from 'react';
import { Bell, AlertTriangle, ShieldCheck, HardDrive, GitBranch, Mic, Boxes, CreditCard, X, Check } from 'lucide-react';
import { getAdminWsManager } from '@/services/AdminWebSocketManager';
import type { AdminWsChannel, AdminWsMessage } from '@/services/AdminWebSocketManager';
import { useAdminStore } from '@/stores/adminStore';
import type { NotificationSeverity } from '@/stores/adminStore';
import type { AdminPanelSlug } from './panels/AdminPanelRegistry';

// ── Severity → style helpers ───────────────────────────────────────────
const severityColor: Record<NotificationSeverity, string> = {
  info:     'text-sky-300 bg-sky-900/40',
  success:  'text-emerald-300 bg-emerald-900/40',
  warn:     'text-amber-300 bg-amber-900/40',
  error:    'text-red-300 bg-red-900/50',
  critical: 'text-red-100 bg-red-700/70 ring-1 ring-red-500',
};

const severityIcon: Record<NotificationSeverity, React.ReactNode> = {
  info:     <Bell size={14} />,
  success:  <Check size={14} />,
  warn:     <AlertTriangle size={14} />,
  error:    <AlertTriangle size={14} />,
  critical: <AlertTriangle size={14} />,
};

// ── Channel → panel mapping for click-through ──────────────────────────
const CHANNEL_TO_SLUG: Record<AdminWsChannel, AdminPanelSlug | undefined> = {
  metrics:    'monitoring_unified',
  topology:   'topology_visualizer',
  audit:      'siem_audit',
  dr:         'dr_console',
  plugins:    'plugin_marketplace',
  federation: 'federation_health',
  qos:        'qos_live',
};

const channelIcon: Partial<Record<AdminWsChannel, React.ReactNode>> = {
  audit:      <ShieldCheck size={14} />,
  dr:         <HardDrive size={14} />,
  federation: <GitBranch size={14} />,
  qos:        <Mic size={14} />,
  plugins:    <Boxes size={14} />,
};

// ─────────────────────────────────────────────────────────────────────────
//  Translation layer — map raw WS payloads into AdminNotification entries
// ─────────────────────────────────────────────────────────────────────────

interface IngestResult {
  push: boolean;
  severity: NotificationSeverity;
  title: string;
  body?: string;
}

function ingestAuditEvent(msg: AdminWsMessage<any>): IngestResult {
  const d = msg.data || {};
  if (msg.type === 'audit.tamper') {
    return {
      push: true,
      severity: 'critical',
      title: 'تكامل سلسلة التدقيق مكسور',
      body: `seq ${d.broken_at_seq ?? '?'} — ${d.message ?? 'مطلوب تحقق فوري'}`,
    };
  }
  if (msg.type === 'audit.alert') {
    const sev = (d.severity || 'warn') as NotificationSeverity;
    return { push: true, severity: sev, title: d.title || 'تنبيه أمني', body: d.body };
  }
  return { push: false, severity: 'info', title: '' };
}

function ingestDrEvent(msg: AdminWsMessage<any>): IngestResult {
  const d = msg.data || {};
  if (msg.type === 'dr.backup_failed') {
    return {
      push: true,
      severity: 'error',
      title: 'فشل النسخ الاحتياطي',
      body: `policy=${d.policy_id ?? '?'} dest=${d.destination_id ?? '?'} — ${d.error ?? 'مجهول'}`,
    };
  }
  if (msg.type === 'dr.destination_down') {
    return {
      push: true,
      severity: 'warn',
      title: 'وجهة DR غير متاحة',
      body: `${d.name ?? d.destination_id} (${d.kind ?? '—'})`,
    };
  }
  return { push: false, severity: 'info', title: '' };
}

function ingestBillingEvent(msg: AdminWsMessage<any>): IngestResult {
  const d = msg.data || {};
  if (msg.type === 'license.expiring') {
    const days = d.days_until_expiry ?? 0;
    return {
      push: true,
      severity: days <= 3 ? 'error' : 'warn',
      title: `ترخيص ينتهي خلال ${days} يوم`,
      body: `${d.tenant_name ?? d.tenant_id} — ${d.plan ?? ''}`,
    };
  }
  return { push: false, severity: 'info', title: '' };
}

function ingestFederationEvent(msg: AdminWsMessage<any>): IngestResult {
  const d = msg.data || {};
  if (msg.type === 'federation.peer_quarantined') {
    return {
      push: true,
      severity: 'warn',
      title: 'تم حجر قِرن اتحاد',
      body: `${d.server_id?.slice(0, 16) ?? '?'} — ${d.reason ?? 'سياسة'}`,
    };
  }
  if (msg.type === 'federation.peer_down') {
    return {
      push: true,
      severity: 'warn',
      title: 'قِرن غير متاح',
      body: `${d.server_id?.slice(0, 16) ?? '?'}`,
    };
  }
  return { push: false, severity: 'info', title: '' };
}

function ingestQosEvent(msg: AdminWsMessage<any>): IngestResult {
  const d = msg.data || {};
  if (msg.type === 'qos.critical') {
    return {
      push: true,
      severity: 'error',
      title: 'جودة مكالمة حرجة',
      body: `call=${d.call_id?.slice(0, 8)} MOS=${d.mos?.toFixed?.(2) ?? '?'} loss=${d.loss_percent ?? '?'}%`,
    };
  }
  return { push: false, severity: 'info', title: '' };
}

function ingestPluginsEvent(msg: AdminWsMessage<any>): IngestResult {
  const d = msg.data || {};
  if (msg.type === 'plugin.update_available') {
    return {
      push: true,
      severity: 'info',
      title: 'تحديث إضافة متوفر',
      body: `${d.name ?? d.id} → v${d.new_version ?? '?'}`,
    };
  }
  if (msg.type === 'plugin.crashed') {
    return {
      push: true,
      severity: 'error',
      title: 'انهيار إضافة',
      body: `${d.name ?? d.id}: ${d.error ?? '?'}`,
    };
  }
  return { push: false, severity: 'info', title: '' };
}

// ─────────────────────────────────────────────────────────────────────────
//  Component
// ─────────────────────────────────────────────────────────────────────────

export interface AdminNotificationsProps {
  /** Compact mode: just the bell + badge (the drawer is shown via the store). */
  compact?: boolean;
  /** Callback when user clicks a notification with a link — host navigates. */
  onNavigateToPanel?: (slug: AdminPanelSlug) => void;
}

const AdminNotifications: React.FC<AdminNotificationsProps> = ({ compact = false, onNavigateToPanel }) => {
  const unreadCount = useAdminStore((s) => s.unreadCount);
  const drawerOpen = useAdminStore((s) => s.drawerOpen);
  const setDrawerOpen = useAdminStore((s) => s.setDrawerOpen);
  const notifications = useAdminStore((s) => s.notifications);
  const markAllSeen = useAdminStore((s) => s.markAllSeen);
  const dismissNotification = useAdminStore((s) => s.dismissNotification);
  const pushNotification = useAdminStore((s) => s.pushNotification);
  const setWsStatus = useAdminStore((s) => s.setWsStatus);

  // ── Wire WS subscriptions ────────────────────────────────────────────
  useEffect(() => {
    const mgr = getAdminWsManager();

    const handler = (channel: AdminWsChannel, msg: AdminWsMessage<any>) => {
      let result: IngestResult = { push: false, severity: 'info', title: '' };
      switch (channel) {
        case 'audit':      result = ingestAuditEvent(msg); break;
        case 'dr':         result = ingestDrEvent(msg); break;
        case 'federation': result = ingestFederationEvent(msg); break;
        case 'qos':        result = ingestQosEvent(msg); break;
        case 'plugins':    result = ingestPluginsEvent(msg); break;
        // metrics + topology are too high-volume to surface as notifications;
        // they update the live panels instead.
      }
      if (result.push) {
        pushNotification({
          channel,
          severity: result.severity,
          title: result.title,
          body: result.body,
          link: CHANNEL_TO_SLUG[channel],
          raw: msg,
        });
      }
      // Also catch license-expiring events that ride over the audit channel
      // in older builds:
      if (channel === 'audit') {
        const billing = ingestBillingEvent(msg);
        if (billing.push) {
          pushNotification({
            channel,
            severity: billing.severity,
            title: billing.title,
            body: billing.body,
            link: 'billing_tenancy',
            raw: msg,
          });
        }
      }
    };

    const channels: AdminWsChannel[] = ['audit', 'dr', 'plugins', 'federation', 'qos'];
    const unsubs = channels.map((c) => mgr.subscribe(c, (m) => handler(c, m)));

    // Listen to in-renderer custom events from EmbeddedAdminPanel so server
    // panels can push notifications via postMessage too.
    const onCustomNotify = (ev: Event) => {
      const ce = ev as CustomEvent<{ slug?: AdminPanelSlug; severity: NotificationSeverity; text: string; ts?: number }>;
      const d = ce.detail;
      if (!d) return;
      pushNotification({
        slug: d.slug,
        severity: d.severity,
        title: d.text,
        ts: d.ts,
      });
    };
    window.addEventListener('helen-admin-notify', onCustomNotify as EventListener);

    // Poll status periodically so the badge dot reflects connectivity.
    const statusTimer = setInterval(() => setWsStatus(mgr.status()), 3000);
    setWsStatus(mgr.status());

    return () => {
      unsubs.forEach((u) => u());
      clearInterval(statusTimer);
      window.removeEventListener('helen-admin-notify', onCustomNotify as EventListener);
    };
  }, [pushNotification, setWsStatus]);

  const visible = useMemo(
    () => notifications.filter((n) => !n.dismissed).slice(0, 100),
    [notifications],
  );

  const toggle = useCallback(() => {
    const next = !drawerOpen;
    setDrawerOpen(next);
    if (next) markAllSeen();
  }, [drawerOpen, setDrawerOpen, markAllSeen]);

  // ── Render ───────────────────────────────────────────────────────────
  return (
    <>
      <button
        onClick={toggle}
        className={`relative inline-flex items-center justify-center w-9 h-9 rounded-md transition-colors ${
          drawerOpen ? 'bg-blue-600 text-white' : 'bg-surface-800 hover:bg-surface-700 text-gray-300'
        }`}
        title="إشعارات المسؤول"
      >
        <Bell size={16} />
        {unreadCount > 0 && (
          <span className="absolute -top-1 -right-1 min-w-[18px] h-[18px] px-1 rounded-full bg-red-600 text-white text-[10px] font-bold flex items-center justify-center ring-2 ring-surface-950">
            {unreadCount > 99 ? '99+' : unreadCount}
          </span>
        )}
      </button>

      {!compact && drawerOpen && (
        <div className="fixed inset-y-0 left-0 w-[380px] z-40 bg-surface-950 border-r border-surface-800 shadow-2xl flex flex-col">
          <div className="flex items-center justify-between px-3 py-2 border-b border-surface-800">
            <div className="text-sm font-semibold text-white">إشعارات المسؤول</div>
            <button onClick={() => setDrawerOpen(false)} className="text-gray-400 hover:text-white">
              <X size={16} />
            </button>
          </div>
          <div className="flex-1 overflow-auto p-2 space-y-1">
            {visible.length === 0 && (
              <div className="text-gray-500 text-xs text-center py-8">لا توجد إشعارات</div>
            )}
            {visible.map((n) => (
              <div
                key={n.id}
                className={`rounded px-3 py-2 text-xs ${severityColor[n.severity]} ${n.seen ? 'opacity-80' : ''}`}
              >
                <div className="flex items-start gap-2">
                  <div className="shrink-0 mt-0.5 opacity-90">
                    {(n.channel && channelIcon[n.channel]) || severityIcon[n.severity]}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="font-semibold truncate">{n.title}</div>
                    {n.body && <div className="opacity-80 text-[11px] mt-0.5 break-words">{n.body}</div>}
                    <div className="flex items-center gap-2 mt-1 text-[10px] opacity-70">
                      <span>{new Date(n.ts).toLocaleTimeString()}</span>
                      {n.channel && <span>· {n.channel}</span>}
                      {n.link && onNavigateToPanel && (
                        <button
                          onClick={() => { onNavigateToPanel(n.link!); setDrawerOpen(false); }}
                          className="ml-auto underline hover:text-white"
                        >
                          فتح اللوحة
                        </button>
                      )}
                    </div>
                  </div>
                  <button
                    onClick={() => dismissNotification(n.id)}
                    className="shrink-0 text-gray-300 hover:text-white opacity-70 hover:opacity-100"
                    title="تجاهل"
                  >
                    <X size={12} />
                  </button>
                </div>
              </div>
            ))}
          </div>
          <div className="px-3 py-2 border-t border-surface-800 flex items-center justify-between text-[11px] text-gray-400">
            <span>{visible.length} عنصر</span>
            <button
              onClick={() => useAdminStore.getState().clearNotifications()}
              className="text-gray-400 hover:text-white"
            >
              مسح الكل
            </button>
          </div>
        </div>
      )}
    </>
  );
};

export default AdminNotifications;
