/**
 * HostMassActions — toolbar of bulk moderation actions for the host.
 *
 * Renders inside the existing host menu (or as a standalone chip).
 * Each action emits ``v2_call_host_force_all`` with the appropriate
 * type. Targets default to "everyone except me" so the host doesn't
 * accidentally mute themselves.
 *
 * Also handles incoming ``call:host_force`` events on the receiver
 * side: when ``action === 'mute'`` the local mute state is flipped
 * (the server already updated the authoritative flag); ``unmute`` /
 * ``video_on`` are soft prompts that show a toast.
 */

import React, { useEffect, useState } from 'react';
import { socketManager } from '@/services/socket.manager';
import { useCallStore } from '@/stores/call.store.v2';
import { useAuthStore } from '@/stores/auth.store';
import { Mic, MicOff, Camera, CameraOff } from 'lucide-react';

const HostMassActions: React.FC = () => {
  const callId = useCallStore((s) => s.callId);
  const status = useCallStore((s) => s.status);
  const hostId = useCallStore((s) => s.hostId);
  const me = useAuthStore((s) => s.user);
  const isHost = !!me && hostId === me.id;

  const [softToast, setSoftToast] = useState<string | null>(null);

  // Receiver: react to incoming host-force events.
  useEffect(() => {
    if (!callId) return;
    const off = socketManager.on('call:host_force', (data: any) => {
      if (data?.call_id !== callId) return;
      // The server already toggled flags for hard actions, but the
      // local engine state needs to follow so the UI updates. We
      // call toggleMute / toggleVideo only when the local state
      // diverges; this avoids double-flipping.
      const cs = useCallStore.getState();
      if (data.action === 'mute' && !cs.isMuted) {
        cs.toggleMute();
        setSoftToast('قام المضيف بكتم الجميع');
      } else if (data.action === 'video_off' && !cs.isVideoOff) {
        cs.toggleVideo();
        setSoftToast('قام المضيف بإيقاف فيديو الجميع');
      } else if (data.action === 'unmute') {
        setSoftToast('طلب المضيف منكم تشغيل الميك');
      } else if (data.action === 'video_on') {
        setSoftToast('طلب المضيف منكم تشغيل الكاميرا');
      }
      setTimeout(() => setSoftToast(null), 4000);
    });
    return () => { try { off(); } catch { /* */ } };
  }, [callId]);

  if (!callId) return null;
  if (status !== 'active' && status !== 'reconnecting') return null;

  const fire = (action: 'mute' | 'unmute' | 'video_off' | 'video_on') => {
    socketManager.emitNoAck('v2_call_host_force_all', {
      call_id: callId,
      action,
      except_self: true,
    });
  };

  return (
    <>
      {/* Host-only mass-action chip cluster. Sits in the top-left
          row alongside the lobby/passcode badges. */}
      {isHost && (
        <div className="fixed top-4 left-72 z-30 flex items-center gap-1
                        bg-black/60 hover:bg-black/80 rounded-full p-0.5 backdrop-blur shadow-lg">
          <button
            onClick={() => fire('mute')}
            className="px-2.5 py-1 rounded-full flex items-center gap-1
                       text-[11px] text-white/80 hover:text-white hover:bg-white/10"
            title="كتم الجميع"
          >
            <MicOff size={12} />
            <span>كتم الكل</span>
          </button>
          <button
            onClick={() => fire('unmute')}
            className="px-2.5 py-1 rounded-full flex items-center gap-1
                       text-[11px] text-white/80 hover:text-white hover:bg-white/10"
            title="طلب من الجميع تشغيل الميك"
          >
            <Mic size={12} />
            <span>السماح</span>
          </button>
          <button
            onClick={() => fire('video_off')}
            className="px-2.5 py-1 rounded-full flex items-center gap-1
                       text-[11px] text-white/80 hover:text-white hover:bg-white/10"
            title="إيقاف فيديو الجميع"
          >
            <CameraOff size={12} />
            <span>كاميرا الكل</span>
          </button>
        </div>
      )}

      {/* Soft toast for receiver actions. Auto-dismisses after 4s. */}
      {softToast && (
        <div className="fixed top-16 left-1/2 -translate-x-1/2 z-40
                        px-4 py-2 rounded-lg bg-blue-500/95 text-white
                        text-sm shadow-2xl pointer-events-none">
          {softToast}
        </div>
      )}
    </>
  );
};

export default HostMassActions;
