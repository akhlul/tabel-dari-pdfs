// Library categories: a path of names (any depth), e.g. ["Statistik Ekonomi", "Transportasi"].
// [] = uncategorised (uploads start there; BPS imports get their BPS subject).
import { icon } from './icons';
import { esc, h } from './ui';

export const UNCAT = 'Uncategorized';
export const SEP = ' / ';

export interface CatNode {
  name: string;
  path: string[];
  /** documents in this category and below */
  count: number;
  /** documents directly in this category */
  own: number;
  children: CatNode[];
}

export const pathKey = (p: string[]) => JSON.stringify(p);
export const catLabel = (p: string[]) => (p.length ? p.join(SEP) : UNCAT);
export const isUnder = (p: string[], prefix: string[]) => prefix.every((x, i) => p[i] === x);

const order = (a: CatNode, b: CatNode) =>
  Number(a.name === 'Lainnya') - Number(b.name === 'Lainnya') || a.name.localeCompare(b.name, 'id');

/** tree of every category used by `items`; uncategorised items are counted on the root only */
export function buildTree(items: { category: string[] }[]): CatNode {
  const root: CatNode = { name: '', path: [], count: 0, own: 0, children: [] };
  for (const it of items) {
    root.count++;
    let n = root;
    for (const name of it.category ?? []) {
      let c = n.children.find((x) => x.name === name);
      if (!c) { c = { name, path: [...n.path, name], count: 0, own: 0, children: [] }; n.children.push(c); }
      c.count++;
      n = c;
    }
    n.own++;
  }
  const sort = (n: CatNode) => { n.children.sort(order); n.children.forEach(sort); };
  sort(root);
  return root;
}

/** every category path in the tree (for pickers), depth-first */
export function allPaths(n: CatNode, out: string[][] = []): string[][] {
  for (const c of n.children) { out.push(c.path); allPaths(c, out); }
  return out;
}

export const parsePath = (s: string) => s.split('/').map((x) => x.replace(/\s+/g, ' ').trim()).filter(Boolean).slice(0, 6);

/**
 * "Move to category" dialog. Resolves with the new path, 'auto' (back to the BPS subject) or null (cancelled).
 * Levels are typed with "/" (Category / Sub-category / Sub-sub-category), or picked from the existing ones.
 */
export function categoryDialog(opts: { heading: string; current: string[]; known: string[][]; auto?: boolean; okLabel?: string }):
  Promise<string[] | 'auto' | null> {
  return new Promise((resolve) => {
    const back = document.activeElement as HTMLElement | null;
    const known = opts.known.filter((p) => p.length);
    const dlg = h(`<dialog aria-labelledby="cdlg-t" class="panel p-0 shadow-2xl backdrop:bg-black/40 w-[560px] max-w-[94vw] m-auto border-ink-300">
      <form method="dialog">
        <div class="px-5 pt-4 pb-1"><h3 id="cdlg-t" class="font-semibold text-ink-900">${esc(opts.heading)}</h3></div>
        <div class="px-5 pb-3">
          <label for="cdlg-in" class="block text-[12px] text-ink-600 mb-1">Category path. Separate levels with “/”: Category / Sub-category / Sub-sub-category. Leave empty for ${UNCAT}.</label>
          <input id="cdlg-in" list="cdlg-list" class="field w-full" autocomplete="off" value="${esc(opts.current.join(SEP))}" placeholder="e.g. Statistik Ekonomi / Transportasi" />
          <datalist id="cdlg-list">${known.map((p) => `<option value="${esc(p.join(SEP))}"></option>`).join('')}</datalist>
          ${known.length ? `<div class="label mt-3">Existing categories</div>
          <div class="max-h-60 overflow-y-auto border border-ink-200" role="listbox" aria-label="Existing categories">
            ${known.map((p) => `<button type="button" data-pick="${esc(pathKey(p))}" class="flex w-full items-center gap-md h-7 pr-2 text-left text-[12.5px] text-ink-800 hover:bg-brand-50 focus:bg-brand-50 focus:outline-none"
              style="padding-left:${8 + (p.length - 1) * 16}px">${icon('folder', 'size-3.5 text-ink-400')}<span class="truncate">${esc(p.at(-1)!)}</span></button>`).join('')}
          </div>` : ''}
        </div>
        <div class="flex items-center gap-lg px-5 py-3 bg-ink-50 border-t border-ink-200">
          ${opts.auto ? `<button type="button" value="auto" class="btn-ghost btn-sm" title="Use the BPS subject (or a guess from the title) again">${icon('wand', 'size-3.5')}Automatic</button>` : ''}
          <button type="button" value="uncat" class="btn-ghost btn-sm">${icon('inbox', 'size-3.5')}${UNCAT}</button>
          <span class="flex-1"></span>
          <button type="button" value="cancel" class="btn-line">Cancel</button>
          <button type="submit" value="ok" class="btn-primary">${icon('folder', 'size-3.5')}${esc(opts.okLabel ?? 'Move')}</button>
        </div>
      </form></dialog>`) as HTMLDialogElement;
    document.body.append(dlg);
    const input = dlg.querySelector<HTMLInputElement>('#cdlg-in')!;
    let result: string[] | 'auto' | null = null;
    dlg.addEventListener('click', (e) => {
      const t = e.target as HTMLElement;
      const pick = t.closest<HTMLElement>('[data-pick]');
      if (pick) { input.value = (JSON.parse(pick.dataset.pick!) as string[]).join(SEP); input.focus(); return; }
      const v = t.closest('button')?.value;
      if (v === 'cancel') dlg.close();
      else if (v === 'uncat') { result = []; dlg.close(); }
      else if (v === 'auto') { result = 'auto'; dlg.close(); }
      else if (e.target === dlg) dlg.close();
    });
    dlg.querySelector('form')!.addEventListener('submit', () => { result = parsePath(input.value); });
    dlg.addEventListener('close', () => { resolve(result); dlg.remove(); back?.focus(); });
    dlg.showModal();
    input.select();
  });
}
