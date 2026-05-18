/**
 * Common UI components barrel export
 */

export { Avatar } from './Avatar';
export { Modal } from './Modal';
export { StatusBadge } from './StatusBadge';
export { ErrorBoundary } from './ErrorBoundary';
export { EmojiPicker } from './EmojiPicker';
export { FilePreview } from './FilePreview';
export { TypingBubble } from './TypingBubble';
export { UnreadBadge } from './UnreadBadge';
export { UserProfileCard } from './UserProfileCard';
export { SearchBar } from './SearchBar';
export { ToastProvider, useToast } from './Toast';
export { ConfirmDialog } from './ConfirmDialog';

// Connection status components
export { default as ConnectionOverlay } from './ConnectionOverlay';
export { default as OfflineBanner } from './OfflineBanner';

export type { ToastType, ToastMessage } from './Toast';
