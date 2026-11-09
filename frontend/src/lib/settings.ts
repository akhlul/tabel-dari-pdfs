// Local settings: stored in this browser only (localStorage), never sent to the server.
import { api } from './api';

export interface Settings {
  /** Search BPS: 'auto' = detect the province from where you are, else a 4-digit BPS domain */
  region: string;
  /** use the browser's location (asks permission once) before falling back to the internet address */
  useBrowserLocation: boolean;
  /** Search BPS: reopen with the last kind / region / year / month / language (never the keyword) */
  rememberSearch: boolean;
  searchLang: 'ind' | 'eng';
  /** Library */
  libraryView: 'grid' | 'list';
  findTablesOnUpload: boolean;
  /** Export */
  decimal: 'auto' | ',' | '.';
  exportTitle: boolean;
}

export const DEFAULTS: Settings = {
  region: 'auto', useBrowserLocation: true, rememberSearch: true, searchLang: 'ind',
  libraryView: 'grid', findTablesOnUpload: true, decimal: 'auto', exportTitle: false,
};

const KEY = 'tdp-settings';

export function getSettings(): Settings {
  try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) || '{}') }; } catch { return { ...DEFAULTS }; }
}

export function saveSettings(patch: Partial<Settings>): Settings {
  const s = { ...getSettings(), ...patch };
  localStorage.setItem(KEY, JSON.stringify(s));
  return s;
}

/** every key this app keeps in localStorage (for "Reset everything") */
export const LOCAL_KEYS = [KEY, 'tdp-search', 'tdp-located', 'lib-view', 'lib-open', 'lib-year'];

export function resetLocal() {
  for (const k of LOCAL_KEYS) localStorage.removeItem(k);
}

// ---------------------------------------------------------------- last search filters (no keyword)
export interface SearchFilters { kind?: string; domain?: string; year?: string; month?: string; lang?: string }

export function lastSearch(): SearchFilters | null {
  if (!getSettings().rememberSearch) return null;
  try { return JSON.parse(localStorage.getItem('tdp-search') || 'null'); } catch { return null; }
}

export function rememberSearch(f: SearchFilters) {
  if (getSettings().rememberSearch) localStorage.setItem('tdp-search', JSON.stringify(f));
}

// ---------------------------------------------------------------- where am I -> BPS domain
export interface Located { domain: string | null; region: string; country: string; source: 'location' | 'ip'; at: number }

function browserCoords(): Promise<GeolocationCoordinates | null> {
  if (!('geolocation' in navigator) || !isSecureContext) return Promise.resolve(null);
  return new Promise((resolve) => navigator.geolocation.getCurrentPosition(
    (p) => resolve(p.coords), () => resolve(null), { timeout: 8000, maximumAge: 24 * 3600e3, enableHighAccuracy: false }));
}

/** the province of the user, cached for a day; null when it can't be found (or is outside Indonesia) */
export async function locate(force = false): Promise<Located | null> {
  if (!force) {
    try {
      const c = JSON.parse(localStorage.getItem('tdp-located') || 'null') as Located | null;
      if (c && Date.now() - c.at < 24 * 3600e3) return c;
    } catch { /* ignore */ }
  }
  const s = getSettings();
  let r: Omit<Located, 'at'> | null = null;
  const coords = s.useBrowserLocation ? await browserCoords() : null;
  try { r = await api.locate(coords ? { lat: coords.latitude, lon: coords.longitude } : {}); } catch { /* offline */ }
  if (!r && coords) { try { r = await api.locate({}); } catch { /* offline */ } }
  if (!r) return null;
  const out = { ...r, at: Date.now() };
  localStorage.setItem('tdp-located', JSON.stringify(out));
  return out;
}

/** default region for Search BPS: the fixed setting, or the detected province, or Pusat */
export async function defaultRegion(): Promise<string> {
  const s = getSettings();
  if (s.region !== 'auto') return s.region;
  return (await locate())?.domain ?? '0000';
}
