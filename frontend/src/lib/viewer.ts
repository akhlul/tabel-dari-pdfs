// legacy build: pdf.js 6 uses very new JS APIs (Map.getOrInsertComputed); legacy ships polyfills
import * as pdfjs from 'pdfjs-dist/legacy/build/pdf.mjs';
import workerUrl from 'pdfjs-dist/legacy/build/pdf.worker.min.mjs?url';
import type { BBox, Table } from './api';
import { esc } from './ui';

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

type PDFDoc = Awaited<ReturnType<typeof pdfjs.getDocument>['promise']>;

interface PageView {
  n: number;
  el: HTMLDivElement;
  canvas: HTMLCanvasElement;
  /** selectable text (pdf.js text layer), built once per page: it scales with --total-scale-factor */
  text: HTMLDivElement;
  textState: 'none' | 'loading' | 'done';
  overlay: HTMLDivElement;
  w: number; // points
  h: number;
  renderedAt: number; // zoom value it was rendered at; 0 = not rendered
  task: { cancel(): void; promise: Promise<unknown> } | null;
}

export interface ViewerEvents {
  onSelect?: (tableId: string) => void;
  /** the selected box was moved / resized (bbox in PDF points) */
  onBoxChange?: (tableId: string, bbox: BBox) => void;
  /** a joined continuation region was clicked (part index, 0-based) */
  onPart?: (tableId: string, part: number) => void;
  onRegion?: (page: number, bbox: BBox) => void;
  onPage?: (page: number) => void;
}

/** Continuous-scroll PDF viewer with table bounding-box overlays and region drawing. */
export class PdfViewer {
  pdf: PDFDoc | null = null;
  pages: PageView[] = [];
  zoom = 1;
  fit = true;
  drawMode = false;
  /** "Select Text": table boxes let the pointer through so text inside them can be selected */
  textMode = false;
  showBoxes = true;
  current = 1;
  active: string | null = null;
  private tables: Table[] = [];
  private io: IntersectionObserver;
  private visible = new Set<number>();
  private queue: number[] = [];
  private rendering = 0;

  constructor(private scroller: HTMLElement, private host: HTMLElement, private ev: ViewerEvents = {}) {
    this.io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        const n = +(e.target as HTMLElement).dataset.page!;
        if (e.isIntersecting) this.visible.add(n); else this.visible.delete(n);
      }
      this.schedule();
    }, { root: scroller, rootMargin: '800px 0px' });
    scroller.addEventListener('scroll', () => this.trackPage(), { passive: true });
    new ResizeObserver(() => { if (this.fit) this.fitWidth(); }).observe(scroller);
  }

  async load(url: string, sizes: [number, number][]) {
    this.host.innerHTML = '';
    this.pages = sizes.map(([w, h], i) => this.makePage(i + 1, w, h));
    this.fitWidth();
    this.drawBoxes();
    this.pdf = await pdfjs.getDocument({ url }).promise;
    this.schedule();
  }

  private makePage(n: number, w: number, h: number): PageView {
    const el = document.createElement('div');
    el.className = 'pdf-page relative mx-auto bg-[#fff] shadow-[0_1px_3px_rgba(20,26,34,.18)]';
    el.dataset.page = String(n);
    el.style.setProperty('--total-scale-factor', String(this.zoom));
    const canvas = document.createElement('canvas');
    canvas.className = 'absolute inset-0 w-full h-full select-none';
    const text = document.createElement('div');
    text.className = 'textLayer';
    const overlay = document.createElement('div');
    overlay.className = 'pv-overlay absolute inset-0 select-none';
    const label = document.createElement('div');
    label.className = 'absolute -left-9 top-0 w-7 text-right text-[11px] text-ink-600 font-medium select-none';
    label.textContent = String(n);
    el.append(canvas, text, overlay, label);
    this.host.append(el);
    this.io.observe(el);
    const pv: PageView = { n, el, canvas, text, textState: 'none', overlay, w, h, renderedAt: 0, task: null };
    this.bindDraw(pv);
    return pv;
  }

  setZoom(z: number, fit = false) {
    const anchor = this.current;
    const rel = this.relScroll();
    this.fit = fit;
    this.zoom = Math.min(4, Math.max(0.3, z));
    for (const p of this.pages) {
      p.el.style.width = `${p.w * this.zoom}px`;
      p.el.style.height = `${p.h * this.zoom}px`;
      p.el.style.setProperty('--total-scale-factor', String(this.zoom));
    }
    this.host.style.gap = `${Math.max(8, 14 * this.zoom)}px`;
    // keep the same spot in view
    const pv = this.pages[anchor - 1];
    if (pv) this.scroller.scrollTop = this.top(pv) + rel * pv.el.offsetHeight;
    this.schedule();
  }

  fitWidth() {
    if (!this.pages.length || !this.scroller.clientWidth) return;
    const maxW = Math.max(...this.pages.map((p) => p.w), 1);
    const avail = this.scroller.clientWidth - 80;
    this.setZoom(Math.max(0.3, Math.min(2, avail / maxW)), true); // wide panes: cap at 200%
  }

  /** page top in scroller content coordinates (host is `relative`, scroller is `relative`) */
  private top(pv: PageView) {
    return this.host.offsetTop + pv.el.offsetTop;
  }

  private relScroll() {
    const pv = this.pages[this.current - 1];
    if (!pv || !pv.el.offsetHeight) return 0;
    return (this.scroller.scrollTop - (this.top(pv))) / pv.el.offsetHeight;
  }

  private trackPage() {
    const mid = this.scroller.scrollTop + this.scroller.clientHeight * 0.35;
    let cur = 1;
    for (const p of this.pages) {
      if (this.top(p) <= mid) cur = p.n; else break;
    }
    if (cur !== this.current) { this.current = cur; this.ev.onPage?.(cur); }
  }

  private schedule() {
    if (!this.pdf) return;
    const want = [...this.visible].filter((n) => this.pages[n - 1]!.renderedAt !== this.zoom)
      .sort((a, b) => Math.abs(a - this.current) - Math.abs(b - this.current));
    this.queue = want;
    // free canvases of pages well outside the viewport (memory: one page at 200% ≈ 30 MB)
    for (const pv of this.pages) {
      if (pv.renderedAt && !this.visible.has(pv.n) && Math.abs(pv.n - this.current) > 3) {
        pv.task?.cancel();
        pv.canvas.width = 0; pv.canvas.height = 0;
        pv.renderedAt = 0;
      }
    }
    while (this.rendering < 2 && this.queue.length) this.render(this.queue.shift()!);
  }

  private async render(n: number) {
    const pv = this.pages[n - 1]!;
    if (pv.renderedAt === this.zoom || !this.pdf) return;
    pv.task?.cancel();
    this.rendering++;
    const zoom = this.zoom;
    try {
      const page = await this.pdf.getPage(n);
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const base = page.getViewport({ scale: 1 });
      // never allocate more than ~12 Mpx per page canvas
      const scale = Math.min(zoom * dpr, Math.sqrt(12e6 / (base.width * base.height)));
      const vp = page.getViewport({ scale });
      const c = document.createElement('canvas');
      c.width = Math.floor(vp.width);
      c.height = Math.floor(vp.height);
      const task = page.render({ canvas: c, viewport: vp });
      pv.task = task;
      await task.promise;
      c.className = pv.canvas.className;
      pv.canvas.replaceWith(c);
      pv.canvas = c;
      pv.renderedAt = zoom;
      if (pv.textState === 'none') this.renderText(pv, page);
    } catch (e: any) {
      if (e?.name !== 'RenderingCancelledException') console.warn('render failed', n, e);
    } finally {
      pv.task = null;
      this.rendering--;
      this.schedule();
    }
  }

  private async renderText(pv: PageView, page: Awaited<ReturnType<PDFDoc['getPage']>>) {
    pv.textState = 'loading';
    try {
      const layer = new pdfjs.TextLayer({
        textContentSource: page.streamTextContent({ includeMarkedContent: true, disableNormalization: true }),
        container: pv.text,
        viewport: page.getViewport({ scale: 1 }),
      });
      await layer.render();
      const end = document.createElement('div'); // keeps a drag-selection from jumping to the page top
      end.className = 'endOfContent';
      pv.text.append(end);
      pv.text.addEventListener('mousedown', () => pv.text.classList.add('selecting'));
      addEventListener('mouseup', () => pv.text.classList.remove('selecting'));
      pv.textState = 'done';
    } catch (e) {
      pv.textState = 'none';
      console.warn('text layer failed', pv.n, e);
    }
  }

  /** "Select Text": boxes stay visible but let the pointer through to the text below */
  setTextMode(on: boolean) {
    this.textMode = on;
    if (on && this.drawMode) this.setDrawMode(false);
    this.host.classList.toggle('text-mode', on);
  }

  scrollToPage(n: number, smooth = false) {
    const pv = this.pages[n - 1];
    if (!pv) return;
    this.scroller.scrollTo({ top: this.top(pv), behavior: smooth ? 'smooth' : 'auto' });
  }

  scrollToTable(t: Table) {
    const pv = this.pages[t.page - 1];
    if (!pv) return;
    const y = this.top(pv) + (t.bbox[1] / pv.h) * pv.el.offsetHeight - 60;
    const top = this.scroller.scrollTop, bottom = top + this.scroller.clientHeight;
    const ty = this.top(pv) + (t.bbox[1] / pv.h) * pv.el.offsetHeight;
    const by = this.top(pv) + (t.bbox[3] / pv.h) * pv.el.offsetHeight;
    if (ty < top + 40 || by > bottom) this.scroller.scrollTo({ top: Math.max(0, y), behavior: 'smooth' });
  }

  setTables(tables: Table[]) {
    this.tables = tables;
    this.drawBoxes();
  }

  setActive(id: string | null) {
    this.active = id;
    this.drawBoxes();
  }

  /** which tables get a box (the workspace filter) */
  isShown: (t: Table) => boolean = (t) => !t.deleted;

  drawBoxes() {
    // keep keyboard focus on the same box across redraws
    const focused = (document.activeElement as HTMLElement | null)?.closest?.('.tbox') as HTMLElement | null;
    const refocus = focused && this.host.contains(focused) ? `${focused.dataset.id}|${focused.dataset.part ?? ''}` : null;
    const byPage = new Map<number, Table[]>();
    for (const t of this.tables) byPage.set(t.page, [...(byPage.get(t.page) ?? []), t]);
    for (const pv of this.pages) {
      pv.overlay.querySelectorAll('.tbox').forEach((b) => b.remove());
      if (!this.showBoxes) continue;
      const onPage = byPage.get(pv.n) ?? [];
      const area = (t: Table) => (t.bbox[2] - t.bbox[0]) * (t.bbox[3] - t.bbox[1]);
      // larger boxes first so nested (region) boxes stay clickable on top
      const order = onPage.filter((t) => this.isShown(t) || t.id === this.active).sort((a, b) => area(b) - area(a));
      order.forEach((t) => {
        const i = onPage.indexOf(t);
        const [x0, y0, x1, y1] = t.bbox;
        if (x1 - x0 <= 0 || y1 - y0 <= 0) return; // manual tables have no area
        const b = document.createElement('div');
        const state = t.deleted ? 'deleted' : t.status;
        b.className = `tbox s-${state}${t.id === this.active ? ' is-active' : ''}`;
        b.dataset.id = t.id;
        this.place(b, pv, t.bbox);
        const flag = state === 'valid' ? ' · ✓ valid' : state === 'invalid' ? ' · ⊘ not a table' : state === 'deleted' ? ' · deleted' : '';
        b.innerHTML = `<span class="tag" aria-hidden="true">T${i + 1}${flag}${t.edited ? ' · edited' : ''}</span>`
          + ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'].map((h) => `<i class="hdl" data-h="${h}" aria-hidden="true"></i>`).join('');
        b.tabIndex = 0;
        b.setAttribute('role', 'button');
        // accessible name starts with the visible tag text (WCAG label-in-name)
        b.setAttribute('aria-label', `T${i + 1}${flag}${t.edited ? ' · edited' : ''}: ${t.title || 'untitled table'}, page ${pv.n}${t.id === this.active ? '. Selected: arrow keys move, Shift+arrow keys resize' : ''}`);
        b.setAttribute('aria-pressed', String(t.id === this.active));
        if (this.drawMode) b.style.pointerEvents = 'none';
        this.bindBox(b, pv, t);
        pv.overlay.append(b);
      });
    }
    // continuation regions of joined tables: drawn on their own page, clicking selects the parent
    for (const t of this.tables) {
      if (!this.showBoxes || !(t.parts?.length) || !(this.isShown(t) || t.id === this.active)) continue;
      t.parts.forEach((p, k) => {
        const pv = this.pages[p.page - 1];
        if (!pv) return;
        const b = document.createElement('div');
        const state = t.deleted ? 'deleted' : t.status;
        b.className = `tbox is-part s-${state}${t.id === this.active ? ' is-active-part' : ''}`;
        b.dataset.id = t.id;
        this.place(b, pv, p.bbox);
        b.innerHTML = `<span class="tag" aria-hidden="true">↳ continues p${t.page} table · part ${k + 2}</span>`;
        b.dataset.part = String(k);
        b.tabIndex = 0;
        b.setAttribute('role', 'button');
        b.setAttribute('aria-label', `↳ continues p${t.page} table · part ${k + 2}: ${t.title || 'untitled table'}`);
        b.addEventListener('keydown', (e) => { if ((e.key === 'Enter' || e.key === ' ') && !this.drawMode) { e.preventDefault(); this.ev.onPart ? this.ev.onPart(t.id, k) : this.ev.onSelect?.(t.id); } });
        if (this.drawMode) b.style.pointerEvents = 'none';
        b.addEventListener('pointerdown', (e) => {
          if (this.drawMode || e.button !== 0) return;
          e.stopPropagation();
          this.ev.onPart ? this.ev.onPart(t.id, k) : this.ev.onSelect?.(t.id);
        });
        pv.overlay.append(b);
      });
    }
    if (refocus) {
      const [id, part] = refocus.split('|');
      this.host.querySelector<HTMLElement>(part ? `.tbox[data-id="${id}"][data-part="${part}"]` : `.tbox[data-id="${id}"]:not([data-part])`)?.focus({ preventScroll: true });
    }
  }

  private place(el: HTMLElement, pv: PageView, [x0, y0, x1, y1]: BBox) {
    Object.assign(el.style, {
      left: `${(x0 / pv.w) * 100}%`, top: `${(y0 / pv.h) * 100}%`,
      width: `${((x1 - x0) / pv.w) * 100}%`, height: `${((y1 - y0) / pv.h) * 100}%`,
    });
  }

  /** click selects; on the selected box, drag moves and the handles resize */
  private bindBox(b: HTMLElement, pv: PageView, t: Table) {
    let kbTimer: ReturnType<typeof setTimeout> | undefined;
    let kbBox: BBox | null = null;
    b.addEventListener('keydown', (e) => {
      if (this.drawMode) return;
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (t.id !== this.active) this.ev.onSelect?.(t.id); return; }
      const dirs: Record<string, [number, number]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
      const d = dirs[e.key];
      if (!d || t.id !== this.active) return;
      e.preventDefault();
      const step = e.altKey ? 0.5 : 2; // points
      let [x0, y0, x1, y1] = kbBox ?? ([...t.bbox] as BBox);
      if (e.shiftKey) { // resize from the bottom-right corner
        x1 = Math.max(x0 + 10, Math.min(pv.w, x1 + d[0] * step));
        y1 = Math.max(y0 + 10, Math.min(pv.h, y1 + d[1] * step));
      } else {
        const w = x1 - x0, hgt = y1 - y0;
        x0 = Math.max(0, Math.min(pv.w - w, x0 + d[0] * step)); y0 = Math.max(0, Math.min(pv.h - hgt, y0 + d[1] * step));
        x1 = x0 + w; y1 = y0 + hgt;
      }
      kbBox = [x0, y0, x1, y1];
      this.place(b, pv, kbBox);
      clearTimeout(kbTimer);
      kbTimer = setTimeout(() => { // commit once the keys are released for a moment
        const next = kbBox!.map((v) => Math.round(v * 100) / 100) as BBox;
        kbBox = null;
        this.ev.onBoxChange?.(t.id, next);
      }, 700);
    });
    b.addEventListener('pointerdown', (e) => {
      if (this.drawMode || e.button !== 0) return;
      e.stopPropagation();
      if (t.id !== this.active) { this.ev.onSelect?.(t.id); return; }
      e.preventDefault();
      const handle = (e.target as HTMLElement).dataset.h ?? 'move';
      const rect = pv.overlay.getBoundingClientRect();
      const k = pv.w / rect.width;
      const start = { x: e.clientX, y: e.clientY };
      const orig = [...t.bbox] as BBox;
      let next = orig;
      let moved = false;
      b.setPointerCapture(e.pointerId);
      b.classList.add('is-moving');
      const move = (ev: PointerEvent) => {
        const dx = (ev.clientX - start.x) * k, dy = (ev.clientY - start.y) * k;
        if (Math.abs(dx) + Math.abs(dy) > 1) moved = true;
        let [x0, y0, x1, y1] = orig;
        if (handle === 'move') {
          const w = x1 - x0, h = y1 - y0;
          x0 = Math.min(Math.max(0, x0 + dx), pv.w - w); y0 = Math.min(Math.max(0, y0 + dy), pv.h - h);
          x1 = x0 + w; y1 = y0 + h;
        } else {
          if (handle.includes('w')) x0 = Math.min(Math.max(0, x0 + dx), x1 - 10);
          if (handle.includes('e')) x1 = Math.max(Math.min(pv.w, x1 + dx), x0 + 10);
          if (handle.includes('n')) y0 = Math.min(Math.max(0, y0 + dy), y1 - 10);
          if (handle.includes('s')) y1 = Math.max(Math.min(pv.h, y1 + dy), y0 + 10);
        }
        next = [x0, y0, x1, y1];
        this.place(b, pv, next);
      };
      const up = () => {
        b.removeEventListener('pointermove', move);
        b.removeEventListener('pointerup', up);
        b.removeEventListener('pointercancel', up);
        b.classList.remove('is-moving');
        if (moved) this.ev.onBoxChange?.(t.id, next.map((v) => Math.round(v * 100) / 100) as BBox);
      };
      b.addEventListener('pointermove', move);
      b.addEventListener('pointerup', up);
      b.addEventListener('pointercancel', up);
    });
  }

  setDrawMode(on: boolean) {
    this.drawMode = on;
    if (on && this.textMode) this.setTextMode(false);
    for (const p of this.pages) {
      p.overlay.style.cursor = on ? 'crosshair' : '';
      p.overlay.style.pointerEvents = on ? 'auto' : ''; // otherwise only the boxes take the pointer: text stays selectable
      p.overlay.querySelectorAll<HTMLElement>('.tbox').forEach((b) => (b.style.pointerEvents = on ? 'none' : ''));
    }
  }

  private bindDraw(pv: PageView) {
    let start: { x: number; y: number } | null = null;
    let box: HTMLDivElement | null = null;
    const pos = (e: PointerEvent) => {
      const r = pv.overlay.getBoundingClientRect();
      return { x: Math.min(Math.max(e.clientX - r.left, 0), r.width), y: Math.min(Math.max(e.clientY - r.top, 0), r.height) };
    };
    pv.overlay.addEventListener('pointerdown', (e) => {
      if (!this.drawMode || e.button !== 0) return;
      e.preventDefault();
      pv.overlay.setPointerCapture(e.pointerId);
      start = pos(e);
      box = document.createElement('div');
      box.className = 'drawing';
      pv.overlay.append(box);
    });
    pv.overlay.addEventListener('pointermove', (e) => {
      if (!start || !box) return;
      const p = pos(e);
      Object.assign(box.style, {
        left: `${Math.min(start.x, p.x)}px`, top: `${Math.min(start.y, p.y)}px`,
        width: `${Math.abs(p.x - start.x)}px`, height: `${Math.abs(p.y - start.y)}px`,
      });
    });
    pv.overlay.addEventListener('pointerup', (e) => {
      if (!start || !box) return;
      const p = pos(e);
      const r = pv.overlay.getBoundingClientRect();
      box.remove();
      const s = start;
      start = null; box = null;
      if (Math.abs(p.x - s.x) < 12 || Math.abs(p.y - s.y) < 12) return;
      const k = pv.w / r.width;
      const bbox: BBox = [Math.min(s.x, p.x) * k, Math.min(s.y, p.y) * k, Math.max(s.x, p.x) * k, Math.max(s.y, p.y) * k];
      this.ev.onRegion?.(pv.n, bbox.map((v) => Math.round(v * 100) / 100) as BBox);
    });
  }

  /** temporary "working" marker while a region is being extracted */
  markBusy(page: number, bbox: BBox): () => void {
    const pv = this.pages[page - 1]!;
    const el = document.createElement('div');
    el.className = 'drawing animate-pulse';
    Object.assign(el.style, {
      left: `${(bbox[0] / pv.w) * 100}%`, top: `${(bbox[1] / pv.h) * 100}%`,
      width: `${((bbox[2] - bbox[0]) / pv.w) * 100}%`, height: `${((bbox[3] - bbox[1]) / pv.h) * 100}%`,
    });
    el.innerHTML = `<span class="absolute -top-5 left-0 bg-brand-600 text-white text-[10px] px-1.5 py-0.5">${esc('extracting…')}</span>`;
    pv.overlay.append(el);
    return () => el.remove();
  }
}
