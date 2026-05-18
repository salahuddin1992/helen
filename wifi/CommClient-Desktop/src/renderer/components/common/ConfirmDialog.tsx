import React from 'react';
import { t } from '@/i18n';
import { Modal } from './Modal';

interface ConfirmDialogProps {
  isOpen: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: 'default' | 'danger';
  onConfirm: () => void;
  onCancel: () => void;
  isLoading?: boolean;
}

export const ConfirmDialog: React.FC<ConfirmDialogProps> = ({
  isOpen,
  title,
  message,
  confirmLabel,
  cancelLabel,
  variant = 'default',
  onConfirm,
  onCancel,
  isLoading = false,
}) => {
  const confirmButtonClasses =
    variant === 'danger'
      ? 'bg-red-500 hover:bg-red-600 disabled:bg-red-500/50'
      : 'bg-primary-500 hover:bg-primary-600 disabled:bg-primary-500/50';

  return (
    <Modal isOpen={isOpen} onClose={onCancel} title={title}>
      <div className="space-y-6">
        {/* Message */}
        <p className="text-surface-300 text-sm">{message}</p>

        {/* Buttons */}
        <div className="flex gap-3 justify-end">
          <button
            onClick={onCancel}
            disabled={isLoading}
            className="px-4 py-2 bg-surface-800 hover:bg-surface-700 disabled:bg-surface-800/50 text-white text-sm font-medium rounded transition-colors"
            aria-label={cancelLabel || t('common.cancel')}
          >
            {cancelLabel || t('common.cancel')}
          </button>

          <button
            onClick={onConfirm}
            disabled={isLoading}
            className={`px-4 py-2 ${confirmButtonClasses} text-white text-sm font-medium rounded transition-colors disabled:cursor-not-allowed`}
            aria-label={confirmLabel || t('common.confirm')}
          >
            {isLoading ? (
              <div className="flex items-center gap-2">
                <div className="w-4 h-4 border-2 border-white/20 border-t-white rounded-full animate-spin" />
                {confirmLabel || t('common.confirm')}
              </div>
            ) : (
              confirmLabel || t('common.confirm')
            )}
          </button>
        </div>
      </div>
    </Modal>
  );
};
