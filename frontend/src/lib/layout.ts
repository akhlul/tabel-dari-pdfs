/**
 * Tiny pane layout engine for the workspace.
 * Four panes (A = PDF viewer, B = table list, M = table details: flag, title, notes, G = cell grid) are arranged
 * as nested splits and tab groups. The details and the grid together form the editor (M above G).
 * Pane elements are moved (not re-created), so their state and listeners survive a layout change.
 */
export type PaneId = 'A' | 'B' | 'M' | 'G';
type Size = string; // 'NNpx' | 'NN%' | '1fr'
export type LayoutNode =
  | { pane: PaneId }
  | { split: 'row' | 'col'; items: LayoutNode[]; sizes: Size[] }
  | { tabs: LayoutNode[] };

export interface LayoutDef { id: string; label: string; hint: string; tree: LayoutNode }

const A = { pane: 'A' } as const, B = { pane: 'B' } as const, M = { pane: 'M' } as const, G = { pane: 'G' } as const;
/** the editor: details above the grid */
const E = (m = '250px'): LayoutNode => ({ split: 'col', items: [M, G], sizes: [m, '1fr'] });
export const LAYOUTS: LayoutDef[] = [
  { id: 'standard', label: 'Standard (3-Column)', hint: 'List · Viewer · Details, the grid below',
    tree: { split: 'row', items: [B, { split: 'col', items: [{ split: 'row', items: [A, M], sizes: ['1fr', '300px'] }, G], sizes: ['1fr', '38%'] }], sizes: ['270px', '1fr'] } },
  { id: 'stacked', label: 'Viewer Left (Stacked Right)', hint: 'The list above the editor',
    tree: { split: 'row', items: [A, { split: 'col', items: [B, M, G], sizes: ['24%', '250px', '1fr'] }], sizes: ['1fr', '48%'] } },
  { id: 'right-tabs', label: 'Viewer Left (Right Tabs)', hint: 'The list and the editor share a tabbed pane',
    tree: { split: 'row', items: [A, { tabs: [B, E()] }], sizes: ['1fr', '48%'] } },
  { id: 'list-tabs', label: 'List Left (Viewer/Editor Tabs)', hint: 'Best for narrow screens',
    tree: { split: 'row', items: [B, { tabs: [A, E()] }], sizes: ['270px', '1fr'] } },
];
/** Single pane (Focus Mode): one pane at a time, in tabs */
export const FOCUS: LayoutDef = { id: 'focus', label: 'Single Pane (Focus Mode)', hint: 'One pane at a time', tree: { tabs: [A, B, E('230px')] } };
const ALL = [...LAYOUTS, FOCUS];
/** layouts from before the details pane existed */
const OLD: Record<string, string> = { three: 'standard', mirror: 'standard', stack: 'standard', split: 'stacked', 'split-tabs': 'right-tabs', tabs: 'standard' };

/** layouts that need room; narrow windows fall back (HIG: adapt to narrow and tiled windows) */
const WIDE = new Set(['standard', 'stacked']);

const KEY = 'tdp.layout2';

export class Layout {
  id: string;
  private tabGroups: { panes: PaneId[]; members: Map<PaneId, PaneId>; active: PaneId; bar: HTMLElement; bodies: Map<PaneId, HTMLElement> }[] = [];

  /** Single pane (Focus Mode), on top of the chosen preset */
  focusMode = false;

  constructor(private host: HTMLElement, private panes: Record<PaneId, HTMLElement>,
    private tabLabel: (p: PaneId) => string, private onChange: () => void = () => {}) {
    const old = localStorage.getItem('tdp.layout');
    this.id = localStorage.getItem(KEY) ?? (old ? OLD[old] ?? old : 'standard');
    if (!LAYOUTS.some((l) => l.id === this.id)) this.id = 'standard';
    this.focusMode = localStorage.getItem(`${KEY}:focus`) === '1' || old === 'tabs' && !localStorage.getItem(KEY);
    addEventListener('resize', () => {
      clearTimeout(this.rt);
      this.rt = setTimeout(() => { if (this.effective() !== this.shown) this.apply(); }, 150);
    });
  }
  private rt: ReturnType<typeof setTimeout> | undefined;
  private shown = '';

  /** the layout actually shown: the chosen one, or a simpler one when the window is narrow */
  effective(id = this.id) {
    const w = innerWidth;
    if (this.focusMode || w < 720) return 'focus';
    if (w < 1100 && WIDE.has(id)) return 'list-tabs';
    return id;
  }

  setFocusMode(on: boolean) {
    this.focusMode = on;
    localStorage.setItem(`${KEY}:focus`, on ? '1' : '0');
    this.apply();
  }

  /** Ctrl+PgDn / Ctrl+PgUp: next / previous tab of the first tab group; false when there are no tabs */
  cycleTab(dir: 1 | -1) {
    const g = this.tabGroups[0];
    if (!g) return false;
    const i = g.panes.indexOf(g.active);
    this.activate(g, g.panes[(i + dir + g.panes.length) % g.panes.length]!);
    return true;
  }

  apply(id = this.id) {
    this.id = id;
    localStorage.setItem(KEY, id);
    const eff = this.effective(id);
    this.shown = eff;
    const def = ALL.find((l) => l.id === eff)!;
    // moving an element resets its scroll position: remember and restore it
    const scrolled: [HTMLElement, number, number][] = [];
    for (const p of Object.values(this.panes)) {
      p.querySelectorAll<HTMLElement>('*').forEach((el) => { if (el.scrollTop || el.scrollLeft) scrolled.push([el, el.scrollTop, el.scrollLeft]); });
    }
    for (const p of Object.values(this.panes)) p.remove();
    this.host.innerHTML = '';
    this.tabGroups = [];
    const el = this.build(def.tree, `${eff}`);
    el.classList.add('flex-1', 'min-h-0', 'min-w-0');
    this.host.append(el);
    for (const [e, t, l] of scrolled) { e.scrollTop = t; e.scrollLeft = l; }
    this.onChange();
  }

  /** bring a pane to front when it sits in a tab group (a tab may hold several panes, e.g. the editor) */
  focus(p: PaneId) {
    const g = this.tabGroups.find((g) => g.members.has(p));
    const tab = g?.members.get(p);
    if (g && tab && g.active !== tab) this.activate(g, tab);
  }

  isVisible(p: PaneId) {
    const g = this.tabGroups.find((g) => g.members.has(p));
    return !g || g.active === g.members.get(p);
  }

  refreshTabs() {
    for (const g of this.tabGroups) this.renderBar(g);
  }

  private build(n: LayoutNode, path: string): HTMLElement {
    if ('pane' in n) {
      const w = document.createElement('div');
      w.className = 'flex min-h-0 min-w-0 h-full w-full';
      w.append(this.panes[n.pane]);
      return w;
    }
    if ('tabs' in n) return this.buildTabs(n.tabs, path);
    const box = document.createElement('div');
    box.className = `flex ${n.split === 'row' ? 'flex-row' : 'flex-col'} min-h-0 min-w-0 h-full w-full`;
    const saved: Size[] = JSON.parse(localStorage.getItem(`${KEY}:${path}`) ?? 'null') ?? n.sizes;
    const cells: HTMLElement[] = [];
    n.items.forEach((item, i) => {
      if (i > 0) box.append(this.splitter(n.split, cells, i, path));
      const cell = document.createElement('div');
      cell.className = 'flex min-h-0 min-w-0';
      setSize(cell, saved[i] ?? '1fr');
      cell.dataset.size = saved[i] ?? '1fr';
      cell.append(this.build(item, `${path}.${i}`));
      cells.push(cell);
      box.append(cell);
    });
    return box;
  }

  private splitter(dir: 'row' | 'col', cells: HTMLElement[], i: number, path: string) {
    const s = document.createElement('div');
    s.className = dir === 'row'
      ? 'w-1 shrink-0 cursor-col-resize bg-ink-200 hover:bg-brand-500 transition-colors'
      : 'h-1 shrink-0 cursor-row-resize bg-ink-200 hover:bg-brand-500 transition-colors';
    s.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      const before = cells[i - 1]!, after = cells[i]!;
      // resize whichever neighbour has a fixed size (the other one flexes)
      const target = before.dataset.size !== '1fr' ? before : after;
      const sign = target === before ? 1 : -1;
      const prop = dir === 'row' ? 'width' : 'height';
      const start = dir === 'row' ? e.clientX : e.clientY;
      const startSize = target.getBoundingClientRect()[prop];
      const parent = target.parentElement!.getBoundingClientRect()[prop];
      const move = (ev: PointerEvent) => {
        const d = ((dir === 'row' ? ev.clientX : ev.clientY) - start) * sign;
        const px = Math.max(120, Math.min(parent - 200, startSize + d));
        target.dataset.size = `${Math.round(px)}px`;
        setSize(target, target.dataset.size);
      };
      const up = () => {
        removeEventListener('pointermove', move);
        removeEventListener('pointerup', up);
        document.body.style.cursor = '';
        localStorage.setItem(`${KEY}:${path}`, JSON.stringify(cells.map((c) => c.dataset.size)));
      };
      addEventListener('pointermove', move);
      addEventListener('pointerup', up);
      document.body.style.cursor = dir === 'row' ? 'col-resize' : 'row-resize';
    });
    s.addEventListener('dblclick', () => { localStorage.removeItem(`${KEY}:${path}`); this.apply(); }); // reset sizes
    s.title = 'Drag or use the arrow keys to resize. Double-click to reset.';
    s.setAttribute('role', 'separator');
    s.setAttribute('aria-orientation', dir === 'row' ? 'vertical' : 'horizontal');
    s.setAttribute('aria-label', 'Resize panes');
    s.setAttribute('aria-valuemin', '0');
    s.setAttribute('aria-valuemax', '100');
    const report = () => {
      const a = cells[i - 1]?.getBoundingClientRect(), p = s.parentElement?.getBoundingClientRect();
      if (a && p) s.setAttribute('aria-valuenow', String(Math.round((100 * (dir === 'row' ? a.width / p.width : a.height / p.height)) || 0)));
    };
    requestAnimationFrame(report);
    s.addEventListener('keyup', report);
    s.addEventListener('pointerup', report);
    s.tabIndex = 0;
    s.classList.add('focus-visible:bg-brand-500');
    s.addEventListener('keydown', (e) => {
      const d = dir === 'row' ? { ArrowLeft: -1, ArrowRight: 1 }[e.key] : { ArrowUp: -1, ArrowDown: 1 }[e.key];
      if (!d) return;
      e.preventDefault();
      const before = cells[i - 1]!, after = cells[i]!;
      const target = before.dataset.size !== '1fr' ? before : after;
      const sign = target === before ? 1 : -1;
      const prop = dir === 'row' ? 'width' : 'height';
      const px = Math.max(120, target.getBoundingClientRect()[prop] + d * sign * (e.shiftKey ? 80 : 20));
      target.dataset.size = `${Math.round(px)}px`;
      setSize(target, target.dataset.size);
      localStorage.setItem(`${KEY}:${path}`, JSON.stringify(cells.map((c) => c.dataset.size)));
    });
    return s;
  }

  private buildTabs(nodes: LayoutNode[], path: string) {
    const box = document.createElement('div');
    box.className = 'flex flex-col min-h-0 min-w-0 h-full w-full bg-surface';
    const bar = document.createElement('div');
    bar.className = 'h-9 shrink-0 flex items-stretch border-b border-ink-200 bg-ink-50';
    const body = document.createElement('div');
    body.className = 'flex-1 min-h-0 min-w-0 flex';
    const bodies = new Map<PaneId, HTMLElement>();
    const members = new Map<PaneId, PaneId>(); // every pane -> the tab (named after its first pane) it is in
    const panes: PaneId[] = [];
    nodes.forEach((n, i) => {
      const ids = paneIds(n);
      const key = ids[0]!;
      panes.push(key);
      ids.forEach((p) => members.set(p, key));
      const w = document.createElement('div');
      w.className = 'flex-1 min-h-0 min-w-0 flex';
      w.append(this.build(n, `${path}.t${i}`));
      bodies.set(key, w);
      body.append(w);
    });
    const saved = localStorage.getItem(`${KEY}:${path}:tab`) as PaneId | null;
    const g = { panes, members, active: saved && panes.includes(saved) ? saved : panes[0]!, bar, bodies, path };
    this.tabGroups.push(g);
    bar.addEventListener('click', (e) => {
      const b = (e.target as HTMLElement).closest<HTMLElement>('[data-tab]');
      if (b) this.activate(g, b.dataset.tab as PaneId);
    });
    bar.addEventListener('keydown', (e) => { // tabs: arrows move between them
      const d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      const next = g.panes[(g.panes.indexOf(g.active) + d + g.panes.length) % g.panes.length]!;
      this.activate(g, next);
      bar.querySelector<HTMLElement>(`[data-tab="${next}"]`)?.focus();
    });
    this.activate(g, g.active, false);
    box.append(bar, body);
    return box;
  }

  private activate(g: (typeof this.tabGroups)[number] & { path?: string }, p: PaneId, notify = true) {
    g.active = p;
    if (g.path) localStorage.setItem(`${KEY}:${g.path}:tab`, p);
    const keep: [HTMLElement, number][] = [];
    g.bodies.forEach((el) => el.querySelectorAll<HTMLElement>('*').forEach((x) => { if (x.scrollTop) keep.push([x, x.scrollTop]); }));
    g.bodies.forEach((el, id) => (el.style.display = id === p ? '' : 'none'));
    for (const [x, t] of keep) if (x.scrollTop !== t) x.scrollTop = t;
    this.renderBar(g);
    if (notify) this.onChange();
  }

  private renderBar(g: (typeof this.tabGroups)[number]) {
    g.bar.setAttribute('role', 'tablist');
    g.bar.innerHTML = g.panes.map((p) => `<button data-tab="${p}" role="tab" aria-selected="${p === g.active}" tabindex="${p === g.active ? 0 : -1}" class="px-4 text-[12.5px] font-medium border-r border-ink-200 -mb-px border-b-2
      ${p === g.active ? 'bg-surface text-ink-900 border-b-brand-600 font-semibold' : 'text-ink-600 border-b-transparent hover:text-ink-900 hover:bg-surface/60'}">${this.tabLabel(p)}</button>`).join('');
  }
}

function paneIds(n: LayoutNode): PaneId[] {
  return 'pane' in n ? [n.pane] : 'tabs' in n ? n.tabs.flatMap(paneIds) : n.items.flatMap(paneIds);
}

function setSize(el: HTMLElement, size: Size) {
  el.style.flex = size === '1fr' ? '1 1 0%' : `0 0 ${size}`;
}
