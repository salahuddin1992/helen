/**
 * AdminPanelRegistry — central registry for the 11 new advanced admin panels
 * that ship with Helen-Server 2026.05.
 *
 * The panels are HTML SPAs served by the backend under
 *   /admin/modules/<slug>.html
 * and are surfaced inside the Electron desktop client as embedded webviews
 * (see EmbeddedAdminPanel.tsx). A future revision can replace each entry
 * with a native React tab; the registry is the single source of truth so
 * the migration only touches `renderMode`.
 *
 * Why a registry instead of inlining the metadata in AdminPanel.tsx?
 *   - The 11 panels share routing/permission/i18n/category semantics. Keep
 *     them in one table so the operator console (and any future Spotlight /
 *     command palette wiring) can iterate them generically.
 *   - i18n labels are stored under separate slug-keyed namespaces
 *     (admin_panels_ar.json / admin_panels_en.json) so translators can ship
 *     panel strings without rebuilding the renderer.
 *   - Hotkeys are declared here so we can register them in one place against
 *     `keyboard-shortcuts.store`.
 */

import type { LucideIcon } from 'lucide-react';
import {
  Activity, Network, Router, ShieldCheck, CreditCard, HardDrive,
  Boxes, GitBranch, Mic, Scale, Wand2,
} from 'lucide-react';

// ── Slug union — must match the file names under
// /admin/modules/<slug>.html exposed by Helen-Server.
export type AdminPanelSlug =
  | 'monitoring_unified'
  | 'topology_visualizer'
  | 'router_control'
  | 'siem_audit'
  | 'billing_tenancy'
  | 'dr_console'
  | 'plugin_marketplace'
  | 'federation_health'
  | 'qos_live'
  | 'compliance_ediscovery'
  | 'onboarding_wizard';

export type AdminPanelCategory =
  | 'observability'
  | 'security'
  | 'governance'
  | 'operations'
  | 'setup';

/** Rendering mode for the panel inside the desktop client. */
export type AdminPanelRenderMode = 'embed' | 'native';

export interface AdminPanelMeta {
  slug: AdminPanelSlug;
  /** Arabic short label (RTL nav). */
  labelAr: string;
  /** English short label (LTR nav). */
  labelEn: string;
  /** Lucide icon component. */
  icon: LucideIcon;
  /** Server-side permission required to access the panel (informational; the
   *  server still enforces). */
  requiresPermission: string;
  category: AdminPanelCategory;
  /** Optional Electron-style accelerator (e.g. "CmdOrCtrl+Shift+M"). */
  hotkey?: string;
  /** How to render inside the desktop client. */
  renderMode: AdminPanelRenderMode;
  /** WebSocket channels this panel listens to for live updates / notifications. */
  wsChannels?: ReadonlyArray<AdminWsChannel>;
  /** Short i18n description shown in tab tooltip / spotlight. */
  descriptionKeyAr: string;
  descriptionKeyEn: string;
  /** Categorization color hint for the nav rail (Tailwind). */
  colorClass: string;
}

/** WS channel identifiers — also see AdminWebSocketManager.ts. */
export type AdminWsChannel =
  | 'metrics'
  | 'topology'
  | 'audit'
  | 'dr'
  | 'plugins'
  | 'federation'
  | 'qos';

// ─────────────────────────────────────────────────────────────────────────────
//  Panel definitions
// ─────────────────────────────────────────────────────────────────────────────

export const ADMIN_PANELS: ReadonlyArray<AdminPanelMeta> = [
  // ── Observability ────────────────────────────────────────────────────────
  {
    slug: 'monitoring_unified',
    labelAr: 'لوحة المراقبة الموحدة',
    labelEn: 'Unified Monitoring',
    icon: Activity,
    requiresPermission: 'admin.monitoring.read',
    category: 'observability',
    hotkey: 'CmdOrCtrl+Shift+M',
    renderMode: 'embed',
    wsChannels: ['metrics'],
    descriptionKeyAr: 'metrics خادم، اتصالات حية، صحة الأنظمة الفرعية',
    descriptionKeyEn: 'Server metrics, live connections, subsystem health',
    colorClass: 'text-emerald-400',
  },
  {
    slug: 'topology_visualizer',
    labelAr: 'مخطط البنية',
    labelEn: 'Topology Visualizer',
    icon: Network,
    requiresPermission: 'admin.topology.read',
    category: 'observability',
    hotkey: 'CmdOrCtrl+Shift+T',
    renderMode: 'embed',
    wsChannels: ['topology'],
    descriptionKeyAr: 'مخطط ديناميكي للأنواد، الأقران، وجلسات SFU',
    descriptionKeyEn: 'Dynamic graph of nodes, peers and SFU sessions',
    colorClass: 'text-cyan-400',
  },
  {
    slug: 'qos_live',
    labelAr: 'جودة الاتصال المباشر',
    labelEn: 'QoS Live',
    icon: Mic,
    requiresPermission: 'admin.qos.read',
    category: 'observability',
    hotkey: 'CmdOrCtrl+Shift+Q',
    renderMode: 'embed',
    wsChannels: ['qos'],
    descriptionKeyAr: 'MOS / jitter / loss للمكالمات النشطة في الوقت الفعلي',
    descriptionKeyEn: 'Real-time MOS / jitter / loss for active calls',
    colorClass: 'text-sky-400',
  },
  {
    slug: 'federation_health',
    labelAr: 'صحة الاتحاد',
    labelEn: 'Federation Health',
    icon: GitBranch,
    requiresPermission: 'admin.federation.read',
    category: 'observability',
    hotkey: 'CmdOrCtrl+Shift+F',
    renderMode: 'embed',
    wsChannels: ['federation'],
    descriptionKeyAr: 'خريطة الأقران، حجر صحي، تأخير الاتحاد',
    descriptionKeyEn: 'Peer health map, quarantine, federation lag',
    colorClass: 'text-indigo-400',
  },

  // ── Security ─────────────────────────────────────────────────────────────
  {
    slug: 'siem_audit',
    labelAr: 'SIEM / تدقيق',
    labelEn: 'SIEM / Audit',
    icon: ShieldCheck,
    requiresPermission: 'admin.siem.read',
    category: 'security',
    hotkey: 'CmdOrCtrl+Shift+A',
    renderMode: 'embed',
    wsChannels: ['audit'],
    descriptionKeyAr: 'سجل سلسلة التدقيق، تنبيهات أمنية، تحقق من السلامة',
    descriptionKeyEn: 'Audit chain, security alerts, integrity verify',
    colorClass: 'text-rose-400',
  },
  {
    slug: 'compliance_ediscovery',
    labelAr: 'الامتثال / eDiscovery',
    labelEn: 'Compliance / eDiscovery',
    icon: Scale,
    requiresPermission: 'admin.compliance.read',
    category: 'security',
    renderMode: 'embed',
    descriptionKeyAr: 'حجوزات قانونية، RTBF، تصدير GDPR',
    descriptionKeyEn: 'Legal holds, RTBF, GDPR exports',
    colorClass: 'text-fuchsia-400',
  },

  // ── Governance ───────────────────────────────────────────────────────────
  {
    slug: 'billing_tenancy',
    labelAr: 'الفوترة / المستأجرون',
    labelEn: 'Billing / Tenancy',
    icon: CreditCard,
    requiresPermission: 'admin.billing.read',
    category: 'governance',
    renderMode: 'embed',
    descriptionKeyAr: 'مستأجرون، تراخيص، خطط، تنبيهات انتهاء',
    descriptionKeyEn: 'Tenants, licenses, plans, expiry alerts',
    colorClass: 'text-amber-400',
  },
  {
    slug: 'plugin_marketplace',
    labelAr: 'سوق الإضافات',
    labelEn: 'Plugin Marketplace',
    icon: Boxes,
    requiresPermission: 'admin.plugins.read',
    category: 'governance',
    renderMode: 'embed',
    wsChannels: ['plugins'],
    descriptionKeyAr: 'الإضافات المثبتة، التحديثات، صلاحيات sandbox',
    descriptionKeyEn: 'Installed plugins, updates, sandbox permissions',
    colorClass: 'text-violet-400',
  },

  // ── Operations ───────────────────────────────────────────────────────────
  {
    slug: 'dr_console',
    labelAr: 'وحدة التعافي من الكوارث',
    labelEn: 'DR Console',
    icon: HardDrive,
    requiresPermission: 'admin.dr.read',
    category: 'operations',
    hotkey: 'CmdOrCtrl+Shift+D',
    renderMode: 'embed',
    wsChannels: ['dr'],
    descriptionKeyAr: 'أهداف النسخ الاحتياطي، سياسات، failover، فحص',
    descriptionKeyEn: 'Backup destinations, policies, failover, verify',
    colorClass: 'text-orange-400',
  },
  {
    slug: 'router_control',
    labelAr: 'تحكم الموجّه',
    labelEn: 'Router Control',
    icon: Router,
    requiresPermission: 'admin.router.read',
    category: 'operations',
    renderMode: 'embed',
    descriptionKeyAr: 'Helen-Router: jails، routes، أنفاق',
    descriptionKeyEn: 'Helen-Router: jails, routes, tunnels',
    colorClass: 'text-teal-400',
  },

  // ── Setup ────────────────────────────────────────────────────────────────
  {
    slug: 'onboarding_wizard',
    labelAr: 'معالج الإعداد',
    labelEn: 'Onboarding Wizard',
    icon: Wand2,
    requiresPermission: 'admin.onboarding.read',
    category: 'setup',
    renderMode: 'embed',
    descriptionKeyAr: 'إعداد أوّلي للمشغّل: TLS، DNS، تخزين، نسخ احتياطي',
    descriptionKeyEn: 'Operator first-run: TLS, DNS, storage, backup',
    colorClass: 'text-lime-400',
  },
];

// ─────────────────────────────────────────────────────────────────────────────
//  Helpers
// ─────────────────────────────────────────────────────────────────────────────

/** Group panels by category for the sidebar. */
export function groupByCategory(): Record<AdminPanelCategory, AdminPanelMeta[]> {
  const out: Record<AdminPanelCategory, AdminPanelMeta[]> = {
    observability: [],
    security: [],
    governance: [],
    operations: [],
    setup: [],
  };
  for (const p of ADMIN_PANELS) out[p.category].push(p);
  return out;
}

/** Look up a panel by slug. */
export function getPanel(slug: AdminPanelSlug): AdminPanelMeta | undefined {
  return ADMIN_PANELS.find((p) => p.slug === slug);
}

/** All WS channels a given role should subscribe to based on its panels. */
export function wsChannelsForPanels(slugs: AdminPanelSlug[]): AdminWsChannel[] {
  const set = new Set<AdminWsChannel>();
  for (const s of slugs) {
    const meta = getPanel(s);
    meta?.wsChannels?.forEach((c) => set.add(c));
  }
  return Array.from(set);
}

/** Category display order on the sidebar. */
export const CATEGORY_ORDER: AdminPanelCategory[] = [
  'observability',
  'security',
  'governance',
  'operations',
  'setup',
];

/** Localized category headings. */
export const CATEGORY_LABELS: Record<AdminPanelCategory, { ar: string; en: string }> = {
  observability: { ar: 'المراقبة', en: 'Observability' },
  security:      { ar: 'الأمن',    en: 'Security' },
  governance:    { ar: 'الحوكمة',  en: 'Governance' },
  operations:    { ar: 'العمليات', en: 'Operations' },
  setup:         { ar: 'الإعداد',   en: 'Setup' },
};
