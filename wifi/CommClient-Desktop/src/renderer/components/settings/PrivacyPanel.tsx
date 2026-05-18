/**
 * PrivacyPanel — toggles for outbound awareness signals.
 *
 * All toggles are *client-side only* — the server never enforces
 * them. Disabling a toggle just stops this client from emitting
 * the corresponding socket event:
 *
 *   * Read receipts       → ``v2_chat_mark_read``
 *   * Typing indicator    → ``v2_chat_typing_start/stop``
 *   * Online presence     → presence-broadcast pings
 *
 * The user keeps receiving everyone *else's* signals; the privacy
 * is one-directional. This matches Signal / WhatsApp / Telegram
 * behavior (where read-receipt suppression is a personal setting,
 * not a negotiated agreement).
 */

import React from 'react';
import { Eye, EyeOff } from 'lucide-react';
import { usePrivacyStore } from '@/stores/privacy.store';

interface ToggleProps {
  label: string;
  description: string;
  value: boolean;
  onChange: (v: boolean) => void;
}

const PrivacyToggle: React.FC<ToggleProps> = ({
  label, description, value, onChange,
}) => (
  <label className="flex items-start gap-3 p-2 rounded
                    hover:bg-surface-800 cursor-pointer">
    <div className="flex-none pt-0.5">
      <input
        type="checkbox"
        checked={value}
        onChange={(e) => onChange(e.target.checked)}
        className="w-4 h-4 accent-blue-500"
      />
    </div>
    <div className="flex-1">
      <div className="text-sm text-gray-100">{label}</div>
      <div className="text-[11px] text-gray-400 mt-0.5">
        {description}
      </div>
    </div>
    <div className="flex-none pt-1">
      {value
        ? <Eye size={14} className="text-emerald-400" />
        : <EyeOff size={14} className="text-amber-400" />}
    </div>
  </label>
);

export const PrivacyPanel: React.FC = () => {
  const sendRead = usePrivacyStore((s) => s.send_read_receipts);
  const sendTyping = usePrivacyStore((s) => s.send_typing_indicator);
  const sendPresence = usePrivacyStore((s) => s.send_presence);
  const setRead = usePrivacyStore((s) => s.setSendReadReceipts);
  const setTyping = usePrivacyStore((s) => s.setSendTypingIndicator);
  const setPresence = usePrivacyStore((s) => s.setSendPresence);

  return (
    <div className="bg-surface-900 border border-surface-700
                    rounded-lg p-4 space-y-2">
      <h3 className="text-sm font-semibold text-gray-100 mb-2">
        الخصوصية
      </h3>

      <PrivacyToggle
        label="إرسال إشعارات القراءة"
        description={
          'عندما تعطّل، لن يعرف الآخرون أنك قرأت رسائلهم. ' +
          'ستظل ترى علامات قراءتهم لرسائلك.'
        }
        value={sendRead}
        onChange={setRead}
      />

      <PrivacyToggle
        label="إظهار «يكتب الآن»"
        description="عندما تعطّل، لن يظهر للآخرين أنك تكتب في القناة."
        value={sendTyping}
        onChange={setTyping}
      />

      <PrivacyToggle
        label="مشاركة حالة الاتصال"
        description={
          'عندما تعطّل، يبدو حسابك دائماً غير متصل ' +
          'بالنسبة للمستخدمين الآخرين.'
        }
        value={sendPresence}
        onChange={setPresence}
      />
    </div>
  );
};
