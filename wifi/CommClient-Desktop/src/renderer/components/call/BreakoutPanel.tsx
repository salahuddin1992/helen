/**
 * BreakoutPanel — host-side breakout-room manager.
 *
 * Workflow
 * --------
 * 1. Host clicks the chip → drawer opens with a list of all
 *    participants and a "groups" pane on the right.
 * 2. Host names each group ("فريق التطوير", "فريق التصميم", ...) and
 *    drags participants into them (or types their id; minimal UI
 *    here uses checkboxes for speed).
 * 3. "تشغيل" emits ``v2_call_breakout_open`` with the assignment.
 * 4. The server fans out per-user ``call:breakout_assigned`` events
 *    so each client knows its group; signaling rebuilds the mesh
 *    inside the group room.
 * 5. "إغلاق" emits ``v2_call_breakout_close`` and everyone returns
 *    to the main mesh.
 *
 * Non-hosts see a static badge labelling their assigned group.
 */

import React, { useEffect, useMemo, useState } from 'react';
import { Users, X, Plus } from 'lucide-react';
import { socketManager } from '@/services/socket.manager';
import { useCallStore } from '@/stores/call.store.v2';
import { useAuthStore } from '@/stores/auth.store';

interface BreakoutGroup {
  id: string;
  name: string;
  members: string[];
}

const BreakoutPanel: React.FC = () => {
  const callId = useCallStore((s) => s.callId);
  const status = useCallStore((s) => s.status);
  const hostId = useCallStore((s) => s.hostId);
  const participants = useCallStore((s) => s.participants);
  const me = useAuthStore((s) => s.user);
  const isHost = !!me && hostId === me.id;

  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(false);
  const [groups, setGroups] = useState<BreakoutGroup[]>([]);
  const [myGroupId, setMyGroupId] = useState<string | null>(null);

  // Subscribe to breakout_state + per-user assignments.
  useEffect(() => {
    if (!callId) return;
    const offState = socketManager.on('call:breakout_state', (data: any) => {
      if (data?.call_id !== callId) return;
      setActive(!!data.open);
      setGroups(Array.isArray(data.groups) ? data.groups : []);
    });
    const offAssign = socketManager.on('call:breakout_assigned', (data: any) => {
      if (data?.call_id !== callId) return;
      setMyGroupId(data.group_id || null);
    });
    return () => {
      try { offState(); } catch { /* ignore */ }
      try { offAssign(); } catch { /* ignore */ }
    };
  }, [callId]);

  useEffect(() => {
    if (status !== 'active' && status !== 'reconnecting') {
      setActive(false);
      setGroups([]);
      setMyGroupId(null);
      setOpen(false);
    }
  }, [status]);

  const others = useMemo(() => {
    if (!me) return [];
    return Object.values(participants)
      .filter((p: any) => p.peerId !== me.id)
      .map((p: any) => ({ id: p.peerId, name: p.displayName || p.peerId }));
  }, [participants, me]);

  // Local draft state for the host's assignment UI.
  const [draft, setDraft] = useState<BreakoutGroup[]>([
    { id: 'g1', name: 'مجموعة 1', members: [] },
    { id: 'g2', name: 'مجموعة 2', members: [] },
  ]);

  const addGroup = () => {
    const idx = draft.length + 1;
    setDraft((d) => [...d, {
      id: `g${idx}`,
      name: `مجموعة ${idx}`,
      members: [],
    }]);
  };

  const renameGroup = (id: string, name: string) => {
    setDraft((d) => d.map((g) => g.id === id ? { ...g, name } : g));
  };

  const toggleMember = (groupId: string, userId: string) => {
    setDraft((d) => d.map((g) => {
      if (g.id !== groupId) {
        // Remove from any other group first — single-membership.
        return { ...g, members: g.members.filter((m) => m !== userId) };
      }
      const has = g.members.includes(userId);
      return {
        ...g,
        members: has ? g.members.filter((m) => m !== userId) : [...g.members, userId],
      };
    }));
  };

  const launch = () => {
    if (!callId) return;
    socketManager.emitNoAck('v2_call_breakout_open', {
      call_id: callId,
      groups: draft.filter((g) => g.members.length > 0),
    });
  };

  const close = () => {
    if (!callId) return;
    socketManager.emitNoAck('v2_call_breakout_close', { call_id: callId });
  };

  if (!callId) return null;
  if (status !== 'active' && status !== 'reconnecting') return null;

  // Non-host with assignment: show a small pinned badge.
  if (!isHost) {
    if (!active || !myGroupId) return null;
    const myGroup = groups.find((g) => g.id === myGroupId);
    return (
      <div className="fixed bottom-44 left-4 z-30 px-3 py-1.5 rounded-full
                      bg-purple-600/90 text-white text-xs font-medium shadow-lg
                      flex items-center gap-1">
        <Users size={12} />
        <span>أنت في: {myGroup?.name || myGroupId}</span>
      </div>
    );
  }

  return (
    <>
      <button
        onClick={() => setOpen((v) => !v)}
        className={`fixed bottom-56 right-4 z-30 flex items-center gap-1
                    px-3 py-1.5 rounded-full text-xs font-medium shadow-lg
                    transition-colors ${
          active
            ? 'bg-purple-600 text-white'
            : 'bg-black/60 text-white/90 hover:bg-black/80'
        }`}
        title="غرف فرعية"
      >
        <Users size={14} />
        <span>{active ? 'غرف فرعية مفعّلة' : 'غرف فرعية'}</span>
      </button>

      {open && (
        <div className="fixed top-16 right-4 bottom-56 z-30 w-96
                        bg-surface-900/95 border border-surface-700
                        rounded-lg shadow-2xl backdrop-blur
                        flex flex-col overflow-hidden">
          <div className="px-3 py-2 border-b border-surface-700 flex items-center gap-2">
            <Users size={14} className="text-purple-400" />
            <span className="flex-1 text-sm font-semibold">إدارة الغرف الفرعية</span>
            <button
              onClick={() => setOpen(false)}
              className="text-text-400 hover:text-text-100"
            >
              <X size={14} />
            </button>
          </div>

          <div className="flex-1 overflow-y-auto p-3 space-y-3">
            {draft.map((g) => (
              <div key={g.id} className="border border-surface-700 rounded-lg p-2 bg-surface-800/50">
                <div className="flex items-center gap-2 mb-2">
                  <input
                    value={g.name}
                    onChange={(e) => renameGroup(g.id, e.target.value)}
                    className="flex-1 bg-transparent text-sm font-medium text-text-100 outline-none"
                  />
                  <span className="text-[10px] text-text-500">
                    {g.members.length} عضو
                  </span>
                </div>
                <div className="grid grid-cols-2 gap-1">
                  {others.map((p) => {
                    const checked = g.members.includes(p.id);
                    return (
                      <label
                        key={p.id}
                        className={`text-xs rounded px-2 py-1 cursor-pointer ${
                          checked
                            ? 'bg-purple-600/30 text-purple-100'
                            : 'bg-surface-800 text-text-300 hover:bg-surface-700'
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleMember(g.id, p.id)}
                          className="me-1"
                        />
                        {p.name}
                      </label>
                    );
                  })}
                </div>
              </div>
            ))}
            <button
              onClick={addGroup}
              className="w-full px-3 py-2 rounded bg-surface-800
                         hover:bg-surface-700 text-text-200 text-xs
                         flex items-center justify-center gap-1"
            >
              <Plus size={12} /> أضف مجموعة
            </button>
          </div>

          <div className="px-3 py-2 border-t border-surface-700 flex gap-2">
            {!active ? (
              <button
                onClick={launch}
                disabled={draft.every((g) => g.members.length === 0)}
                className="flex-1 px-3 py-1.5 rounded bg-purple-600
                           hover:bg-purple-500 text-white text-xs disabled:opacity-40"
              >
                تشغيل الغرف الفرعية
              </button>
            ) : (
              <button
                onClick={close}
                className="flex-1 px-3 py-1.5 rounded bg-red-600
                           hover:bg-red-500 text-white text-xs"
              >
                إغلاق وعودة للجلسة الرئيسية
              </button>
            )}
          </div>
        </div>
      )}
    </>
  );
};

export default BreakoutPanel;
