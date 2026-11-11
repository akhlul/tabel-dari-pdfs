import type { DecimalSep, Merge, Table, Version } from './api';
import { icon } from './icons';
import { contextMenu, esc, toast, type MenuItem } from './ui';

// thousands may also be grouped by a space: 5 474 · 130 032 (older BPS publications)
const NUM = /^[(\[]?[-+−–]?\d+(?:[.,]\d+|[ \u00a0\u202f]\d{3}(?!\d))*%?[)\]]?\*{0,3}$|^[-–—]$/;

// ------------------------------------------------------------------ decimal separator detection
// (same rules as backend/app/exporters.py detect_decimal, so the UI shows what the export will use)
const TOKEN = /^\d+(?:[.,]\d+)+$/;
const WRAP = /^[(\[]?[-+−–]?|%?[)\]]?\*{0,3}$/g;

/** what one cell says about the decimal separator: [sep, strong] or null */
function vote(cell: string): [',' | '.', boolean] | null {
  const t = (cell ?? '').trim().replace(/[\u00a0\u202f ]/g, '').replace(WRAP, '');
  if (!TOKEN.test(t)) return null;
  const seps = [...t].filter((ch) => ch === '.' || ch === ',') as (',' | '.')[];
  const groups = t.split(/[.,]/);
  const g3 = (gs: string[]) => gs.every((g) => g.length === 3);
  if (new Set(seps).size === 2) { // 10.498,2 · 1,234.5: the last one is the decimal
    const dec = seps.at(-1)!;
    if (seps.filter((s) => s === dec).length !== 1 || groups[0]!.length > 3 || !g3(groups.slice(1, -1))) return null;
    return [dec, true];
  }
  const s = seps[0]!, other = s === '.' ? ',' : '.';
  if (seps.length > 1) return groups[0]!.length > 3 || !g3(groups.slice(1)) ? null : [other, true]; // 1.234.567
  const [head, tail] = groups as [string, string];
  if (tail.length !== 3 || head.length > 3 || head === '0') return [s, true]; // 25,9 · 405.5 · 0,125
  return [other, false]; // 1.234: probably thousands
}

export interface DecimalGuess { decimal: ',' | '.'; sure: boolean; strong: number; weak: number }

/** guess the decimal separator from the body cells; strong votes decide, weak ones break ties, default ',' */
export function detectDecimal(rows: string[][], headerRows = 0, fallback: ',' | '.' = ','): DecimalGuess {
  const body = rows.slice(headerRows).length ? rows.slice(headerRows) : rows;
  const strong = { ',': 0, '.': 0 }, weak = { ',': 0, '.': 0 };
  for (const r of body) for (const c of r) { const v = vote(c); if (v) (v[1] ? strong : weak)[v[0]]++; }
  const pick = (o: typeof strong) => (o[','] > o['.'] ? ',' : '.') as ',' | '.';
  const n = { strong: strong[','] + strong['.'], weak: weak[','] + weak['.'] };
  if (strong[','] !== strong['.']) return { decimal: pick(strong), sure: true, ...n };
  if (weak[','] !== weak['.']) return { decimal: pick(weak), sure: false, ...n };
  return { decimal: fallback, sure: false, ...n };
}

const SEP_LABEL = { ',': '1.234,5', '.': '1,234.5' } as const;
export const colName = (j: number) => {
  let s = '';
  for (let n = j + 1; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + ((n - 1) % 26)) + s;
  return s;
};
const plaintextOnly = (() => {
  const d = document.createElement('div');
  d.contentEditable = 'plaintext-only';
  return d.contentEditable === 'plaintext-only';
})();

export interface EditorChange {
  rows?: string[][]; merges?: Merge[]; header_rows?: number; title?: string; notes?: string;
  decimal_sep?: DecimalSep;
  checkpoint?: boolean; // structural edit: server keeps the previous state in history
}

interface Snapshot { rows: string[][]; merges: Merge[]; header: number }
interface Range { r0: number; c0: number; r1: number; c1: number }
type Cell = { r: number; c: number };

const clone = (s: Snapshot): Snapshot => ({ rows: s.rows.map((r) => [...r]), merges: s.merges.map((m) => [...m] as Merge), header: s.header });
const same = (a: Snapshot, b: Snapshot) => JSON.stringify(a) === JSON.stringify(b);

/**
 * Spreadsheet-like editor for one table: edit cells, select ranges (drag / shift-click / row & column
 * headers), merge & unmerge cells, insert & delete rows / columns, undo / redo (seeded with saved versions).
 */
export class TableEditor {
  rows: string[][] = [];
  merges: Merge[] = [];
  header = 1;
  title = '';
  notes = '';
  /** decimal separator used on export: 'auto' = detectDecimal() */
  decimalSep: DecimalSep = 'auto';
  readonly = false;
  /** joined tables: page + row count of each appended part (first part = the remaining top rows) */
  private parts: { page: number; rows: number; cols: number }[] = [];
  private firstPage = 0;
  /** "show this page in the PDF" (context menu) */
  onShowPage?: (page: number) => void;
  private orig: string[][] = [];
  private undoStack: Snapshot[] = [];
  private redoStack: Snapshot[] = [];
  private cur: Cell = { r: 0, c: 0 };
  private anchor: Cell = { r: 0, c: 0 };
  private range: Range | null = null;
  private dragging = false;
  private dirtyCell = false;
  private grid!: HTMLElement;

  constructor(private root: HTMLElement, private onChange: (c: EditorChange) => void) {
    root.addEventListener('click', (e) => {
      const op = (e.target as HTMLElement).closest<HTMLElement>('[data-op]')?.dataset.op;
      if (!op || this.readonly) return;
      ({
        undo: () => this.undo(), redo: () => this.redo(),
        'row-above': () => this.insertRow(false), 'row-below': () => this.insertRow(true),
        'row-del': () => this.deleteRows(), 'merge-up': () => this.joinUp(),
        'col-left': () => this.insertCol(false), 'col-right': () => this.insertCol(true),
        'col-del': () => this.deleteCols(), 'merge-left': () => this.joinLeft(),
        merge: () => this.mergeCells(), unmerge: () => this.unmergeCells(),
        clean: () => this.removeEmpty(), transpose: () => this.transpose(), clear: () => this.clearCells(),
        'hdr-dec': () => this.setHeader(this.header - 1), 'hdr-inc': () => this.setHeader(this.header + 1),
      } as Record<string, () => void>)[op]?.();
    });
    addEventListener('mouseup', () => (this.dragging = false));
  }

  open(t: Table, versions: Version[] = [], readonly = false) {
    this.rows = t.rows.map((r) => [...r]);
    this.merges = (t.merges ?? []).map((m) => [...m] as Merge);
    this.orig = t.original_rows;
    this.header = t.header_rows;
    this.title = t.title;
    this.notes = t.notes ?? '';
    this.decimalSep = t.decimal_sep ?? 'auto';
    this.readonly = readonly;
    // column parts add columns on the right (plus, rarely, rows for labels the first part lacks)
    this.parts = (t.parts ?? []).map((p) => (p.mode === 'columns'
      ? { page: p.page, rows: p.extra_rows ?? 0, cols: p.cols ?? 0 } : { page: p.page, rows: p.rows, cols: 0 }));
    this.firstPage = t.page;
    // saved history becomes the undo stack, so "previous version" survives a reload
    this.undoStack = [];
    for (const v of versions) {
      const s: Snapshot = { rows: v.rows, merges: v.merges ?? [], header: v.header_rows };
      if (!this.undoStack.length || !same(this.undoStack.at(-1)!, s)) this.undoStack.push(s);
    }
    while (this.undoStack.length && same(this.undoStack.at(-1)!, this.snapshot())) this.undoStack.pop();
    this.redoStack = [];
    this.cur = { r: 0, c: 0 };
    this.anchor = { r: 0, c: 0 };
    this.range = null;
    this.render();
  }

  private width() { return Math.max(1, ...this.rows.map((r) => r.length)); }
  private snapshot(): Snapshot { return clone({ rows: this.rows, merges: this.merges, header: this.header }); }

  private push() {
    this.undoStack.push(this.snapshot());
    if (this.undoStack.length > 300) this.undoStack.shift();
    this.redoStack = [];
  }

  undo() {
    this.commitCell();
    const s = this.undoStack.pop();
    if (!s) return;
    this.redoStack.push(this.snapshot());
    this.restore(s);
  }

  redo() {
    const s = this.redoStack.pop();
    if (!s) return;
    this.undoStack.push(this.snapshot());
    this.restore(s);
  }

  private restore(s: Snapshot) {
    this.rows = s.rows.map((r) => [...r]);
    this.merges = s.merges.map((m) => [...m] as Merge);
    this.header = s.header;
    this.range = null;
    this.clampCursor();
    this.renderGrid();
    this.emit(false);
  }

  private emit(checkpoint: boolean) {
    this.onChange({ rows: this.rows, merges: this.merges, header_rows: this.header, checkpoint });
  }

  private clampCursor() {
    this.cur.r = Math.max(0, Math.min(this.cur.r, this.rows.length - 1));
    this.cur.c = Math.max(0, Math.min(this.cur.c, this.width() - 1));
  }

  /** structural edit helper: snapshot, mutate, normalise, re-render, save with a history checkpoint */
  private edit(fn: () => void) {
    if (this.readonly) return;
    this.commitCell();
    const before = this.snapshot();
    fn();
    const w = this.width();
    this.rows = this.rows.map((r) => [...r, ...Array(Math.max(0, w - r.length)).fill('')]);
    if (!this.rows.length) this.rows = [['']];
    this.header = Math.max(0, Math.min(this.header, this.rows.length));
    this.merges = this.merges.filter(([r, c, rs, cs]) => rs >= 1 && cs >= 1 && (rs > 1 || cs > 1)
      && r + rs <= this.rows.length && c + cs <= this.width());
    this.clampCursor();
    if (same(before, this.snapshot())) { this.renderGrid(); return; }
    this.undoStack.push(before);
    this.redoStack = [];
    this.renderGrid();
    this.emit(true);
  }

  private commitCell() {
    if (this.dirtyCell) this.dirtyCell = false;
  }

  // ------------------------------------------------------------------ joined parts
  /** row ranges per source page (counts from the join; later row inserts/deletes shift the first part) */
  segments(): { page: number; start: number; end: number }[] {
    if (!this.parts.length) return [];
    const tail = this.parts.reduce((n, p) => n + p.rows, 0);
    let start = Math.max(0, this.rows.length - tail);
    const out = [{ page: this.firstPage, start: 0, end: start - 1 }];
    for (const p of this.parts) { out.push({ page: p.page, start, end: Math.min(this.rows.length, start + p.rows) - 1 }); start += p.rows; }
    return out.filter((x) => x.end >= x.start);
  }
  pageOfRow(r: number) { return this.segments().find((x) => r >= x.start && r <= x.end)?.page ?? this.firstPage; }
  /** column ranges of parts joined side by side (counted from the right edge) */
  colSegments(): { page: number; start: number; end: number }[] {
    const cols = this.parts.filter((p) => p.cols > 0);
    if (!cols.length) return [];
    const w = this.width();
    let start = Math.max(0, w - cols.reduce((n, p) => n + p.cols, 0));
    const out: { page: number; start: number; end: number }[] = [];
    for (const p of cols) { out.push({ page: p.page, start, end: Math.min(w, start + p.cols) - 1 }); start += p.cols; }
    return out.filter((x) => x.end >= x.start);
  }
  pageOfCell(r: number, c: number) {
    return this.colSegments().find((x) => c >= x.start && c <= x.end)?.page ?? this.pageOfRow(r);
  }

  /** select and scroll to the rows (or columns) that came from `page` */
  reveal(page: number) {
    const col = this.colSegments().find((x) => x.page === page);
    if (col && this.grid) {
      this.cur = { r: 0, c: col.start };
      this.anchor = { ...this.cur };
      this.range = { r0: 0, c0: col.start, r1: Math.max(0, this.rows.length - 1), c1: col.end };
      this.paintSelection();
      const th = this.grid.querySelector<HTMLElement>(`thead th[data-col="${col.start}"]`);
      if (th) this.grid.scrollTo({ left: Math.max(0, th.offsetLeft - 48), behavior: 'smooth' });
      return;
    }
    const seg = this.segments().find((x) => x.page === page);
    if (!seg || !this.grid) return;
    this.cur = { r: seg.start, c: 0 };
    this.anchor = { ...this.cur };
    this.range = { r0: seg.start, c0: 0, r1: seg.end, c1: this.width() - 1 };
    this.paintSelection();
    const th = this.grid.querySelector<HTMLElement>(`th[data-row="${seg.start}"]`);
    const head = this.grid.querySelector('thead')?.getBoundingClientRect().height ?? 24;
    if (th) {
      this.grid.scrollTo({ top: Math.max(0, th.parentElement!.offsetTop - head - 2), behavior: 'smooth' });
      const tr = th.parentElement!;
      tr.classList.remove('seg-flash'); void tr.offsetWidth; tr.classList.add('seg-flash');
    }
  }

  // ------------------------------------------------------------------ selection
  /** current selection (range or the active cell), grown to whole merged cells */
  sel(): Range {
    const r = this.range ?? { r0: this.cur.r, c0: this.cur.c, r1: this.cur.r, c1: this.cur.c };
    let { r0, c0, r1, c1 } = { r0: Math.min(r.r0, r.r1), c0: Math.min(r.c0, r.c1), r1: Math.max(r.r0, r.r1), c1: Math.max(r.c0, r.c1) };
    for (let changed = true; changed;) {
      changed = false;
      for (const [mr, mc, rs, cs] of this.merges) {
        if (mr <= r1 && mr + rs - 1 >= r0 && mc <= c1 && mc + cs - 1 >= c0) {
          const n = { r0: Math.min(r0, mr), c0: Math.min(c0, mc), r1: Math.max(r1, mr + rs - 1), c1: Math.max(c1, mc + cs - 1) };
          if (n.r0 !== r0 || n.c0 !== c0 || n.r1 !== r1 || n.c1 !== c1) { ({ r0, c0, r1, c1 } = n); changed = true; }
        }
      }
    }
    return { r0, c0, r1, c1 };
  }

  private setRange(a: Cell, b: Cell) {
    this.range = a.r === b.r && a.c === b.c ? null : { r0: a.r, c0: a.c, r1: b.r, c1: b.c };
    this.paintSelection();
  }

  private paintSelection() {
    const s = this.sel();
    const multi = !!this.range;
    this.grid.querySelectorAll<HTMLElement>('td').forEach((td) => {
      const d = td.firstElementChild as HTMLElement;
      const r = +d.dataset.r!, c = +d.dataset.c!;
      td.classList.toggle('is-sel', multi && r >= s.r0 && r <= s.r1 && c >= s.c0 && c <= s.c1);
    });
    this.grid.querySelectorAll<HTMLElement>('th[data-col]').forEach((th) => {
      const j = +th.dataset.col!;
      th.classList.toggle(multi ? 'is-hi' : 'is-cur', j >= s.c0 && j <= s.c1);
      th.classList.remove(multi ? 'is-cur' : 'is-hi');
    });
    this.grid.querySelectorAll<HTMLElement>('th[data-row]').forEach((th) => {
      const i = +th.dataset.row!;
      th.classList.toggle(multi ? 'is-hi' : 'is-cur', i >= s.r0 && i <= s.r1);
      th.classList.remove(multi ? 'is-cur' : 'is-hi');
    });
    this.status();
  }

  // ------------------------------------------------------------------ merge bookkeeping
  /** keep only the given old row / column indices (in order) and remap spans onto the new grid */
  private remap(keepR: number[], keepC: number[]) {
    const rpos = new Map(keepR.map((v, i) => [v, i])), cpos = new Map(keepC.map((v, i) => [v, i]));
    const out: Merge[] = [];
    for (const [r, c, rs, cs] of this.merges) {
      const rr: number[] = [], cc: number[] = [];
      for (let i = r; i < r + rs; i++) if (rpos.has(i)) rr.push(rpos.get(i)!);
      for (let j = c; j < c + cs; j++) if (cpos.has(j)) cc.push(cpos.get(j)!);
      if (rr.length && cc.length) out.push([rr[0]!, cc[0]!, rr.at(-1)! - rr[0]! + 1, cc.at(-1)! - cc[0]! + 1]);
    }
    this.rows = keepR.map((i) => keepC.map((j) => this.rows[i]?.[j] ?? ''));
    this.merges = out.filter((m) => m[2] > 1 || m[3] > 1);
  }

  private shiftForInsert(axis: 'row' | 'col', at: number) {
    const [p, s] = (axis === 'row' ? [0, 2] : [1, 3]) as [0 | 1, 2 | 3];
    for (const m of this.merges) {
      if (m[p] >= at) m[p]++;
      else if (at < m[p] + m[s]) m[s]++; // insertion inside a span widens it
    }
  }

  // ------------------------------------------------------------------ ops
  insertRow(after: boolean) {
    const s = this.sel();
    const at = after ? s.r1 + 1 : s.r0;
    this.edit(() => {
      this.rows.splice(at, 0, Array(this.width()).fill(''));
      this.shiftForInsert('row', at);
      if (at < this.header) this.header++;
      this.cur = { r: at, c: this.cur.c };
      this.range = null;
    });
  }
  deleteRows() {
    const s = this.sel();
    if (s.r1 - s.r0 + 1 >= this.rows.length) return;
    this.edit(() => {
      const keep = this.rows.map((_, i) => i).filter((i) => i < s.r0 || i > s.r1);
      const removedHead = Math.max(0, Math.min(this.header, s.r1 + 1) - s.r0);
      this.remap(keep, Array.from({ length: this.width() }, (_, j) => j));
      this.header -= removedHead;
      this.cur = { r: s.r0, c: this.cur.c };
      this.range = null;
    });
  }
  insertCol(after: boolean) {
    const s = this.sel();
    const at = after ? s.c1 + 1 : s.c0;
    this.edit(() => {
      this.rows.forEach((r) => r.splice(at, 0, ''));
      this.shiftForInsert('col', at);
      this.cur = { r: this.cur.r, c: at };
      this.range = null;
    });
  }
  deleteCols() {
    const s = this.sel();
    if (s.c1 - s.c0 + 1 >= this.width()) return;
    this.edit(() => {
      const keep = Array.from({ length: this.width() }, (_, j) => j).filter((j) => j < s.c0 || j > s.c1);
      this.remap(this.rows.map((_, i) => i), keep);
      this.cur = { r: this.cur.r, c: s.c0 };
      this.range = null;
    });
  }
  /** merge the selected cells into one (text of all cells joined into the top-left cell) */
  mergeCells() {
    const s = this.sel();
    if (s.r0 === s.r1 && s.c0 === s.c1) return;
    this.edit(() => {
      const texts: string[] = [];
      for (let i = s.r0; i <= s.r1; i++) for (let j = s.c0; j <= s.c1; j++) {
        const v = (this.rows[i]![j] ?? '').trim();
        if (v && !texts.includes(v)) texts.push(v);
        this.rows[i]![j] = '';
      }
      this.rows[s.r0]![s.c0] = texts.join(' ');
      this.merges = this.merges.filter(([r, c, rs, cs]) => !(r <= s.r1 && r + rs - 1 >= s.r0 && c <= s.c1 && c + cs - 1 >= s.c0));
      this.merges.push([s.r0, s.c0, s.r1 - s.r0 + 1, s.c1 - s.c0 + 1]);
      this.cur = { r: s.r0, c: s.c0 };
      this.range = null;
    });
  }
  unmergeCells() {
    const s = this.sel();
    this.edit(() => {
      this.merges = this.merges.filter(([r, c, rs, cs]) => !(r <= s.r1 && r + rs - 1 >= s.r0 && c <= s.c1 && c + cs - 1 >= s.c0));
    });
  }
  /** fold the selected row(s) into the row above (wrapped labels) */
  joinUp() {
    const i = this.sel().r0;
    if (i === 0) return;
    this.edit(() => {
      const a = this.rows[i - 1]!, b = this.rows[i]!;
      this.rows[i - 1] = a.map((v, j) => [v, b[j] ?? ''].filter(Boolean).join(' '));
      const keep = this.rows.map((_, k) => k).filter((k) => k !== i);
      this.remap(keep, Array.from({ length: this.width() }, (_, j) => j));
      if (i < this.header) this.header--;
      this.cur = { r: i - 1, c: this.cur.c };
      this.range = null;
    });
  }
  /** fold the selected column into the one on its left */
  joinLeft() {
    const j = this.sel().c0;
    if (j === 0) return;
    this.edit(() => {
      this.rows.forEach((r) => { r[j - 1] = [r[j - 1], r[j]].filter(Boolean).join(' '); });
      const keep = Array.from({ length: this.width() }, (_, k) => k).filter((k) => k !== j);
      this.remap(this.rows.map((_, k) => k), keep);
      this.cur = { r: this.cur.r, c: j - 1 };
      this.range = null;
    });
  }
  removeEmpty() {
    this.edit(() => {
      const keepR = this.rows.map((_, i) => i).filter((i) => this.rows[i]!.some((c) => c.trim()));
      const keepC = Array.from({ length: this.width() }, (_, j) => j).filter((j) => this.rows.some((r) => (r[j] ?? '').trim()));
      this.remap(keepR, keepC);
      this.range = null;
    });
  }
  transpose() {
    this.edit(() => {
      const w = this.width();
      this.rows = Array.from({ length: w }, (_, j) => this.rows.map((r) => r[j] ?? ''));
      this.merges = this.merges.map(([r, c, rs, cs]) => [c, r, cs, rs] as Merge);
      this.header = 1;
      this.range = null;
    });
  }
  setHeader(n: number) {
    n = Math.max(0, Math.min(this.rows.length, n));
    if (n === this.header || this.readonly) return;
    this.push();
    this.header = n;
    this.renderGrid();
    this.emit(false);
  }

  clearCells() {
    const s = this.sel();
    this.edit(() => { for (let i = s.r0; i <= s.r1; i++) for (let j = s.c0; j <= s.c1; j++) this.rows[i]![j] = ''; });
  }
  /** rows 1..(last row of the selection) become the header */
  headerThrough() { this.setHeader(this.sel().r1 + 1); }

  selectionTSV() {
    const s = this.sel();
    return this.rows.slice(s.r0, s.r1 + 1).map((r) => r.slice(s.c0, s.c1 + 1)
      .map((c) => (/[\t\n"]/.test(c) ? `"${c.replace(/"/g, '""')}"` : c)).join('\t')).join('\n');
  }

  pasteAt(text: string) {
    const block = parseTSV(text.replace(/\n$/, ''));
    const at = this.sel();
    this.edit(() => {
      block.forEach((row, i) => {
        while (this.rows.length <= at.r0 + i) this.rows.push(Array(this.width()).fill(''));
        row.forEach((v, j) => { const tr = this.rows[at.r0 + i]!; while (tr.length <= at.c0 + j) tr.push(''); tr[at.c0 + j] = v; });
      });
    });
  }

  private menu(e: MouseEvent) {
    const s = this.sel();
    const rows = s.r1 - s.r0 + 1, cols = s.c1 - s.c0 + 1;
    const multi = rows > 1 || cols > 1;
    const inMerge = this.merges.some(([r, c, rs, cs]) => r <= s.r1 && r + rs - 1 >= s.r0 && c <= s.c1 && c + cs - 1 >= s.c0);
    const ref = `${colName(s.c0)}${s.r0 + 1}${multi ? `:${colName(s.c1)}${s.r1 + 1}` : ''}`;
    const ro = this.readonly;
    const items: MenuItem[] = [
      { heading: `${ref} · ${rows} row${rows > 1 ? 's' : ''} × ${cols} col${cols > 1 ? 's' : ''}` },
      { label: 'Copy', icon: 'copy', hint: 'Ctrl+C', action: () => navigator.clipboard.writeText(this.selectionTSV()).then(() => toast(`Copied ${rows * cols} cell${rows * cols > 1 ? 's' : ''}`, 'ok')) },
      { label: 'Paste', icon: 'download', disabled: ro, action: async () => {
        try { this.pasteAt(await navigator.clipboard.readText()); } catch { toast('The browser blocked the clipboard. Click a cell and press Ctrl+V instead.', 'error'); }
      } },
      { label: 'Clear Contents', icon: 'x', hint: 'Del', disabled: ro, action: () => this.clearCells() },
      'sep',
      { label: 'Merge Cells', icon: 'mergeCells', hint: 'Ctrl+M', disabled: ro || !multi, action: () => this.mergeCells() },
      { label: 'Unmerge Cells', icon: 'splitCells', disabled: ro || !inMerge, action: () => this.unmergeCells() },
      'sep',
      { label: 'Insert Row Above', icon: 'rowAbove', disabled: ro, action: () => this.insertRow(false) },
      { label: 'Insert Row Below', icon: 'rowBelow', disabled: ro, action: () => this.insertRow(true) },
      { label: rows > 1 ? `Delete ${rows} Rows` : 'Delete Row', icon: 'rowDel', danger: true, disabled: ro || rows >= this.rows.length, action: () => this.deleteRows() },
      { label: 'Join Row into Row Above', icon: 'merge', disabled: ro || s.r0 === 0, action: () => this.joinUp() },
      'sep',
      { label: 'Insert Column Left', icon: 'colLeft', disabled: ro, action: () => this.insertCol(false) },
      { label: 'Insert Column Right', icon: 'colRight', disabled: ro, action: () => this.insertCol(true) },
      { label: cols > 1 ? `Delete ${cols} Columns` : 'Delete Column', icon: 'colDel', danger: true, disabled: ro || cols >= this.width(), action: () => this.deleteCols() },
      { label: 'Join Column into Left Column', icon: 'merge', disabled: ro || s.c0 === 0, action: () => this.joinLeft() },
      'sep',
      ...(this.onShowPage ? [{ label: `Show Page ${this.pageOfRow(s.r0)} in PDF`, icon: 'target', action: () => this.onShowPage!(this.pageOfRow(s.r0)) } as MenuItem] : []),
      { label: `Use Rows 1–${s.r1 + 1} as Header`, icon: 'layers', disabled: ro, action: () => this.headerThrough() },
      { label: 'Undo', icon: 'undo', hint: 'Ctrl+Z', disabled: ro || !this.undoStack.length, action: () => this.undo() },
      { label: 'Redo', icon: 'redo', hint: 'Ctrl+Y', disabled: ro || !this.redoStack.length, action: () => this.redo() },
    ];
    contextMenu(e, items);
  }

  toTSV() {
    return this.rows.map((r) => r.map((c) => (/[\t\n"]/.test(c) ? `"${c.replace(/"/g, '""')}"` : c)).join('\t')).join('\n');
  }

  // ------------------------------------------------------------------ render
  render() {
    const ro = this.readonly;
    this.root.innerHTML = `
      <div class="flex flex-col h-full min-h-0">
        <div class="px-3 pt-2.5 pb-2 border-b border-ink-100 space-y-1.5">
          <div class="flex items-start gap-2">
            <span class="label !mb-0 pt-1.5 w-11 shrink-0" aria-hidden="true">Title</span>
            <textarea data-title rows="1" ${ro ? 'readonly' : ''} class="flex-1 resize-none text-[13.5px] font-semibold text-ink-900 leading-snug bg-transparent border border-transparent
              hover:border-ink-200 focus:border-brand-500 focus:bg-surface focus:outline-none px-1.5 py-1"
              aria-label="Table title" placeholder="Table title, for example Tabel 2.1 Panjang Jalan">${esc(this.title)}</textarea>
          </div>
          <div class="flex items-start gap-2">
            <span class="label !mb-0 pt-1.5 w-11 shrink-0" aria-hidden="true">Notes</span>
            <textarea data-notes rows="1" ${ro ? 'readonly' : ''} class="flex-1 resize-none text-[12px] text-ink-600 leading-snug bg-transparent border border-transparent
              hover:border-ink-200 focus:border-brand-500 focus:bg-surface focus:outline-none px-1.5 py-1"
              aria-label="Notes" placeholder="Source and footnotes, for example Sumber: BPS">${esc(this.notes)}</textarea>
          </div>
        </div>
        <div role="toolbar" aria-label="Cells, rows and columns" class="flex flex-wrap items-center gap-md px-2 min-h-10 py-1 border-b border-ink-200 bg-ink-50 ${ro ? 'opacity-50 pointer-events-none' : ''}">
          ${this.tool('undo', 'undo', 'Undo (Ctrl+Z)')}${this.tool('redo', 'redo', 'Redo (Ctrl+Y)')}
          <span class="w-px h-5 bg-ink-200" role="separator"></span>
          ${this.group('Cells', 'mergeCells', [['merge', 'mergeCells', 'Merge Cells', 'Ctrl+M'], ['unmerge', 'splitCells', 'Unmerge Cells', ''], ['clear', 'x', 'Clear Contents', 'Del']])}
          ${this.group('Rows', 'rowBelow', [['row-above', 'rowAbove', 'Insert Row Above', ''], ['row-below', 'rowBelow', 'Insert Row Below', ''], ['row-del', 'rowDel', 'Delete Rows', ''], ['merge-up', 'merge', 'Join Row into Row Above', '']])}
          ${this.group('Columns', 'colRight', [['col-left', 'colLeft', 'Insert Column Left', ''], ['col-right', 'colRight', 'Insert Column Right', ''], ['col-del', 'colDel', 'Delete Columns', ''], ['merge-left', 'merge', 'Join Column into Left Column', '']])}
          ${this.group('Table', 'table', [['clean', 'wand', 'Remove Empty Rows and Columns', ''], ['transpose', 'layers', 'Transpose', '']])}
          <span class="w-px h-5 bg-ink-200" role="separator"></span>
          <span class="flex items-center gap-md text-[12px] text-ink-600 whitespace-nowrap" title="How many rows at the top form the header">
            <span id="hdr-l">Header rows:</span>
            <span class="flex items-center border border-ink-200 bg-surface" role="group" aria-labelledby="hdr-l">
              <button data-op="hdr-dec" class="w-6 h-6 hover:bg-ink-100" aria-label="Fewer header rows">−</button>
              <span data-hdr class="w-6 text-center font-medium text-ink-900 tabular-nums" aria-live="polite">${this.header}</span>
              <button data-op="hdr-inc" class="w-6 h-6 hover:bg-ink-100" aria-label="More header rows">+</button>
            </span>
          </span>
          <span class="w-px h-5 bg-ink-200" role="separator"></span>
          <label class="flex items-center gap-md text-[12px] text-ink-600 whitespace-nowrap">
            <span>Decimal:</span>
            <select data-decimal ${ro ? 'disabled' : ''} class="field h-6 text-[12px] px-1" aria-label="Decimal separator used when numbers are exported">
              <option value="auto"></option>
              <option value=",">Comma · ${SEP_LABEL[',']}</option>
              <option value=".">Dot · ${SEP_LABEL['.']}</option>
            </select>
          </label>
        </div>
        <div data-grid class="flex-1 min-h-0 overflow-auto bg-surface"></div>
        <div data-status class="flex items-center gap-3 px-3 h-7 border-t border-ink-200 text-[11px] text-ink-500 bg-ink-50/70 whitespace-nowrap overflow-hidden"></div>
      </div>`;
    this.grid = this.root.querySelector('[data-grid]')!;
    for (const key of ['title', 'notes'] as const) {
      const ta = this.root.querySelector<HTMLTextAreaElement>(`[data-${key}]`)!;
      const grow = () => { ta.style.height = 'auto'; ta.style.height = `${Math.min(ta.scrollHeight + 2, key === 'notes' ? 140 : 80)}px`; };
      requestAnimationFrame(grow);
      ta.addEventListener('input', grow);
      ta.addEventListener('change', () => { this[key] = ta.value.trim(); this.onChange({ [key]: this[key] }); });
      if (key === 'title') ta.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); ta.blur(); } });
    }
    this.root.querySelectorAll<HTMLButtonElement>('[data-op]').forEach((b) =>
      b.addEventListener('mousedown', (e) => e.preventDefault()), // keep cell focus
    );
    const dec = this.root.querySelector<HTMLSelectElement>('[data-decimal]')!;
    dec.value = this.decimalSep;
    dec.addEventListener('change', () => {
      this.decimalSep = dec.value as DecimalSep;
      this.updateDecimal();
      this.onChange({ decimal_sep: this.decimalSep });
    });
    this.bindGrid();
    this.renderGrid();
  }

  private group(name: string, ic: string, items: [string, string, string, string][]) {
    return `<div class="relative" data-menu>
      <button class="btn-ghost btn-sm" data-menu-toggle>${icon(ic, 'size-4')}${name}${icon('chevron', 'size-3')}</button>
      <div class="menu hidden left-0 w-64">${items.map(([op, i, label, kbd]) =>
        `<button data-op="${op}" class="menu-item">${icon(i, 'size-4 text-ink-500')}<span class="flex-1">${label}</span>${kbd ? `<span class="text-[11px] text-ink-500">${kbd}</span>` : ''}</button>`).join('')}</div></div>`;
  }

  /** separator the export will use (the table's own choice, or the detected one) */
  decimal(): ',' | '.' {
    return this.decimalSep === 'auto' ? detectDecimal(this.rows, this.header).decimal : this.decimalSep;
  }

  /** refresh the "Auto (detected …)" option after cells or header rows change */
  private updateDecimal() {
    const sel = this.root.querySelector<HTMLSelectElement>('[data-decimal]');
    if (!sel) return;
    const g = detectDecimal(this.rows, this.header);
    const name = g.decimal === ',' ? 'comma' : 'dot';
    sel.options[0]!.textContent = `Auto · ${SEP_LABEL[g.decimal]}${g.sure ? '' : '?'}`;
    sel.title = g.strong
      ? `Detected ${name} as the decimal separator from ${g.strong} number${g.strong > 1 ? 's' : ''}`
      : g.weak
        ? `Probably ${name}: only ambiguous numbers like 1.234 found. Choose Comma or Dot to be sure.`
        : `No decimal numbers found, using ${name}`;
    sel.classList.toggle('text-amber-700', this.decimalSep === 'auto' && !g.sure && g.weak > 0);
  }

  private tool(ic: string, op: string, title: string) {
    return `<button data-op="${op}" class="btn-ghost btn-sm btn-icon" title="${esc(title)}">${icon(ic, 'size-4')}</button>`;
  }

  private renderGrid() {
    const w = this.width();
    const sameShape = this.orig.length === this.rows.length && this.orig.every((r) => r.length === w);
    const ce = this.readonly ? 'false' : plaintextOnly ? 'plaintext-only' : 'true';
    const origin = new Map<string, Merge>();
    const covered = new Set<string>();
    for (const m of this.merges) {
      origin.set(`${m[0]},${m[1]}`, m);
      for (let i = m[0]; i < m[0] + m[2]; i++) for (let j = m[1]; j < m[1] + m[3]; j++) if (i !== m[0] || j !== m[1]) covered.add(`${i},${j}`);
    }
    let html = '<table aria-label="Table cells" class="grid-editor text-[12.5px] w-max min-w-full"><thead><tr><th class="sticky top-0 left-0 z-20 bg-ink-100 w-9 min-w-9"><span class="sr-only">Row</span></th>';
    for (let j = 0; j < w; j++) {
      html += `<th data-col="${j}" class="sticky top-0 z-10 h-6 px-2 text-[10.5px] font-semibold select-none bg-ink-100 text-ink-500 hover:bg-ink-200">${colName(j)}</th>`;
    }
    html += '</tr></thead><tbody>';
    const segStart = new Map(this.segments().map((x) => [x.start, x.page]));
    this.rows.forEach((r, i) => {
      const head = i < this.header;
      const sp = segStart.get(i);
      html += `<tr class="${head ? 'is-head' : ''} ${sp !== undefined ? 'seg-start' : ''}" ${sp !== undefined ? `data-seg="${sp}"` : ''}><th data-row="${i}" class="sticky left-0 z-10 px-1.5 text-[10.5px] font-semibold text-right select-none
        ${head ? 'bg-brand-100 text-brand-700' : 'bg-ink-50 text-ink-400 hover:bg-ink-100'}">${sp !== undefined ? `<span class="seg-tag" title="Rows from page ${sp} start here">p${sp}</span>` : ''}${i + 1}</th>`;
      for (let j = 0; j < w; j++) {
        if (covered.has(`${i},${j}`)) continue;
        const m = origin.get(`${i},${j}`);
        const v = r[j] ?? '';
        const cls = [
          !head && NUM.test(v.trim()) ? 'is-num' : '',
          m ? 'is-merged' : '',
          sameShape && this.orig[i]![j] !== v ? 'is-changed' : '',
        ].join(' ');
        const span = m ? ` rowspan="${m[2]}" colspan="${m[3]}"` : '';
        html += `<td class="align-top ${cls}"${span}><div contenteditable="${ce}" spellcheck="false" data-r="${i}" data-c="${j}"
          class="px-2 py-1 min-w-12 max-w-72 min-h-[26px] whitespace-pre-wrap break-words focus:outline-none">${esc(v)}</div></td>`;
      }
      html += '</tr>';
    });
    html += '</tbody></table>';
    this.grid.innerHTML = html;
    const hdr = this.root.querySelector('[data-hdr]');
    if (hdr) hdr.textContent = String(this.header);
    this.updateDecimal();
    this.paintSelection();
  }

  private status() {
    const s = this.root.querySelector('[data-status]');
    if (!s) return;
    const w = this.width();
    const sel = this.sel();
    const ref = `${colName(sel.c0)}${sel.r0 + 1}${sel.r1 !== sel.r0 || sel.c1 !== sel.c0 ? `:${colName(sel.c1)}${sel.r1 + 1}` : ''}`;
    s.innerHTML = `<span>${this.rows.length} rows × ${w} cols</span>
      ${this.merges.length ? `<span>${this.merges.length} merged</span>` : ''}
      <span class="font-medium text-ink-700">${ref}</span>
      ${this.parts.length ? `<span class="chip border-brand-200 text-brand-700 bg-brand-50">from page ${this.pageOfCell(sel.r0, sel.c0)}</span>` : ''}
      <span>${this.undoStack.length ? `${this.undoStack.length} undo step${this.undoStack.length > 1 ? 's' : ''}` : ''}</span>
      <span class="ml-auto hidden 2xl:inline">Drag or Shift+click to select · right-click for more</span>`;
  }

  focusCell(r: number, c: number) {
    // a covered cell focuses its merge origin
    for (const [mr, mc, rs, cs] of this.merges) if (r >= mr && r < mr + rs && c >= mc && c < mc + cs) { r = mr; c = mc; }
    const el = this.grid.querySelector<HTMLElement>(`[data-r="${r}"][data-c="${c}"]`);
    if (!el) return;
    el.focus();
    const range = document.createRange();
    range.selectNodeContents(el);
    range.collapse(false);
    const s = getSelection();
    s?.removeAllRanges();
    s?.addRange(range);
  }

  private cellOf(t: EventTarget | null): (Cell & { el: HTMLElement }) | null {
    const td = (t as HTMLElement)?.closest?.('td');
    const el = td?.querySelector<HTMLElement>('[data-r]');
    return el ? { r: +el.dataset.r!, c: +el.dataset.c!, el } : null;
  }

  private bindGrid() {
    const g = this.grid;
    g.addEventListener('mousedown', (e) => {
      const t = e.target as HTMLElement;
      const colTh = t.closest<HTMLElement>('th[data-col]'), rowTh = t.closest<HTMLElement>('th[data-row]');
      if (colTh || rowTh) {
        e.preventDefault();
        const last = this.rows.length - 1, lastC = this.width() - 1;
        if (colTh) {
          const j = +colTh.dataset.col!;
          const a = e.shiftKey ? this.anchor : { r: 0, c: j };
          this.anchor = a; this.cur = { r: 0, c: j };
          this.range = { r0: 0, c0: a.c, r1: last, c1: j };
        } else {
          const i = +rowTh!.dataset.row!;
          const a = e.shiftKey ? this.anchor : { r: i, c: 0 };
          this.anchor = a; this.cur = { r: i, c: 0 };
          this.range = { r0: a.r, c0: 0, r1: i, c1: lastC };
        }
        this.paintSelection();
        return;
      }
      const cell = this.cellOf(t);
      if (!cell || e.button !== 0) return;
      if (e.shiftKey) {
        e.preventDefault();
        this.setRange(this.anchor, cell);
        return;
      }
      this.anchor = { r: cell.r, c: cell.c };
      this.cur = { r: cell.r, c: cell.c };
      this.dragging = true;
      if (this.range) { this.range = null; this.paintSelection(); }
    });
    g.addEventListener('contextmenu', (e) => {
      const t = e.target as HTMLElement;
      const colTh = t.closest<HTMLElement>('th[data-col]'), rowTh = t.closest<HTMLElement>('th[data-row]');
      const s = this.sel();
      if (colTh) { // right-click on a column letter outside the selection selects that column
        const j = +colTh.dataset.col!;
        if (!(this.range && j >= s.c0 && j <= s.c1 && s.r0 === 0 && s.r1 === this.rows.length - 1)) {
          this.cur = { r: 0, c: j }; this.anchor = { ...this.cur }; this.range = { r0: 0, c0: j, r1: this.rows.length - 1, c1: j };
        }
      } else if (rowTh) {
        const i = +rowTh.dataset.row!;
        if (!(this.range && i >= s.r0 && i <= s.r1 && s.c0 === 0 && s.c1 === this.width() - 1)) {
          this.cur = { r: i, c: 0 }; this.anchor = { ...this.cur }; this.range = { r0: i, c0: 0, r1: i, c1: this.width() - 1 };
        }
      } else {
        const cell = this.cellOf(t);
        if (!cell) return;
        const inside = cell.r >= s.r0 && cell.r <= s.r1 && cell.c >= s.c0 && cell.c <= s.c1;
        if (!inside) { this.cur = { r: cell.r, c: cell.c }; this.anchor = { ...this.cur }; this.range = null; }
      }
      this.dragging = false;
      this.paintSelection();
      this.menu(e);
    });
    g.addEventListener('mouseover', (e) => {
      if (!this.dragging) return;
      const cell = this.cellOf(e.target);
      if (!cell || (cell.r === this.anchor.r && cell.c === this.anchor.c && !this.range)) return;
      getSelection()?.removeAllRanges();
      this.setRange(this.anchor, cell);
    });
    g.addEventListener('focusin', (e) => {
      const cell = this.cellOf(e.target);
      if (!cell) return;
      this.cur = { r: cell.r, c: cell.c };
      this.status();
    });
    g.addEventListener('beforeinput', () => {
      if (!this.dirtyCell) { this.push(); this.dirtyCell = true; }
    });
    g.addEventListener('input', (e) => {
      const cell = this.cellOf(e.target);
      if (!cell) return;
      this.rows[cell.r]![cell.c] = (cell.el.innerText ?? '').replace(/\n$/, '');
      cell.el.parentElement!.classList.toggle('is-num', cell.r >= this.header && NUM.test(this.rows[cell.r]![cell.c]!.trim()));
      this.updateDecimal();
      this.onChange({ rows: this.rows });
    });
    g.addEventListener('focusout', () => { this.dirtyCell = false; });
    g.addEventListener('click', (e) => {
      const t = e.target as HTMLElement;
      const td = t.closest('td');
      if (td && !t.closest('[data-r]') && !this.range) { // click in the cell padding below the text
        const d = td.querySelector<HTMLElement>('[data-r]')!;
        this.focusCell(+d.dataset.r!, +d.dataset.c!);
      }
    });
    g.addEventListener('keydown', (e) => {
      const cell = this.cellOf(e.target);
      if (!cell) return;
      const { r, c, el } = cell;
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key.toLowerCase() === 'z') { e.preventDefault(); e.shiftKey ? this.redo() : this.undo(); this.focusCell(this.cur.r, this.cur.c); return; }
      if (mod && e.key.toLowerCase() === 'y') { e.preventDefault(); this.redo(); this.focusCell(this.cur.r, this.cur.c); return; }
      if (mod && e.key.toLowerCase() === 'm') { e.preventDefault(); this.mergeCells(); this.focusCell(this.cur.r, this.cur.c); return; }
      if (e.key === 'Enter' && !e.altKey) {
        e.preventDefault();
        this.commitCell();
        const span = this.merges.find((m) => m[0] === r && m[1] === c);
        const nr = e.shiftKey ? r - 1 : r + (span ? span[2] : 1);
        if (nr >= this.rows.length) { this.cur = { r, c }; this.range = null; this.insertRow(true); this.focusCell(r + 1, c); }
        else if (nr >= 0) this.focusCell(nr, c);
        return;
      }
      if (e.key === 'ArrowDown' && (mod || this.atEdge(el, 'end'))) { e.preventDefault(); this.focusCell(Math.min(r + 1, this.rows.length - 1), c); }
      if (e.key === 'ArrowUp' && (mod || this.atEdge(el, 'start'))) { e.preventDefault(); this.focusCell(Math.max(r - 1, 0), c); }
      if (e.key === 'Escape') { this.range = null; this.paintSelection(); el.blur(); }
      if ((e.key === 'Delete' || e.key === 'Backspace') && this.range) { e.preventDefault(); this.clearCells(); }
      if (mod && e.key.toLowerCase() === 'c' && this.range) { e.preventDefault(); navigator.clipboard.writeText(this.selectionTSV()); }
    });
    g.addEventListener('paste', (e) => {
      const cell = this.cellOf(e.target);
      if (!cell || this.readonly) return;
      const text = e.clipboardData?.getData('text/plain') ?? '';
      if (!/[\t\n]/.test(text.replace(/\n$/, ''))) {
        if (!plaintextOnly) { e.preventDefault(); document.execCommand('insertText', false, text); }
        return;
      }
      e.preventDefault();
      const block = parseTSV(text);
      this.edit(() => {
        block.forEach((row, i) => {
          while (this.rows.length <= cell.r + i) this.rows.push(Array(this.width()).fill(''));
          row.forEach((v, j) => { const tr = this.rows[cell.r + i]!; while (tr.length <= cell.c + j) tr.push(''); tr[cell.c + j] = v; });
        });
      });
      this.focusCell(cell.r, cell.c);
    });
  }

  /** single-line cells: arrows move between cells; multi-line cells only at their first/last line */
  private atEdge(el: HTMLElement, where: 'start' | 'end') {
    const text = el.innerText.replace(/\n$/, '');
    if (!text.includes('\n')) return true;
    const s = getSelection();
    if (!s || !s.rangeCount) return true;
    const r = s.getRangeAt(0);
    const probe = document.createRange();
    probe.selectNodeContents(el);
    if (where === 'start') probe.setEnd(r.startContainer, r.startOffset);
    else probe.setStart(r.endContainer, r.endOffset);
    return !probe.toString().includes('\n');
  }
}

/** Excel/Sheets clipboard (TSV with quoted fields) */
export function parseTSV(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [], cell = '', q = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]!;
    if (q) {
      if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++; }
      else if (ch === '"') q = false;
      else cell += ch;
    } else if (ch === '"' && !cell) q = true;
    else if (ch === '\t') { row.push(cell); cell = ''; }
    else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++;
      row.push(cell); rows.push(row); row = []; cell = '';
    } else cell += ch;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  return rows;
}

/** read-only HTML rendering of a table (with merges) for previews */
export function tableHTML(t: Pick<Table, 'rows' | 'merges' | 'header_rows'>, maxRows = Infinity): string {
  const covered = new Set<string>();
  const origin = new Map<string, Merge>();
  for (const m of t.merges ?? []) {
    origin.set(`${m[0]},${m[1]}`, m);
    for (let i = m[0]; i < m[0] + m[2]; i++) for (let j = m[1]; j < m[1] + m[3]; j++) if (i !== m[0] || j !== m[1]) covered.add(`${i},${j}`);
  }
  const w = Math.max(1, ...t.rows.map((r) => r.length));
  let h = '<table class="preview-table">';
  t.rows.slice(0, maxRows).forEach((r, i) => {
    const head = i < t.header_rows;
    h += '<tr>';
    for (let j = 0; j < w; j++) {
      if (covered.has(`${i},${j}`)) continue;
      const m = origin.get(`${i},${j}`);
      const v = r[j] ?? '';
      const tag = head ? 'th' : 'td';
      const span = m ? ` rowspan="${Math.min(m[2], Math.max(1, maxRows - i))}" colspan="${m[3]}"` : '';
      h += `<${tag}${span} class="${!head && NUM.test(v.trim()) ? 'num' : ''}">${esc(v)}</${tag}>`;
    }
    h += '</tr>';
  });
  return h + '</table>';
}
