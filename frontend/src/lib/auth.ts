/** Who is logged in, and what the UI may offer.
 *  role 'public' = not logged in: only tables flagged valid, read only.
 *  Elements with data-need="editor" / data-need="admin" are hidden by CSS (global.css) unless <html data-role> allows them.
 *  The last known role is kept in localStorage so the first paint already matches (no flash of edit buttons). */

export type Role = 'public' | 'editor' | 'admin';

export interface User {
  id: string; username: string | null; email: string | null; name: string; role: 'editor' | 'admin';
  active: boolean; has_password: boolean; google_linked: boolean; created_at: number; last_login_at: number | null;
  login_method?: 'password' | 'google';
}

export interface Me { user: User | null; google: boolean }

const KEY = 'tdp-role';
const RANK: Record<Role, number> = { public: 0, editor: 1, admin: 2 };

export const role = (): Role => (document.documentElement.dataset.role as Role) || 'public';
export const can = (need: 'editor' | 'admin') => RANK[role()] >= RANK[need];
export const canEdit = () => can('editor');

function setRole(r: Role) {
  const changed = role() !== r;
  document.documentElement.dataset.role = r;
  localStorage.setItem(KEY, r);
  if (changed) dispatchEvent(new CustomEvent('tdp-role', { detail: r }));
}

let current: Promise<Me> | null = null;

/** the logged-in user (fetched once per page) */
export function me(refresh = false): Promise<Me> {
  if (!current || refresh) {
    current = fetch('/api/auth/me', { credentials: 'same-origin' })
      .then((r) => (r.ok ? r.json() : { user: null, google: false }))
      .catch(() => ({ user: null, google: false, offline: true }))
      .then((m: Me & { offline?: boolean }) => {
        if (!m.offline) setRole(m.user ? m.user.role : 'public');
        return m;
      });
  }
  return current;
}

/** the session ended (401 from the server): show the page as a visitor */
export function sessionLost() {
  if (role() !== 'public') { setRole('public'); current = null; }
}

async function post<T>(url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method: 'POST', credentials: 'same-origin',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const j = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof j.detail === 'string' ? j.detail : j.detail?.map?.((d: any) => d.msg).join('; ') ?? `HTTP ${res.status}`);
  return j as T;
}

export async function login(username: string, password: string): Promise<User> {
  const r = await post<{ user: User }>('/api/auth/login', { username, password });
  setRole(r.user.role);
  current = null;
  return r.user;
}

export async function logout() {
  try { await post('/api/auth/logout'); } finally { setRole('public'); current = null; }
}

export const changePassword = (current: string, next: string) => post<{ ok: true }>('/api/auth/password', { current, new: next });

export const googleUrl = (next = location.pathname + location.search) => `/api/auth/google/start?next=${encodeURIComponent(next)}`;
export const loginUrl = (next = location.pathname + location.search) => `/login?next=${encodeURIComponent(next)}`;

/** inline script for <head>: set the remembered role before the first paint */
export const EARLY = `document.documentElement.dataset.role=localStorage.getItem('${KEY}')||'public';`
  + `try{var t=JSON.parse(localStorage.getItem('tdp-settings')||'{}').theme;if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch(e){}`;
