import { create } from 'zustand';
import type { Contact, User, UserStatus } from '../types';
import { api } from '../services/api.client';
import { socketManager } from '../services/socket.manager';

interface ContactsState {
  contacts: Contact[];
  allUsers: User[];
  onlineUsers: Record<string, UserStatus>;
  isLoading: boolean;

  loadContacts: () => Promise<void>;
  loadUsers: (search?: string) => Promise<void>;
  addContact: (userId: string) => Promise<void>;
  removeContact: (contactId: string) => Promise<void>;
  getUserStatus: (userId: string) => UserStatus;
  setupPresenceListeners: () => () => void;
}

export const useContactsStore = create<ContactsState>((set, get) => ({
  contacts: [],
  allUsers: [],
  onlineUsers: {},
  isLoading: false,

  loadContacts: async () => {
    set({ isLoading: true });
    try {
      const contacts = await api.listContacts();
      set({ contacts, isLoading: false });
    } catch { set({ isLoading: false }); }
  },

  loadUsers: async (search?: string) => {
    try {
      const data = await api.listUsers({ search, limit: 200 });
      set({ allUsers: data.users });
    } catch {}
  },

  addContact: async (userId) => {
    await api.addContact({ contact_id: userId });
    await get().loadContacts();
  },

  removeContact: async (contactId) => {
    await api.removeContact(contactId);
    set((s) => ({ contacts: s.contacts.filter((c) => c.contact.id !== contactId) }));
  },

  getUserStatus: (userId) => get().onlineUsers[userId] || 'offline',

  setupPresenceListeners: () => {
    const unsub1 = socketManager.on('presence:online_list', (data: { online_users: Record<string, string> }) => {
      set({ onlineUsers: data.online_users as Record<string, UserStatus> });
    });
    const unsub2 = socketManager.on('presence:user_online', (data: { user_id: string; status: string; online_users: Record<string, string> }) => {
      set({ onlineUsers: data.online_users as Record<string, UserStatus> });
    });
    const unsub3 = socketManager.on('presence:user_offline', (data: { user_id: string }) => {
      set((s) => {
        const copy = { ...s.onlineUsers };
        delete copy[data.user_id];
        return { onlineUsers: copy };
      });
    });
    const unsub4 = socketManager.on('presence:user_status', (data: { user_id: string; status: string }) => {
      set((s) => ({ onlineUsers: { ...s.onlineUsers, [data.user_id]: data.status as UserStatus } }));
    });
    return () => { unsub1(); unsub2(); unsub3(); unsub4(); };
  },
}));
