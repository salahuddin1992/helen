/**
 * QAPanel — combined Q&A + polls side panel for webinars.
 *
 * Toggleable side drawer with two tabs:
 *   - "Questions" — audience asks, host marks answered/dismissed,
 *     participants can upvote to help the host triage.
 *   - "Polls" — host creates a poll; everyone sees live tallies.
 *
 * The state lives entirely in this component (mirrored from socket
 * events) — no need to pollute the call store with per-question
 * objects. When the call ends, the component unmounts and resets.
 */

import React, { useEffect, useState, useCallback } from 'react';
import { ChevronUp, Send, X, MessageSquare, Check, Trash2 } from 'lucide-react';

/** Bar-chart icon for the polls tab. Inline because the bare
 *  `BarChart3` symbol isn't re-exported in our pinned lucide-react. */
const BarChart3Svg: React.FC<{ size?: number }> = ({ size = 14 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2"
       strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 3v18h18" />
    <path d="M7 16V8" />
    <path d="M12 16v-5" />
    <path d="M17 16v-3" />
  </svg>
);
import { socketManager } from '@/services/socket.manager';
import { useCallStore } from '@/stores/call.store.v2';
import { useAuthStore } from '@/stores/auth.store';

interface Question {
  id: string;
  userId: string;
  text: string;
  ts: number;
  status: 'open' | 'answered' | 'dismissed';
  votes: Record<string, number>; // user_id → ±1
}

interface Poll {
  id: string;
  question: string;
  options: string[];
  votes: Record<string, number>; // user_id → option index
  closed: boolean;
  ts: number;
}

const QAPanel: React.FC = () => {
  const callId = useCallStore((s) => s.callId);
  const status = useCallStore((s) => s.status);
  const hostId = useCallStore((s) => s.hostId);
  const me = useAuthStore((s) => s.user);
  const isHost = !!me && hostId === me.id;

  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<'qa' | 'polls'>('qa');
  const [questions, setQuestions] = useState<Question[]>([]);
  const [polls, setPolls] = useState<Poll[]>([]);
  const [draftQ, setDraftQ] = useState('');
  const [pollDraft, setPollDraft] = useState({ q: '', options: ['', ''] });

  // Subscribe to Q&A + poll socket events.
  useEffect(() => {
    if (!callId) return;
    const offs: Array<() => void> = [];

    offs.push(socketManager.on('call:qa_added', (data: any) => {
      if (data?.call_id !== callId) return;
      setQuestions((prev) => {
        if (prev.some((q) => q.id === data.id)) return prev;
        return [...prev, {
          id: data.id, userId: data.user_id, text: data.text,
          ts: data.ts, status: data.status || 'open', votes: {},
        }];
      });
    }));
    offs.push(socketManager.on('call:qa_status', (data: any) => {
      if (data?.call_id !== callId) return;
      setQuestions((prev) => prev.map(
        (q) => q.id === data.id ? { ...q, status: data.status } : q,
      ));
    }));
    offs.push(socketManager.on('call:qa_vote', (data: any) => {
      if (data?.call_id !== callId) return;
      setQuestions((prev) => prev.map((q) => {
        if (q.id !== data.id) return q;
        const votes = { ...q.votes };
        if (data.delta > 0) votes[data.user_id] = 1;
        else delete votes[data.user_id];
        return { ...q, votes };
      }));
    }));
    offs.push(socketManager.on('call:poll_started', (data: any) => {
      if (data?.call_id !== callId) return;
      setPolls((prev) => {
        if (prev.some((p) => p.id === data.id)) return prev;
        return [...prev, {
          id: data.id, question: data.question, options: data.options,
          votes: {}, closed: false, ts: data.ts,
        }];
      });
    }));
    offs.push(socketManager.on('call:poll_vote', (data: any) => {
      if (data?.call_id !== callId) return;
      setPolls((prev) => prev.map((p) => {
        if (p.id !== data.id) return p;
        return { ...p, votes: { ...p.votes, [data.user_id]: data.choice } };
      }));
    }));
    offs.push(socketManager.on('call:poll_closed', (data: any) => {
      if (data?.call_id !== callId) return;
      setPolls((prev) => prev.map(
        (p) => p.id === data.id ? { ...p, closed: true } : p,
      ));
    }));

    return () => { for (const f of offs) { try { f(); } catch { /* */ } } };
  }, [callId]);

  // Reset when the call ends.
  useEffect(() => {
    if (status !== 'active' && status !== 'reconnecting') {
      setQuestions([]);
      setPolls([]);
      setOpen(false);
    }
  }, [status]);

  const submitQuestion = useCallback(() => {
    const text = draftQ.trim();
    if (!text || !callId) return;
    socketManager.emitNoAck('v2_call_qa_ask', { call_id: callId, text });
    setDraftQ('');
  }, [draftQ, callId]);

  const resolveQuestion = (id: string, status: Question['status']) => {
    if (!callId) return;
    socketManager.emitNoAck('v2_call_qa_resolve', { call_id: callId, id, status });
  };

  const upvote = (id: string, up: boolean) => {
    if (!callId) return;
    socketManager.emitNoAck('v2_call_qa_upvote', { call_id: callId, id, up });
  };

  const launchPoll = () => {
    if (!callId) return;
    const opts = pollDraft.options.map((o) => o.trim()).filter(Boolean);
    if (!pollDraft.q.trim() || opts.length < 2) return;
    socketManager.emitNoAck('v2_call_poll_create', {
      call_id: callId,
      question: pollDraft.q.trim(),
      options: opts,
    });
    setPollDraft({ q: '', options: ['', ''] });
  };

  const votePoll = (pollId: string, choice: number) => {
    if (!callId) return;
    socketManager.emitNoAck('v2_call_poll_vote', { call_id: callId, id: pollId, choice });
  };

  const closePoll = (pollId: string) => {
    if (!callId) return;
    socketManager.emitNoAck('v2_call_poll_close', { call_id: callId, id: pollId });
  };

  if (!callId) return null;
  if (status !== 'active' && status !== 'reconnecting') return null;

  // Sort questions: open + most-voted first; dismissed last.
  const sortedQuestions = [...questions].sort((a, b) => {
    const order = { open: 0, answered: 1, dismissed: 2 };
    if (order[a.status] !== order[b.status]) return order[a.status] - order[b.status];
    const av = Object.values(a.votes).reduce((s, v) => s + v, 0);
    const bv = Object.values(b.votes).reduce((s, v) => s + v, 0);
    return bv - av;
  });

  return (
    <>
      {/* Side toggle button */}
      <button
        onClick={() => setOpen((v) => !v)}
        className={`fixed bottom-32 right-4 z-30 flex items-center gap-1 px-3 py-1.5
                    rounded-full text-xs font-medium shadow-lg transition-colors ${
          open ? 'bg-blue-600 text-white' : 'bg-black/60 text-white/90 hover:bg-black/80'
        }`}
        title="Q&A + Polls"
      >
        <MessageSquare size={14} />
        <span>{questions.filter((q) => q.status === 'open').length || ''}</span>
      </button>

      {open && (
        <div className="fixed top-16 right-4 bottom-32 z-30 w-80
                        bg-surface-900/95 border border-surface-700
                        rounded-lg shadow-2xl backdrop-blur
                        flex flex-col overflow-hidden">
          <div className="flex border-b border-surface-700 text-sm">
            <button
              onClick={() => setTab('qa')}
              className={`flex-1 py-2 flex items-center justify-center gap-1
                          ${tab === 'qa' ? 'bg-surface-800 text-text-100' : 'text-text-400'}`}
            >
              <MessageSquare size={14} /> أسئلة
            </button>
            <button
              onClick={() => setTab('polls')}
              className={`flex-1 py-2 flex items-center justify-center gap-1
                          ${tab === 'polls' ? 'bg-surface-800 text-text-100' : 'text-text-400'}`}
            >
              <BarChart3Svg size={14} /> استطلاع
            </button>
            <button
              onClick={() => setOpen(false)}
              className="px-3 text-text-400 hover:text-text-100"
            >
              <X size={14} />
            </button>
          </div>

          {tab === 'qa' ? (
            <>
              <div className="flex-1 overflow-y-auto divide-y divide-surface-800">
                {sortedQuestions.length === 0 ? (
                  <div className="p-4 text-xs text-text-500 text-center">
                    لا أسئلة بعد
                  </div>
                ) : (
                  sortedQuestions.map((q) => {
                    const totalVotes = Object.values(q.votes).reduce((s, v) => s + v, 0);
                    const myVote = me ? q.votes[me.id] : 0;
                    return (
                      <div key={q.id} className={`p-3 ${
                        q.status === 'dismissed' ? 'opacity-50' :
                        q.status === 'answered' ? 'bg-green-500/10' : ''
                      }`}>
                        <div className="flex gap-2">
                          <div className="flex flex-col items-center text-text-400">
                            <button
                              onClick={() => upvote(q.id, !myVote)}
                              className={`p-0.5 ${myVote ? 'text-blue-400' : 'hover:text-text-200'}`}
                              title="Upvote"
                            >
                              <ChevronUp size={16} />
                            </button>
                            <span className="text-xs tabular-nums">{totalVotes}</span>
                          </div>
                          <div className="flex-1 text-sm text-text-100">{q.text}</div>
                        </div>
                        {isHost && q.status === 'open' && (
                          <div className="flex gap-2 mt-2">
                            <button
                              onClick={() => resolveQuestion(q.id, 'answered')}
                              className="text-[10px] px-2 py-0.5 rounded bg-green-600 hover:bg-green-500 text-white flex items-center gap-1"
                            >
                              <Check size={10} /> أُجيب
                            </button>
                            <button
                              onClick={() => resolveQuestion(q.id, 'dismissed')}
                              className="text-[10px] px-2 py-0.5 rounded bg-surface-700 hover:bg-surface-600 text-text-200 flex items-center gap-1"
                            >
                              <Trash2 size={10} /> تجاهل
                            </button>
                          </div>
                        )}
                      </div>
                    );
                  })
                )}
              </div>
              <div className="p-2 border-t border-surface-700 flex gap-1">
                <input
                  value={draftQ}
                  onChange={(e) => setDraftQ(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && submitQuestion()}
                  placeholder="اطرح سؤال..."
                  maxLength={800}
                  className="flex-1 bg-surface-800 border border-surface-700
                             rounded px-2 py-1 text-sm text-text-100 outline-none"
                />
                <button
                  onClick={submitQuestion}
                  disabled={!draftQ.trim()}
                  className="px-3 py-1 rounded bg-blue-600 hover:bg-blue-500 text-white
                             text-sm disabled:opacity-40"
                >
                  <Send size={14} />
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="flex-1 overflow-y-auto divide-y divide-surface-800">
                {polls.length === 0 ? (
                  <div className="p-4 text-xs text-text-500 text-center">
                    لا استطلاعات حالياً
                  </div>
                ) : (
                  polls.slice().reverse().map((p) => {
                    const totals = p.options.map((_, i) =>
                      Object.values(p.votes).filter((v) => v === i).length,
                    );
                    const totalVotes = totals.reduce((s, n) => s + n, 0) || 1;
                    const myVote = me ? p.votes[me.id] : undefined;
                    return (
                      <div key={p.id} className="p-3 space-y-1">
                        <div className="text-sm font-medium text-text-100">{p.question}</div>
                        <ul className="space-y-1 mt-1">
                          {p.options.map((opt, i) => {
                            const pct = Math.round((totals[i] / totalVotes) * 100);
                            const selected = myVote === i;
                            return (
                              <li key={i}>
                                <button
                                  onClick={() => !p.closed && votePoll(p.id, i)}
                                  disabled={p.closed}
                                  className={`w-full text-start relative rounded p-2 text-xs
                                              border ${
                                    selected
                                      ? 'border-blue-500 bg-blue-500/10'
                                      : 'border-surface-700 hover:bg-surface-800'
                                  } ${p.closed ? 'cursor-default' : 'cursor-pointer'}`}
                                >
                                  <div
                                    className="absolute inset-0 bg-blue-500/10 rounded"
                                    style={{ width: `${pct}%` }}
                                  />
                                  <div className="relative flex justify-between">
                                    <span>{opt}</span>
                                    <span className="tabular-nums">{pct}% · {totals[i]}</span>
                                  </div>
                                </button>
                              </li>
                            );
                          })}
                        </ul>
                        {isHost && !p.closed && (
                          <button
                            onClick={() => closePoll(p.id)}
                            className="text-[10px] px-2 py-0.5 rounded bg-surface-700 hover:bg-surface-600 text-text-200"
                          >
                            إغلاق التصويت
                          </button>
                        )}
                        {p.closed && (
                          <div className="text-[10px] text-text-500">منتهٍ</div>
                        )}
                      </div>
                    );
                  })
                )}
              </div>
              {isHost && (
                <div className="p-2 border-t border-surface-700 space-y-1">
                  <input
                    value={pollDraft.q}
                    onChange={(e) => setPollDraft((d) => ({ ...d, q: e.target.value }))}
                    placeholder="السؤال..."
                    maxLength={400}
                    className="w-full bg-surface-800 border border-surface-700
                               rounded px-2 py-1 text-sm text-text-100 outline-none"
                  />
                  {pollDraft.options.map((o, i) => (
                    <div key={i} className="flex gap-1">
                      <input
                        value={o}
                        onChange={(e) => setPollDraft((d) => ({
                          ...d,
                          options: d.options.map((x, j) => j === i ? e.target.value : x),
                        }))}
                        placeholder={`خيار ${i + 1}`}
                        maxLength={120}
                        className="flex-1 bg-surface-800 border border-surface-700
                                   rounded px-2 py-1 text-xs text-text-100 outline-none"
                      />
                      {pollDraft.options.length > 2 && (
                        <button
                          onClick={() => setPollDraft((d) => ({
                            ...d, options: d.options.filter((_, j) => j !== i),
                          }))}
                          className="px-2 text-text-400 hover:text-red-400"
                        >
                          <X size={12} />
                        </button>
                      )}
                    </div>
                  ))}
                  <div className="flex gap-1">
                    {pollDraft.options.length < 8 && (
                      <button
                        onClick={() => setPollDraft((d) => ({
                          ...d, options: [...d.options, ''],
                        }))}
                        className="flex-1 px-2 py-1 rounded bg-surface-700
                                   hover:bg-surface-600 text-text-200 text-xs"
                      >
                        + خيار
                      </button>
                    )}
                    <button
                      onClick={launchPoll}
                      disabled={!pollDraft.q.trim() || pollDraft.options.filter((o) => o.trim()).length < 2}
                      className="flex-1 px-2 py-1 rounded bg-blue-600 hover:bg-blue-500
                                 text-white text-xs disabled:opacity-40"
                    >
                      إطلاق
                    </button>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </>
  );
};

export default QAPanel;
