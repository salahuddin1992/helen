/**
 * CalendarPage — internal events + reminders + ICS feed.
 * Backend: /api/calendar/* (see app/api/routes/calendar.py).
 *
 * Reminders fire as Socket.IO `calendar:reminder` events; the UI
 * displays them via the existing notification system. This page
 * focuses on event CRUD + per-day list view.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Plus, ExternalLink, Trash2, Edit2, RefreshCw, X } from 'lucide-react';
import { api } from '@/services/api.client';
import { AttendeePicker } from '@/components/calendar/AttendeePicker';
import { RecurrencePicker } from '@/components/calendar/RecurrencePicker';

interface CalendarEvent {
    event_id: string;
    creator_id: string;
    title: string;
    start_at: number;
    end_at: number;
    description: string;
    location: string;
    channel_id: string | null;
    attendees: string[];
    recurrence: string | null;
    reminders: number[];
    created_at: number;
    cancelled: boolean;
}

const fmtTime = (epoch: number) => {
    try { return new Date(epoch * 1000).toLocaleString(); }
    catch { return ''; }
};

const fmtDay = (epoch: number) => {
    try { return new Date(epoch * 1000).toLocaleDateString(); }
    catch { return ''; }
};

const initialFormState = () => {
    const start = Math.floor(Date.now() / 1000) + 600;
    return {
        title: '',
        start_at: start,
        end_at: start + 3600,
        description: '',
        location: '',
        attendees: '',
        reminders: '5,30',
        // RFC 5545 RRULE — populated by RecurrencePicker; null = single
        // event. The server's calendar_service stores it as-is.
        recurrence: null as string | null,
    };
};

const epochToInputValue = (epoch: number) => {
    const d = new Date(epoch * 1000);
    const pad = (n: number) => String(n).padStart(2, '0');
    return (
        `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T` +
        `${pad(d.getHours())}:${pad(d.getMinutes())}`
    );
};

const inputValueToEpoch = (val: string): number => {
    const t = Date.parse(val);
    return isNaN(t) ? 0 : Math.floor(t / 1000);
};

export const CalendarPage: React.FC = () => {
    const [events, setEvents] = useState<CalendarEvent[]>([]);
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState<string | null>(null);
    const [showForm, setShowForm] = useState(false);
    const [editing, setEditing] = useState<CalendarEvent | null>(null);
    const [form, setForm] = useState(initialFormState());

    const load = useCallback(async () => {
        setBusy(true);
        try {
            const r = await api.calendar.list({ limit: 200 });
            setEvents(r.events || []);
            setErr(null);
        } catch (e: any) {
            setErr(e?.message || 'Failed to load events');
        } finally {
            setBusy(false);
        }
    }, []);

    useEffect(() => { load(); }, [load]);

    // Listen for live reminders via Socket.IO; fold them into the
    // OS notification surface. The actual socket subscription lives
    // in services/socket.manager.ts; here we just listen for the
    // already-relayed CustomEvent.
    useEffect(() => {
        const onReminder = (ev: any) => {
            const d = ev.detail || {};
            const title = d.title || 'Reminder';
            const mins = d.starts_in_minutes ?? '?';
            const body = `Starts in ${mins} min`;
            try {
                if ('Notification' in window && Notification.permission === 'granted') {
                    new Notification(title, { body });
                }
            } catch { /* ignore */ }
        };
        window.addEventListener('calendar:reminder' as any, onReminder);
        return () => window.removeEventListener('calendar:reminder' as any, onReminder);
    }, []);

    const grouped = useMemo(() => {
        const out: { day: string; items: CalendarEvent[] }[] = [];
        const sorted = [...events].sort((a, b) => a.start_at - b.start_at);
        for (const ev of sorted) {
            const day = fmtDay(ev.start_at);
            const last = out[out.length - 1];
            if (last && last.day === day) last.items.push(ev);
            else out.push({ day, items: [ev] });
        }
        return out;
    }, [events]);

    const openCreate = () => {
        setForm(initialFormState());
        setEditing(null);
        setShowForm(true);
    };

    const openEdit = (ev: CalendarEvent) => {
        setEditing(ev);
        setForm({
            title: ev.title,
            start_at: ev.start_at,
            end_at: ev.end_at,
            description: ev.description,
            location: ev.location,
            attendees: ev.attendees.join(','),
            reminders: ev.reminders.join(','),
            recurrence: ev.recurrence ?? null,
        });
        setShowForm(true);
    };

    const submit = async () => {
        const body = {
            title: form.title.trim(),
            start_at: form.start_at,
            end_at: form.end_at,
            description: form.description.trim(),
            location: form.location.trim(),
            attendees: form.attendees
                .split(',').map(s => s.trim()).filter(Boolean),
            reminders: form.reminders
                .split(',').map(s => parseInt(s.trim(), 10))
                .filter(n => Number.isFinite(n) && n > 0),
            recurrence: form.recurrence,
        };
        if (!body.title) {
            setErr('Title is required');
            return;
        }
        if (body.end_at <= body.start_at) {
            setErr('End must be after start');
            return;
        }
        try {
            if (editing) {
                await api.calendar.update(editing.event_id, body);
            } else {
                await api.calendar.create(body);
            }
            setShowForm(false);
            setErr(null);
            await load();
        } catch (e: any) {
            setErr(e?.message || 'Save failed');
        }
    };

    const remove = async (ev: CalendarEvent) => {
        if (!window.confirm(`Cancel "${ev.title}"?`)) return;
        try {
            await api.calendar.cancel(ev.event_id);
            await load();
        } catch (e: any) {
            setErr(e?.message);
        }
    };

    return (
        <div className="page calendar-page" style={{ padding: 16 }}>
            <header style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <h1 style={{ margin: 0, flex: 1 }}>Calendar</h1>
                <button onClick={load} disabled={busy} className="btn btn-ghost" title="Refresh">
                    <RefreshCw size={16} />
                </button>
                <a
                    href={api.calendar.icsFeedUrl()}
                    target="_blank"
                    rel="noreferrer"
                    className="btn btn-ghost"
                    title="iCal feed (subscribe in any calendar app)"
                >
                    <ExternalLink size={16} /> ICS feed
                </a>
                <button onClick={openCreate} className="btn btn-primary">
                    <Plus size={16} /> New event
                </button>
            </header>

            {err && (
                <div role="alert" style={{
                    margin: '10px 0', padding: 8, color: '#fff',
                    background: '#a33', borderRadius: 4,
                }}>
                    {err}
                </div>
            )}

            {!busy && events.length === 0 && (
                <p style={{ opacity: 0.7 }}>
                    No events scheduled. Click "New event" to create one.
                </p>
            )}

            {grouped.map(({ day, items }) => (
                <section key={day} style={{ marginTop: 18 }}>
                    <h3 style={{ margin: '8px 0', fontSize: 14, opacity: 0.7 }}>{day}</h3>
                    <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
                        {items.map(ev => (
                            <li key={ev.event_id} style={{
                                padding: 10, marginBottom: 6,
                                background: 'rgba(255,255,255,0.03)',
                                borderRadius: 6,
                            }}>
                                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                                    <div style={{ flex: 1 }}>
                                        <strong>{ev.title}</strong>
                                        <div style={{ fontSize: 12, opacity: 0.7 }}>
                                            {fmtTime(ev.start_at)} → {fmtTime(ev.end_at)}
                                        </div>
                                        {ev.location && (
                                            <div style={{ fontSize: 12, opacity: 0.7 }}>
                                                @ {ev.location}
                                            </div>
                                        )}
                                        {ev.description && (
                                            <div style={{ marginTop: 4 }}>{ev.description}</div>
                                        )}
                                        {ev.attendees.length > 0 && (
                                            <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4 }}>
                                                attendees: {ev.attendees.join(', ')}
                                            </div>
                                        )}
                                    </div>
                                    <button
                                        onClick={() => openEdit(ev)}
                                        className="btn btn-ghost btn-sm"
                                        title="Edit"
                                    >
                                        <Edit2 size={14} />
                                    </button>
                                    <button
                                        onClick={() => remove(ev)}
                                        className="btn btn-ghost btn-sm"
                                        title="Cancel event"
                                    >
                                        <Trash2 size={14} />
                                    </button>
                                </div>
                            </li>
                        ))}
                    </ul>
                </section>
            ))}

            {showForm && (
                <div className="modal-backdrop" style={{
                    position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    zIndex: 1000,
                }}>
                    <div style={{
                        background: '#222', padding: 16, minWidth: 360,
                        borderRadius: 8, color: '#eee',
                    }}>
                        <header style={{ display: 'flex', alignItems: 'center' }}>
                            <h2 style={{ margin: 0, flex: 1, fontSize: 18 }}>
                                {editing ? 'Edit event' : 'New event'}
                            </h2>
                            <button
                                onClick={() => setShowForm(false)}
                                className="btn btn-ghost"
                            >
                                <X size={16} />
                            </button>
                        </header>
                        <div style={{ display: 'grid', gap: 8, marginTop: 10 }}>
                            <label>
                                Title
                                <input
                                    type="text"
                                    value={form.title}
                                    onChange={e => setForm({ ...form, title: e.target.value })}
                                    style={{ width: '100%' }}
                                />
                            </label>
                            <label>
                                Start
                                <input
                                    type="datetime-local"
                                    value={epochToInputValue(form.start_at)}
                                    onChange={e => setForm({
                                        ...form,
                                        start_at: inputValueToEpoch(e.target.value),
                                    })}
                                    style={{ width: '100%' }}
                                />
                            </label>
                            <label>
                                End
                                <input
                                    type="datetime-local"
                                    value={epochToInputValue(form.end_at)}
                                    onChange={e => setForm({
                                        ...form,
                                        end_at: inputValueToEpoch(e.target.value),
                                    })}
                                    style={{ width: '100%' }}
                                />
                            </label>
                            <label>
                                Location (optional)
                                <input
                                    type="text"
                                    value={form.location}
                                    onChange={e => setForm({ ...form, location: e.target.value })}
                                    style={{ width: '100%' }}
                                />
                            </label>
                            <label>
                                Description
                                <textarea
                                    value={form.description}
                                    onChange={e => setForm({ ...form, description: e.target.value })}
                                    rows={3}
                                    style={{ width: '100%' }}
                                />
                            </label>
                            <label>
                                المدعوّون
                                <AttendeePicker
                                    value={
                                        form.attendees
                                            ? form.attendees.split(',')
                                                  .map(s => s.trim())
                                                  .filter(Boolean)
                                            : []
                                    }
                                    onChange={(ids) =>
                                        setForm({ ...form, attendees: ids.join(',') })
                                    }
                                />
                            </label>
                            <label>
                                التكرار
                                <RecurrencePicker
                                    value={form.recurrence}
                                    onChange={(rule) =>
                                        setForm({ ...form, recurrence: rule })
                                    }
                                />
                            </label>
                            <label>
                                Reminders (minutes before, comma-separated)
                                <input
                                    type="text"
                                    value={form.reminders}
                                    onChange={e => setForm({ ...form, reminders: e.target.value })}
                                    style={{ width: '100%' }}
                                    placeholder="5,30"
                                />
                            </label>
                        </div>
                        <footer style={{ marginTop: 12, display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                            <button onClick={() => setShowForm(false)} className="btn btn-ghost">
                                Cancel
                            </button>
                            <button onClick={submit} className="btn btn-primary">
                                {editing ? 'Save' : 'Create'}
                            </button>
                        </footer>
                    </div>
                </div>
            )}
        </div>
    );
};

export default CalendarPage;
