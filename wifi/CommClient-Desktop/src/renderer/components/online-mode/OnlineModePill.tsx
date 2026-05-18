/**
 * OnlineModePill — title-bar indicator that shows whether the
 * server's master "Online Mode" toggle is on or off.
 *
 *   * Every authenticated user sees the indicator (read-only).
 *   * Admins see a clickable button that flips the gate via the
 *     admin endpoints. Non-admins clicking the pill open a tooltip
 *     explaining what it means.
 *
 * Renders nothing if the user isn't authenticated yet, or if the
 * server returns ``configured: false`` (the gate hasn't been wired
 * — older servers without this feature stay invisible to clients).
 *
 * Polls the server every 10s. The poll is cheap (one auth'd GET) and
 * the indicator is small enough that real-time accuracy isn't worth
 * a websocket subscription.
 */

import React, { useEffect, useState } from 'react';
import { Globe, Lock } from 'lucide-react';
import toast from 'react-hot-toast';
import { useAuthStore } from '@/stores/auth.store';
import { api } from '@/services/api.client';

type ServiceLite = { name: string; running: boolean };

type Status = {
  configured: boolean;
  enabled: boolean;
  last_change_at?: number | null;
  services?: ServiceLite[];
};

const POLL_INTERVAL_MS = 10_000;

export const OnlineModePill: React.FC = () => {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const me = useAuthStore((s) => s.user);
  const isAdmin = me?.role === 'admin';

  const [status, setStatus] = useState<Status | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!isAuthenticated) {
      setStatus(null);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      try {
        const s = await api.onlineMode.status();
        if (!cancelled) setStatus(s as Status);
      } catch {
        // Silently ignore — older servers won't have the endpoint.
        if (!cancelled) setStatus(null);
      } finally {
        if (!cancelled) {
          timer = setTimeout(tick, POLL_INTERVAL_MS);
        }
      }
    };
    tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [isAuthenticated]);

  if (!isAuthenticated || !status?.configured) return null;

  const enabled = status.enabled;

  const onClick = async () => {
    if (!isAdmin) {
      toast(
        enabled
          ? 'بوّابة الإنترنت مفتوحة — السيرفر يستخدم خدمات خارجية اختيارية'
          : 'الوضع الطبيعي — كل شيء يعمل عبر الـ LAN. لا حاجة للإنترنت',
        { icon: enabled ? '🌐' : '🔒' },
      );
      return;
    }
    if (busy) return;
    const verb = enabled ? 'إيقاف' : 'تفعيل';
    if (!window.confirm(`${verb} وضع الإنترنت؟`)) return;
    setBusy(true);
    try {
      const reply = enabled
        ? await api.onlineMode.disable()
        : await api.onlineMode.enable();
      setStatus(reply as Status);
      toast.success(
        enabled
          ? 'تم إيقاف وضع الإنترنت — رجعنا LAN فقط'
          : 'تم تفعيل وضع الإنترنت',
      );
    } catch (e: any) {
      toast.error('فشل التبديل: ' + (e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  const Icon = enabled ? Globe : Lock;
  const colour = enabled
    ? 'bg-emerald-700/30 text-emerald-300 border border-emerald-600/40 hover:bg-emerald-700/50'
    : 'bg-slate-700/30 text-slate-300 border border-slate-600/40 hover:bg-slate-700/50';

  const runningCount = (status.services || []).filter((s) => s.running).length;
  const totalCount = (status.services || []).length;
  const subscript =
    totalCount > 0 ? ` (${runningCount}/${totalCount})` : '';

  // The previous wording — "وضع الإنترنت متوقف" — read like a fault
  // ("internet is off") even though the LAN-only state is the *normal*
  // and *desired* one for Helen. Reword so users understand:
  //   - LAN-only  = الوضع الطبيعي، كل شيء يشتغل عبر الشبكة المحلية
  //   - Online    = الأدمن فتح بوّابة اختيارية لخدمات إنترنت إضافية
  const title = isAdmin
    ? (enabled
        ? `بوّابة الإنترنت مفتوحة — السيرفر يستخدم خدمات خارجية${subscript}\nانقر لإغلاقها (الرجوع لـ LAN فقط)`
        : `الوضع: LAN فقط (الطبيعي) — كل شيء يعمل عبر الشبكة المحلية${subscript}\nانقر لفتح بوّابة الإنترنت الاختيارية`)
    : enabled
    ? 'السيرفر يستخدم بعض خدمات الإنترنت — الوظائف الأساسية تعمل عبر الـ LAN كالعادة'
    : 'الوضع الطبيعي — السيرفر يعمل بالكامل على الشبكة المحلية';

  return (
    <button
      onClick={onClick}
      disabled={busy}
      className={
        'flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium ' +
        'transition-colors disabled:opacity-60 ' +
        colour
      }
      style={{ WebkitAppRegion: 'no-drag' } as React.CSSProperties}
      title={title}
      aria-label={
        enabled ? 'Online mode is on' : 'Online mode is off'
      }
    >
      <Icon size={11} />
      <span>{enabled ? 'Online' : 'LAN-only'}</span>
      {totalCount > 0 && (
        <span className="text-[10px] opacity-70">
          {runningCount}/{totalCount}
        </span>
      )}
    </button>
  );
};
