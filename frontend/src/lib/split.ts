/**
 * A draggable divider that resizes one fixed-width pane next to a flexible one.
 * Drag, or focus it and use the arrow keys (Shift = bigger steps); double-click resets. The width is remembered.
 */
export function splitter(bar: HTMLElement, pane: HTMLElement, opts: { key: string; side: 'left' | 'right'; min: number; max: number; def: number; label: string }) {
  const k = `tdp.split.${opts.key}`;
  const clamp = (w: number) => Math.max(opts.min, Math.min(opts.max, Math.round(w)));
  const set = (w: number, save = true) => {
    w = clamp(w);
    pane.style.width = `${w}px`;
    bar.setAttribute('aria-valuenow', String(w));
    if (save) localStorage.setItem(k, String(w));
  };
  set(+(localStorage.getItem(k) ?? opts.def) || opts.def, false);
  bar.setAttribute('role', 'separator');
  bar.setAttribute('aria-orientation', 'vertical');
  bar.setAttribute('aria-label', opts.label);
  bar.setAttribute('aria-valuemin', String(opts.min));
  bar.setAttribute('aria-valuemax', String(opts.max));
  bar.tabIndex = 0;
  bar.title = 'Drag or use the arrow keys to resize. Double-click to reset.';
  bar.classList.add('splitter');
  const sign = opts.side === 'left' ? 1 : -1; // the pane is left of the bar: dragging right grows it
  bar.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    const x0 = e.clientX, w0 = pane.getBoundingClientRect().width;
    const move = (ev: PointerEvent) => set(w0 + (ev.clientX - x0) * sign, false);
    const up = () => {
      removeEventListener('pointermove', move); removeEventListener('pointerup', up);
      document.body.style.cursor = '';
      localStorage.setItem(k, String(pane.getBoundingClientRect().width | 0));
    };
    addEventListener('pointermove', move); addEventListener('pointerup', up);
    document.body.style.cursor = 'col-resize';
  });
  bar.addEventListener('keydown', (e) => {
    const d = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
    if (!d) return;
    e.preventDefault();
    set(pane.getBoundingClientRect().width + d * sign * (e.shiftKey ? 80 : 20));
  });
  bar.addEventListener('dblclick', () => { localStorage.removeItem(k); set(opts.def, false); });
}
