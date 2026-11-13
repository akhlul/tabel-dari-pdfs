import { icon } from './icons';

export const $ = <T extends Element = HTMLElement>(sel: string, root: ParentNode = document) =>
  root.querySelector(sel) as T;
export const $$ = <T extends Element = HTMLElement>(sel: string, root: ParentNode = document) =>
  Array.from(root.querySelectorAll(sel)) as T[];

export function esc(s: unknown): string {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!);
}

export function h(html: string): HTMLElement {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild as HTMLElement;
}

// ---------------------------------------------------------------- passive notifications (toasts)
// HIG: toasts are non-modal and dismissable; they carry an action (Undo / Retry) when there is one.
// Don't toast success that is already visible on screen.
type ToastKind = 'info' | 'ok' | 'error';
export interface ToastAction { label: string; run: () => void | Promise<unknown> }
export interface ToastOpts { ms?: number; action?: ToastAction; /** a second action, e.g. View Queue next to Undo */ action2?: ToastAction }

export function toast(msg: string, kind: ToastKind = 'info', opts: number | ToastOpts = {}) {
  const o: ToastOpts = typeof opts === 'number' ? { ms: opts } : opts;
  const ms = o.ms ?? (o.action ? 8000 : kind === 'error' ? 7000 : 3800);
  let host = document.getElementById('toasts');
  if (!host) {
    host = h('<div id="toasts" class="fixed bottom-4 right-4 z-50 flex flex-col gap-lg w-96 max-w-[calc(100vw-2rem)]"></div>');
    document.body.append(host);
  }
  const color = kind === 'error' ? 'border-l-red-600' : kind === 'ok' ? 'border-l-emerald-600' : 'border-l-brand-600';
  const ic = kind === 'error' ? 'x' : kind === 'ok' ? 'check' : 'dot';
  const el = h(`<div role="${kind === 'error' ? 'alert' : 'status'}" class="panel border-l-4 ${color} shadow-lg pl-3 pr-1.5 py-1.5 text-[13px] flex gap-lg items-center
      translate-y-2 opacity-0 transition-all duration-200">
      <span class="${kind === 'error' ? 'text-red-600' : kind === 'ok' ? 'text-emerald-600' : 'text-brand-600'}">${icon(ic, 'size-3.5')}</span>
      <div class="flex-1 text-ink-800 py-1">${esc(msg)}</div>
      ${o.action2 ? `<button data-act2 class="btn-line btn-sm">${esc(o.action2.label)}</button>` : ''}
      ${o.action ? `<button data-act class="btn-line btn-sm">${esc(o.action.label)}</button>` : ''}
      <button data-close class="btn-ghost btn-sm btn-icon" aria-label="Dismiss">${icon('x', 'size-3.5')}</button></div>`);
  host.append(el);
  requestAnimationFrame(() => el.classList.remove('translate-y-2', 'opacity-0'));
  let timer: ReturnType<typeof setTimeout>;
  const close = () => { clearTimeout(timer); el.classList.add('opacity-0'); setTimeout(() => el.remove(), 200); };
  const arm = () => { clearTimeout(timer); timer = setTimeout(close, ms); };
  el.addEventListener('mouseenter', () => clearTimeout(timer)); // pause while pointing at it
  el.addEventListener('mouseleave', arm);
  el.addEventListener('focusin', () => clearTimeout(timer));
  el.querySelector('[data-close]')!.addEventListener('click', close);
  for (const [sel, a] of [['[data-act]', o.action], ['[data-act2]', o.action2]] as const) {
    el.querySelector(sel)?.addEventListener('click', async () => {
      close();
      try { await a!.run(); } catch (e: any) { toast(e?.message ?? String(e), 'error'); }
    });
  }
  arm();
  return close;
}

/** an error with a Retry action (HIG: every error must be actionable) */
export function toastError(e: unknown, retry?: () => unknown) {
  const msg = (e as any)?.message ?? String(e);
  toast(msg, 'error', retry ? { action: { label: 'Retry', run: retry as () => void } } : {});
}

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 ** 2).toFixed(1)} MB`;
}

export function fmtDate(ts: number | string | null | undefined): string {
  if (!ts) return '';
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts);
  if (isNaN(+d)) return String(ts);
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
}

export function ago(ts: number): string {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return fmtDate(ts);
}

export const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** "1-3, 7, 10-12" -> [1,2,3,7,10,11,12]; null for empty/"all" */
export function parsePages(s: string, max: number): number[] | null {
  s = s.trim().toLowerCase();
  if (!s || s === 'all') return null;
  const out = new Set<number>();
  for (const part of s.split(/[,\s]+/).filter(Boolean)) {
    const m = part.match(/^(\d+)(?:[-–](\d+))?$/);
    if (!m) throw new Error(`“${part}” is not a page or range. Use for example 1–5, 9.`);
    const a = +m[1]!, b = m[2] ? +m[2] : a;
    for (let p = Math.min(a, b); p <= Math.max(a, b); p++) if (p >= 1 && p <= max) out.add(p);
  }
  if (!out.size) throw new Error(`No pages between 1 and ${max} in that range.`);
  return [...out].sort((x, y) => x - y);
}

export function debounce<A extends unknown[]>(fn: (...a: A) => void, ms: number) {
  let t: ReturnType<typeof setTimeout> | undefined;
  const d = (...a: A) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  d.flush = (...a: A) => { clearTimeout(t); fn(...a); };
  d.cancel = () => clearTimeout(t);
  return d;
}

/** Only for actions that cannot be undone (HIG: prefer Undo over confirmation). */
export function confirmDialog(title: string, body: string, okLabel: string): Promise<boolean> {
  return new Promise((resolve) => {
    const back = document.activeElement as HTMLElement | null;
    const dlg = h(`<dialog aria-labelledby="dlg-t" class="panel p-0 shadow-2xl backdrop:bg-black/40 w-[440px] max-w-[92vw] m-auto border-ink-300">
      <div class="px-5 pt-4 pb-2"><h3 id="dlg-t" class="font-semibold text-ink-900">${esc(title)}</h3></div>
      <p class="px-5 pb-4 text-[13px] text-ink-600">${esc(body)}</p>
      <div class="flex justify-end gap-lg px-5 py-3 bg-ink-50 border-t border-ink-200">
        <button class="btn-line" value="cancel" autofocus>Cancel</button>
        <button class="btn bg-red-700 border-red-700 text-white hover:bg-red-800" value="ok">${icon('trash', 'size-3.5')}${esc(okLabel)}</button>
      </div></dialog>`) as HTMLDialogElement;
    document.body.append(dlg);
    dlg.addEventListener('click', (e) => {
      const v = (e.target as HTMLElement).closest('button')?.value;
      if (v) dlg.close(v);
      else if (e.target === dlg) dlg.close('cancel');
    });
    dlg.addEventListener('close', () => { resolve(dlg.returnValue === 'ok'); dlg.remove(); back?.focus(); });
    dlg.showModal();
  });
}

// ---------------------------------------------------------------- keyboard navigation inside menus
const ITEM = 'button:not([disabled]), a[href], a.menu-item, [role="menuitem"]:not([disabled]), input:not([disabled]), select';

function menuKeys(menu: HTMLElement, close: () => void) {
  menu.addEventListener('keydown', (e) => {
    const items = $$<HTMLElement>(ITEM, menu).filter((x) => x.offsetParent !== null);
    const i = items.indexOf(document.activeElement as HTMLElement);
    const go = (n: number) => { e.preventDefault(); items[(n + items.length) % items.length]?.focus(); };
    if (e.key === 'ArrowDown') go(i + 1);
    else if (e.key === 'ArrowUp') go(i < 0 ? items.length - 1 : i - 1);
    else if (e.key === 'Home') go(0);
    else if (e.key === 'End') go(items.length - 1);
    else if (e.key === 'Escape' || e.key === 'Tab') { if (e.key === 'Escape') e.preventDefault(); close(); }
  });
}

function focusFirst(menu: HTMLElement) {
  menu.querySelectorAll<HTMLElement>('a.menu-item:not([href])').forEach((a) => { a.tabIndex = -1; a.setAttribute('role', 'menuitem'); });
  requestAnimationFrame(() => $$<HTMLElement>(ITEM, menu).find((x) => x.offsetParent !== null)?.focus());
}

/** Dropdown menus: <div data-menu><button data-menu-toggle/> <div class="menu hidden">...</div></div> */
export function initMenus(root: ParentNode = document) {
  const closeAll = (except?: Element | null) => $$('[data-menu] > .menu').forEach((m) => {
    if (m === except || m.classList.contains('hidden')) return;
    m.classList.add('hidden');
    m.parentElement!.querySelector('[data-menu-toggle]')?.setAttribute('aria-expanded', 'false');
  });
  $$('[data-menu-toggle]', root).forEach((t) => { t.setAttribute('aria-haspopup', 'menu'); t.setAttribute('aria-expanded', 'false'); });
  root.addEventListener('click', (e) => {
    const t = e.target as HTMLElement;
    const toggle = t.closest<HTMLElement>('[data-menu-toggle]');
    const keep = t.closest('[data-keep-open]') ? t.closest('[data-menu] > .menu') : null; // checkboxes, radios
    if (toggle) {
      const menu = toggle.parentElement!.querySelector<HTMLElement>('.menu')!;
      const open = menu.classList.contains('hidden');
      closeAll(open ? null : menu);
      menu.classList.toggle('hidden', !open);
      toggle.setAttribute('aria-expanded', String(open));
      if (open) {
        menu.setAttribute('role', 'menu');
        if (!menu.dataset.keys) { menu.dataset.keys = '1'; menuKeys(menu, () => { menu.classList.add('hidden'); toggle.setAttribute('aria-expanded', 'false'); toggle.focus(); }); }
        if ((e as PointerEvent).detail === 0) focusFirst(menu); // opened from the keyboard
      }
      return;
    }
    closeAll(keep);
  });
  root.addEventListener('keydown', (e) => {
    const k = e as KeyboardEvent;
    const toggle = (k.target as HTMLElement).closest?.('[data-menu-toggle]');
    if (toggle && k.key === 'ArrowDown') { k.preventDefault(); (toggle as HTMLElement).click(); focusFirst(toggle.parentElement!.querySelector('.menu')!); }
  });
}

export const KIND_LABEL: Record<string, string> = { publication: 'Publication', pressrelease: 'Press Release', upload: 'Upload' };

// ---------------------------------------------------------------- context menu
export type MenuItem =
  | { label: string; icon?: string; hint?: string; danger?: boolean; disabled?: boolean; action: () => void }
  | { heading: string }
  | 'sep';

let openMenu: HTMLElement | null = null;
let anchorEl: Node | null = null;
let returnFocus: HTMLElement | null = null;
export function closeContextMenu(restore = false) {
  if (!openMenu) return;
  openMenu.remove();
  openMenu = null;
  if (restore) returnFocus?.focus();
}

/**
 * Context menu at the pointer (MouseEvent) or below an element (keyboard: Menu key / Shift+F10).
 * Fully keyboard operable: arrows, Home/End, Enter, Escape.
 */
export function contextMenu(at: MouseEvent | HTMLElement, items: MenuItem[]) {
  if (at instanceof Event) at.preventDefault();
  closeContextMenu();
  returnFocus = document.activeElement as HTMLElement | null;
  anchorEl = at instanceof Event ? (at.target as Node) : at;
  const el = h(`<div role="menu" class="fixed z-[60] min-w-56 max-w-96 bg-surface border border-ink-300 shadow-xl py-1 text-[12.5px] select-none"></div>`);
  items.forEach((it, i) => {
    if (it === 'sep') { el.append(h('<div role="separator" class="my-1 border-t border-ink-100"></div>')); return; }
    if ('heading' in it) { el.append(h(`<div role="presentation" class="px-3 pt-1.5 pb-1 text-[10.5px] font-semibold uppercase tracking-wider text-ink-500 truncate">${esc(it.heading)}</div>`)); return; }
    const b = h(`<button role="menuitem" tabindex="-1" data-i="${i}" ${it.disabled ? 'disabled aria-disabled="true"' : ''} class="flex w-full items-center gap-lg px-3 h-8 text-left focus:outline-none
      ${it.danger ? 'text-red-700 hover:bg-red-50 focus:bg-red-50' : 'text-ink-800 hover:bg-brand-50 focus:bg-brand-50'} disabled:text-ink-300 disabled:hover:bg-transparent">
      <span class="w-4 shrink-0 ${it.danger ? '' : 'text-ink-500'}">${it.icon ? icon(it.icon, 'size-4') : ''}</span>
      <span class="flex-1 truncate">${it.label}</span>${it.hint ? `<span class="text-[11px] text-ink-400 pl-3">${esc(it.hint)}</span>` : ''}</button>`);
    b.addEventListener('click', () => { closeContextMenu(true); it.action(); });
    el.append(b);
  });
  document.body.append(el);
  const r = el.getBoundingClientRect();
  let x: number, y: number;
  if (at instanceof Event) { x = at.clientX; y = at.clientY; }
  else { const b = at.getBoundingClientRect(); x = b.left + 24; y = b.bottom; }
  el.style.left = `${Math.max(6, Math.min(x, innerWidth - r.width - 6))}px`;
  el.style.top = `${Math.max(6, Math.min(y, innerHeight - r.height - 6))}px`;
  openMenu = el;
  menuKeys(el, () => closeContextMenu(true));
  el.querySelector<HTMLElement>('[role=menuitem]:not([disabled])')?.focus();
}
addEventListener('mousedown', (e) => { if (openMenu && !openMenu.contains(e.target as Node)) closeContextMenu(); }, true);
addEventListener('blur', () => closeContextMenu());
addEventListener('resize', () => closeContextMenu());
// close only when the content the menu belongs to scrolls (other panes, e.g. a smooth-scrolling PDF, don't matter)
addEventListener('scroll', (e) => {
  const t = e.target;
  if (openMenu && (t === document || (t instanceof Node && anchorEl && t.contains(anchorEl)))) closeContextMenu();
}, true);

/** Menu key / Shift+F10 on an element */
export const isMenuKey = (e: KeyboardEvent) => e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10');

// ---------------------------------------------------------------- tooltips
// HIG: tooltips also appear on keyboard focus and press-and-hold, show the shortcut, and icon-only
// controls get an accessible name. Native `title` attributes are converted on the fly.
const SHORTCUT = /\s*\(((?:Ctrl|Shift|Alt|Meta)\+[^)]+|[A-Z]|Del|Delete|Enter|Esc|F\d+|Menu)\)\s*$/;
let tipEl: HTMLElement | null = null;
let tipTimer: ReturnType<typeof setTimeout> | undefined;

function prepare(el: HTMLElement) {
  const raw = el.getAttribute('title');
  if (raw !== null) {
    el.removeAttribute('title');
    el.dataset.tip = raw;
  }
  const text = el.dataset.tip ?? '';
  const label = text.replace(SHORTCUT, '').trim();
  const visible = (el.innerText ?? '').trim();
  const interactive = el.matches('a, button, input, select, textarea, [role], [tabindex]');
  if (!interactive) return text; // plain elements keep only the visual tooltip
  if (label && !el.getAttribute('aria-label') && !visible) el.setAttribute('aria-label', label);
  else if (label && visible && label !== visible && !el.getAttribute('aria-description')) el.setAttribute('aria-description', label);
  return text;
}

function showTip(el: HTMLElement) {
  const text = prepare(el);
  if (!text) return;
  hideTip();
  const m = text.match(SHORTCUT);
  const body = m ? text.replace(SHORTCUT, '') : text;
  tipEl = h(`<div class="tip" role="tooltip">${esc(body)}${m ? `<kbd>${esc(m[1])}</kbd>` : ''}</div>`);
  document.body.append(tipEl);
  const r = el.getBoundingClientRect(), t = tipEl.getBoundingClientRect();
  let top = r.bottom + 6;
  if (top + t.height > innerHeight - 4) top = r.top - t.height - 6;
  tipEl.style.top = `${top}px`;
  tipEl.style.left = `${Math.max(4, Math.min(r.left + r.width / 2 - t.width / 2, innerWidth - t.width - 4))}px`;
}
function hideTip() { clearTimeout(tipTimer); tipEl?.remove(); tipEl = null; }

export function initTooltips() {
  const target = (e: Event) => (e.target as HTMLElement)?.closest?.<HTMLElement>('[title],[data-tip]') ?? null;
  // convert existing titles up front so screen readers get names before any hover
  const scan = (root: ParentNode) => $$<HTMLElement>('[title]', root).forEach((el) => { if (!(el instanceof SVGElement)) prepare(el); });
  scan(document);
  new MutationObserver((ms) => ms.forEach((m) => m.addedNodes.forEach((n) => {
    if (n instanceof HTMLElement) { if (n.hasAttribute('title')) prepare(n); scan(n); }
  }))).observe(document.body, { childList: true, subtree: true });
  addEventListener('mouseover', (e) => {
    const el = target(e);
    if (!el) return;
    clearTimeout(tipTimer);
    tipTimer = setTimeout(() => showTip(el), 450);
  });
  addEventListener('mouseout', (e) => { if (target(e)) hideTip(); });
  addEventListener('focusin', (e) => {
    const el = target(e);
    if (el && el.matches(':focus-visible')) showTip(el);
  });
  addEventListener('focusout', hideTip);
  addEventListener('pointerdown', (e) => {
    hideTip();
    if ((e as PointerEvent).pointerType !== 'touch') return;
    const el = target(e);
    if (el) tipTimer = setTimeout(() => showTip(el), 500); // press-and-hold
  }, true);
  addEventListener('pointerup', () => clearTimeout(tipTimer), true);
  addEventListener('keydown', (e) => { if (e.key === 'Escape') hideTip(); });
  addEventListener('scroll', hideTip, true);
}
