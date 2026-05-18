/**
 * Shared TypeScript types for the renderer process.
 */
import type { ElectronAPI } from '../../preload/index';

declare global {
  interface Window {
    electronAPI: ElectronAPI;
  }
}

// ── User ─────────────────────────────────────────
export interface User {
  id: string;
  username: string;
  display_name: string;
  avatar_url: string | null;
  bio?: string | null;
  status: UserStatus;
  last_seen?: string;
  created_at?: string;
  role?: 'user' | 'moderator' | 'admin';
}

export type UserStatus = 'online' | 'offline' | 'away' | 'busy' | 'dnd' | 'in_call';

// ── Profile photos ───────────────────────────────
export type ProfilePhotoVisibility = 'public' | 'contacts' | 'private';

export interface ProfilePhoto {
  id: string;
  user_id: string;
  visibility: ProfilePhotoVisibility;
  is_primary: boolean;
  position: number;
  mime_type: string;
  size_bytes: number;
  caption: string | null;
  url: string;
  created_at: string;
}

// ── Auth ─────────────────────────────────────────
export interface AuthTokens {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface AuthResponse {
  user: User;
  tokens: AuthTokens;
}

// ── Contact ──────────────────────────────────────
export interface Contact {
  id: string;
  contact: User;
  nickname: string | null;
  is_blocked: boolean;
  is_favorite: boolean;
  created_at: string;
}

// ── Channel ──────────────────────────────────────
export interface Channel {
  id: string;
  type: 'dm' | 'group';
  name: string | null;
  description: string | null;
  avatar_url: string | null;
  created_by: string | null;
  is_active: boolean;
  members: ChannelMember[];
  member_count: number;
  created_at: string;
  updated_at: string;
}

export interface ChannelMember {
  user_id: string;
  username: string;
  display_name: string;
  avatar_url: string | null;
  status: UserStatus;
  role: 'admin' | 'moderator' | 'member';
  joined_at: string;
}

// ── Message ──────────────────────────────────────
export interface Message {
  id: string;
  channel_id: string;
  sender: {
    id: string;
    username: string;
    display_name: string;
    avatar_url: string | null;
  };
  content: string;
  type: 'text' | 'file' | 'image' | 'system' | 'reply' | 'voice';
  reply_to: string | null;
  file_id: string | null;
  status: 'sent' | 'delivered' | 'read';
  reactions: ReactionInfo[];
  edited_at: string | null;
  created_at: string;
}

export interface ReactionInfo {
  emoji: string;
  count: number;
  user_ids: string[];
}

// ── Call ─────────────────────────────────────────
export interface CallState {
  call_id: string | null;
  status: 'idle' | 'ringing' | 'connecting' | 'active' | 'ended';
  type: 'audio' | 'video';
  routing: 'p2p' | 'sfu';
  is_initiator: boolean;
  remote_user_id: string | null;
  channel_id: string | null;
  participants: CallParticipant[];
  is_muted: boolean;
  is_video_off: boolean;
  is_screen_sharing: boolean;
  started_at: number | null;
}

export interface CallParticipant {
  user_id: string;
  display_name: string;
  avatar_url: string | null;
  muted: boolean;
  video_off: boolean;
  sharing_screen: boolean;
  stream: MediaStream | null;
}

export interface IncomingCall {
  call_id: string;
  caller_id: string;
  caller_name?: string;
  media_type: 'audio' | 'video';
  channel_id?: string;
}

// ── Screen Source ────────────────────────────────
export interface ScreenSource {
  id: string;
  name: string;
  thumbnail: string;
  appIcon: string | null;
  display_id: string;
}

// ── Settings ─────────────────────────────────────
// Resolution presets span 360p → 8K UHD. 'custom' lets the user type
// exact pixel dimensions for cameras with non-standard modes.
export type VideoResolution =
  | '360p' | '480p' | '720p' | '1080p' | '1440p'
  | '4k' | '5k' | '8k' | 'custom';
export type VideoFrameRate = 15 | 24 | 30 | 60 | 90 | 120;
export type AudioSampleRate = 8000 | 16000 | 24000 | 32000 | 44100 | 48000 | 96000;

export interface AppSettings {
  serverUrl: string;
  // 'system' tracks the OS prefers-color-scheme media query in real time.
  theme: 'dark' | 'light' | 'system';
  language: 'en' | 'ar';
  // Do Not Disturb. ISO timestamp string (UTC). null = off; "indefinite"
  // sentinel = until the user toggles it back. Anything in the past = off.
  dndUntil?: string | null;
  // Per-channel notification preferences. Map of channel_id → mode:
  //   'all'         — default (silent fallback when key absent)
  //   'mentions'    — only @mentions trigger a popup; other messages log silently
  //   'muted'       — never popup (badge still updates)
  // Stored client-side; the server doesn't track this so a fresh login
  // on another device reverts to 'all' for everything until the user
  // re-customizes. Sync to server is a future addition.
  channelMutes?: Record<string, 'all' | 'mentions' | 'muted'>;
  audioInputDevice: string;
  audioOutputDevice: string;
  videoInputDevice: string;
  notifications: boolean;
  startMinimized: boolean;
  pushToTalk: boolean;
  pushToTalkKey: string;

  // Camera quality
  videoResolution: VideoResolution;
  videoFrameRate: VideoFrameRate;
  mirrorCamera: boolean;
  // When videoResolution === 'custom' these pixel dimensions apply.
  // Also used as the fallback for unknown presets.
  customVideoWidth: number;
  customVideoHeight: number;
  // Manual override for frame rate — stored separately so flipping
  // presets doesn't clobber a user-entered value.
  customVideoFrameRate: number;
  useCustomFrameRate: boolean;

  // Audio quality / processing
  audioSampleRate: AudioSampleRate;
  echoCancellation: boolean;
  noiseSuppression: boolean;
  autoGainControl: boolean;
  microphoneGain: number; // 0-100
  speakerVolume: number;  // 0-100

  // When on, the client probes each camera / mic via getCapabilities()
  // and captures at the device's own maximum (still clamped by the server
  // policy). When off, the manual resolution/FPS/sample-rate fields above
  // decide. The server can force this on via MediaPolicy.auto_max_quality.
  autoMaxQuality: boolean;
}

// ── API Responses ────────────────────────────────
export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  has_more?: boolean;
}
