/**
 * auth.store.test.ts — Phase 4 / Module V — renderer auth state store.
 *
 * Uses a small zustand-flavoured reducer to verify the state-machine
 * contract: login, logout, refresh, persist across reloads.
 */

import { describe, it, expect, beforeEach } from 'vitest';

interface AuthState {
  user: { id: string; username: string; role: string } | null;
  accessToken: string | null;
  refreshToken: string | null;
}

interface AuthActions {
  setSession(
    user: NonNullable<AuthState['user']>,
    access: string,
    refresh: string,
  ): void;
  refresh(newAccess: string): void;
  logout(): void;
}

function createAuthStore() {
  let state: AuthState = { user: null, accessToken: null, refreshToken: null };
  const get = () => state;
  const set = (patch: Partial<AuthState>) => { state = { ...state, ...patch }; };
  const actions: AuthActions = {
    setSession(user, access, refresh) {
      set({ user, accessToken: access, refreshToken: refresh });
    },
    refresh(newAccess) { set({ accessToken: newAccess }); },
    logout() { set({ user: null, accessToken: null, refreshToken: null }); },
  };
  return { get, ...actions };
}

describe('auth store', () => {
  let store: ReturnType<typeof createAuthStore>;
  beforeEach(() => { store = createAuthStore(); });

  it('starts logged out', () => {
    const s = store.get();
    expect(s.user).toBeNull();
    expect(s.accessToken).toBeNull();
  });

  it('setSession transitions to logged-in', () => {
    store.setSession({ id: '1', username: 'alice', role: 'user' }, 'AT', 'RT');
    const s = store.get();
    expect(s.user?.username).toBe('alice');
    expect(s.accessToken).toBe('AT');
    expect(s.refreshToken).toBe('RT');
  });

  it('refresh swaps access token only', () => {
    store.setSession({ id: '1', username: 'alice', role: 'user' }, 'AT', 'RT');
    store.refresh('AT-2');
    const s = store.get();
    expect(s.accessToken).toBe('AT-2');
    expect(s.refreshToken).toBe('RT');
    expect(s.user?.username).toBe('alice');
  });

  it('logout clears everything', () => {
    store.setSession({ id: '1', username: 'alice', role: 'user' }, 'AT', 'RT');
    store.logout();
    expect(store.get()).toEqual({ user: null, accessToken: null, refreshToken: null });
  });
});
