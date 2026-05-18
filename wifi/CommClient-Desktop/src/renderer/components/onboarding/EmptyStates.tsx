/**
 * EmptyStates.tsx — Friendly empty-state screens for first-time users.
 *
 * After onboarding, the user lands in the app with zero content.
 * These screens fill the void with:
 *   - Warm illustrations (icon-based, no external assets)
 *   - Clear explanation of what belongs here
 *   - Single CTA to get started
 *   - Encouraging tone (never "empty" or "nothing here")
 *
 * Each empty state is context-aware and provides the next logical action.
 */

import React from 'react';
import {
  MessageCircle, UserPlus, Phone, Users, AlertCircle,
  ArrowRight, ArrowLeft, Star, Search, Bell,
} from 'lucide-react';
import { t, getLanguage } from '@/i18n';

// ── Shared AlertCircle ───────────────────────────────────────────

interface EmptyStateProps {
  icon: React.ReactNode;
  iconBgClass: string;
  title: string;
  subtitle: string;
  actionLabel?: string;
  onAction?: () => void;
  secondaryLabel?: string;
  onSecondary?: () => void;
  decoration?: React.ReactNode;
}

const EmptyStateLayout: React.FC<EmptyStateProps> = ({
  icon,
  iconBgClass,
  title,
  subtitle,
  actionLabel,
  onAction,
  secondaryLabel,
  onSecondary,
  decoration,
}) => {
  const isRTL = getLanguage() === 'ar';
  const ArrowIcon = isRTL ? ArrowLeft : ArrowRight;

  return (
    <div className="flex flex-col items-center justify-center h-full px-6 py-12 select-none">
      {/* Illustration area */}
      <div className="relative mb-6">
        <div className={`w-20 h-20 rounded-2xl ${iconBgClass} flex items-center justify-center`}>
          {icon}
        </div>
        {decoration}
      </div>

      {/* Text */}
      <div className="text-center max-w-xs mb-8">
        <h2 className="text-lg font-semibold text-white mb-2">{title}</h2>
        <p className="text-sm text-gray-400 leading-relaxed">{subtitle}</p>
      </div>

      {/* Actions */}
      <div className="flex flex-col items-center gap-3 w-full max-w-xs">
        {actionLabel && onAction && (
          <button
            onClick={onAction}
            className="w-full py-3 bg-blue-600 hover:bg-blue-700 text-white font-medium rounded-xl transition-colors flex items-center justify-center gap-2 text-sm shadow-lg shadow-blue-600/10"
          >
            {actionLabel}
            <ArrowIcon size={16} />
          </button>
        )}
        {secondaryLabel && onSecondary && (
          <button
            onClick={onSecondary}
            className="text-sm text-gray-500 hover:text-gray-300 transition-colors"
          >
            {secondaryLabel}
          </button>
        )}
      </div>
    </div>
  );
};

// ── Empty Chats ─────────────────────────────────────────────

export interface EmptyChatsProps {
  onStartChat?: () => void;
  onAddContact?: () => void;
}

export const EmptyChats: React.FC<EmptyChatsProps> = ({ onStartChat, onAddContact }) => (
  <EmptyStateLayout
    icon={<MessageCircle size={36} className="text-blue-400" />}
    iconBgClass="bg-blue-500/10"
    title={t('empty.chats_title')}
    subtitle={t('empty.chats_subtitle')}
    actionLabel={t('empty.chats_action')}
    onAction={onStartChat}
    secondaryLabel={t('empty.chats_secondary')}
    onSecondary={onAddContact}
    decoration={
      <>
        <Star size={14} className="text-blue-400/50 absolute -top-1 -right-2 animate-pulse" />
        <div className="absolute -bottom-1 -left-1 w-5 h-5 rounded bg-blue-500/10 rotate-12" />
      </>
    }
  />
);

// ── Empty Contacts ──────────────────────────────────────────

export interface EmptyContactsProps {
  onSearchUsers?: () => void;
}

export const EmptyContacts: React.FC<EmptyContactsProps> = ({ onSearchUsers }) => (
  <EmptyStateLayout
    icon={<UserPlus size={36} className="text-green-400" />}
    iconBgClass="bg-green-500/10"
    title={t('empty.contacts_title')}
    subtitle={t('empty.contacts_subtitle')}
    actionLabel={t('empty.contacts_action')}
    onAction={onSearchUsers}
    decoration={
      <div className="absolute -top-2 -right-2 w-8 h-8 rounded-full bg-green-500/10 flex items-center justify-center">
        <Search size={12} className="text-green-400/50" />
      </div>
    }
  />
);

// ── Empty Calls ─────────────────────────────────────────────

export interface EmptyCallsProps {
  onMakeCall?: () => void;
}

export const EmptyCalls: React.FC<EmptyCallsProps> = ({ onMakeCall }) => (
  <EmptyStateLayout
    icon={<Phone size={36} className="text-emerald-400" />}
    iconBgClass="bg-emerald-500/10"
    title={t('empty.calls_title')}
    subtitle={t('empty.calls_subtitle')}
    actionLabel={t('empty.calls_action')}
    onAction={onMakeCall}
    decoration={
      <div className="absolute -bottom-2 -right-1 w-6 h-3 rounded-full bg-emerald-500/10 rotate-45" />
    }
  />
);

// ── Empty Groups ────────────────────────────────────────────

export interface EmptyGroupsProps {
  onCreateGroup?: () => void;
}

export const EmptyGroups: React.FC<EmptyGroupsProps> = ({ onCreateGroup }) => (
  <EmptyStateLayout
    icon={<Users size={36} className="text-purple-400" />}
    iconBgClass="bg-purple-500/10"
    title={t('empty.groups_title')}
    subtitle={t('empty.groups_subtitle')}
    actionLabel={t('empty.groups_action')}
    onAction={onCreateGroup}
    decoration={
      <div className="absolute -top-1 -left-2 w-4 h-4 rounded-full bg-purple-500/15" />
    }
  />
);

// ── Empty Notifications ─────────────────────────────────────

export const EmptyNotifications: React.FC = () => (
  <EmptyStateLayout
    icon={<Bell size={36} className="text-yellow-400" />}
    iconBgClass="bg-yellow-500/10"
    title={t('empty.notif_title')}
    subtitle={t('empty.notif_subtitle')}
    decoration={
      <div className="absolute -top-2 right-0 w-3 h-3 rounded-full bg-yellow-500/20" />
    }
  />
);

// ── Empty Screen Share ──────────────────────────────────────

export interface EmptyScreenShareProps {
  onShareScreen?: () => void;
}

export const EmptyScreenShare: React.FC<EmptyScreenShareProps> = ({ onShareScreen }) => (
  <EmptyStateLayout
    icon={<AlertCircle size={36} className="text-cyan-400" />}
    iconBgClass="bg-cyan-500/10"
    title={t('empty.screen_title')}
    subtitle={t('empty.screen_subtitle')}
    actionLabel={t('empty.screen_action')}
    onAction={onShareScreen}
  />
);
