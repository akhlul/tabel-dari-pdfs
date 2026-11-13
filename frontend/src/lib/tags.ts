// Tag input: chips with a remove button and a text field. Enter or comma adds a tag, Backspace in the empty
// field removes the last one; suggestions come from tags already in use (datalist).
import { esc } from './ui';

let n = 0;

export interface TagInput { get(): string[]; set(tags: string[]): void; flush(): void }

export function clean(tags: string[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const t of tags) {
    const v = t.replace(/\s+/g, ' ').replace(/^,+|,+$/g, '').trim().slice(0, 40).trim();
    if (v && !seen.has(v.toLowerCase())) { seen.add(v.toLowerCase()); out.push(v); }
  }
  return out.slice(0, 30);
}

/** host: an empty element; it becomes the field (looks like .field) */
export function tagInput(host: HTMLElement, initial: string[], suggestions: string[], label: string, onChange?: (tags: string[]) => void): TagInput {
  const id = `tags-${++n}`;
  let tags = clean(initial);
  host.className = 'tag-field flex flex-wrap items-center gap-sm min-h-8 px-1 py-[3px] bg-surface border border-ink-200 hover:border-ink-300 focus-within:border-brand-500 focus-within:ring-2 focus-within:ring-brand-100';
  host.innerHTML = `<span data-chips class="contents"></span>
    <input id="${id}" list="${id}-list" aria-label="${esc(label)}" class="flex-1 min-w-24 h-6 px-1 text-[13px] bg-transparent focus:outline-none" placeholder="add a tag…" autocomplete="off" />
    <datalist id="${id}-list">${suggestions.map((s) => `<option value="${esc(s)}"></option>`).join('')}</datalist>`;
  const input = host.querySelector('input')!;
  const chips = host.querySelector<HTMLElement>('[data-chips]')!;
  const render = () => {
    chips.innerHTML = tags.map((t, i) => `<span class="chip h-6 border-ink-300 bg-ink-50 text-ink-800 text-[12px]">${esc(t)}<button type="button" data-rm="${i}"
      class="-mr-1 px-1 text-ink-500 hover:text-red-700" aria-label="Remove tag ${esc(t)}">×</button></span>`).join('');
    onChange?.(tags);
  };
  const add = (raw: string) => {
    const parts = raw.split(',').map((x) => x.trim()).filter(Boolean);
    if (!parts.length) return;
    tags = clean([...tags, ...parts]);
    input.value = '';
    render();
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ',') { if (input.value.trim()) { e.preventDefault(); add(input.value); } else if (e.key === ',') e.preventDefault(); }
    else if (e.key === 'Backspace' && !input.value && tags.length) { tags = tags.slice(0, -1); render(); }
  });
  input.addEventListener('change', () => { if (suggestions.includes(input.value)) add(input.value); }); // picked from the list
  input.addEventListener('blur', () => add(input.value));
  host.addEventListener('click', (e) => {
    const rm = (e.target as HTMLElement).closest<HTMLElement>('[data-rm]');
    if (rm) { tags = tags.filter((_, i) => i !== +rm.dataset.rm!); render(); input.focus(); return; }
    if (e.target === host) input.focus();
  });
  render();
  return { get: () => tags, set: (t) => { tags = clean(t); render(); }, flush: () => add(input.value) };
}
