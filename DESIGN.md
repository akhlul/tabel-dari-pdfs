# Design

The UI follows the [KDE Human Interface Guidelines](https://develop.kde.org/hig/), with one deliberate exception:
**square corners everywhere** (no rounded corners, no squircles). Every `--radius-*` token is `0`; don't add `rounded-*`
classes or `border-radius`.

This file is the reference for anyone changing the frontend. Tokens live in `frontend/src/styles/global.css`, shared
behaviour in `frontend/src/lib/ui.ts`.

## Principles

1. **Undo, don't ask.** Act immediately and offer Undo. Confirm only what can't be undone.
2. **Don't report what's visible.** No toast for a save, rename or flag the user can already see.
3. **Every error is actionable.** Say what happened and what to do; offer Retry when retrying can help.
4. **Color is never alone.** Every state also has an icon, text or line style.
5. **Everything works from the keyboard**, and focus never gets lost.
6. **Plain words.** Describe what the user gets, not how it's implemented.

## Color

Colors carry meaning. Use them only for that meaning.

| Meaning | Token | Used for |
|---|---|---|
| Selection & focus | `brand-*` (blue) | selected rows/cells/boxes (fill), focus ring (outline), primary button |
| Non-default / changed | `accent-*` (orange) | "edited" chip, changed-cell marker in the editor |
| Destructive / error | `red-*` | delete buttons, error toasts, deleted boxes |
| Valid | `emerald-*` (green) | "Valid" flag, success toasts |
| Neutral | `ink-50`…`ink-900` | text, borders, backgrounds |
| Surfaces | `--color-surface`, `--color-header` | panels, menus, app header |

- Selection is a **fill**, focus is an **outline** (`:focus-visible`, 2px `brand-500`), so the two never look alike.
- Orange is not an action color: there is no orange button. Primary buttons are `btn-primary` (blue).
- **Dark scheme** follows `prefers-color-scheme`: the `ink`, `brand`, `accent`, `red`, `emerald` scales and the surface
  tokens are redefined in the dark media query. Use tokens, never hard-coded hex values, so both schemes work.
  Overlays (menus, tooltips, toasts) have a contrasting border so they stay visible on dark backgrounds.
- Text must meet WCAG AA contrast in both schemes (checked by `mise run check:a11y`).

### Table flags

| Flag | List / chips | PDF box (`.tbox`) |
|---|---|---|
| Unflagged | no icon | solid blue outline |
| Valid | green check + "Valid" | solid green outline |
| Not a Table | slashed icon + "Not a table" | dashed grey outline, hatched fill |
| Deleted | trash icon + "Deleted" | dotted red outline, faded |
| Selected | bold title, blue fill | thicker dark-blue outline, handles, white halo |
| Joined part | `p69` tag | dashed outline |

## Spacing and type

- Spacing (Kirigami units): `sm` 4px, `md` 6px, `lg` 8px, grid unit 18px. Use `gap-sm|md|lg`, `p-lg`, etc.
- Controls are 32px tall (`h-8`), small controls 28px (`btn-sm`). Base UI text is 13px; secondary text 11–12px.
- Fonts: `--font-sans` (Inter, Noto Sans, system), `--font-mono` for codes and shortcuts. Numbers use tabular figures.
- Numeric cells are right-aligned; header rows are bold on `ink-100`.

## Components

Utilities defined in `global.css`:

| Class | Use |
|---|---|
| `btn-primary` | the one main action in a view |
| `btn-line` | ordinary actions |
| `btn-ghost` | toolbar and icon buttons |
| `btn-danger` | destructive actions |
| `btn-sm`, `btn-icon` | size modifiers |
| `field`, `label` | inputs and their labels |
| `chip` | small state labels (flag, edited, engine) |
| `panel` | bordered surface |
| `menu`, `menu-item` | dropdowns and context menus |
| `kbd` | shortcut hints |
| `skeleton` | loading placeholders |
| `.flag-opt` | segmented radio for the flag (Unflagged · Valid · Not a Table) |

- Buttons use the default cursor; only real links (`a[href]`) get the pointer.
- **Icon-only buttons** need a `title`. `initTooltips()` turns it into a tooltip (hover, keyboard focus, long-press)
  and an `aria-label` (or `aria-description` when the button has visible text). End the title with the shortcut in
  parentheses, e.g. `title="Zoom in (+)"`; it is shown as a key cap and left out of the accessible name.
- **Toolbars** show the few most-used actions; the rest go into a **More** menu. The editor groups its actions into
  Cells / Rows / Columns / Table menus. Toolbars wrap on narrow windows instead of clipping.
- **Dialogs** are a last resort. Prefer inline panes (the Tables page uses a preview pane, not a modal).
  `confirmDialog` is only used for *Delete Permanently…*.

## Feedback

Use `toast(msg, kind, { action })` from `ui.ts`.

- **Undoable actions** (delete document/table, Reset, Extract Again, Join, Unjoin, moving or resizing a table area)
  show a toast with **Undo**, visible for 8 s. The backend supports this: documents are soft-deleted and purged after
  one hour (`POST /api/documents/{id}/restore`); table changes snapshot a version and return `undo_version`.
- **Errors** use `toastError(e, retry)`: 7 s, `role="alert"`, with Retry when a retry function is given.
- Toasts pause while hovered or focused.
- **Long operations** show determinate progress ("Downloading 4.7 MB of 8.1 MB") and can be cancelled (BPS import).
  Uploads show percentage.
- **Empty states** explain why it's empty and offer the action that fills it.
- The header health indicator only appears when something is wrong (server unreachable, BPS key missing).

## Text

- **Title Case** for buttons, menu items, tabs and window titles: *Get All Tables*, *Copy for Spreadsheet*.
  **Sentence case** for everything else: labels, messages, tooltips.
- **"…"** only when the action asks for more input before it runs (*Delete Permanently…*).
- Name results, not mechanisms: *Ruled table* (not `lines`), *Borderless table* (not `stream`).
- Use `plural(n, 'table')` for counts. Use curly quotes (“ ”) around names in messages.
- The interface and exports are in **English**. BPS content (titles, notes, cells) stays as published, in Indonesian.

## Keyboard

| Where | Keys |
|---|---|
| Anywhere in the workspace | Alt+1 / 2 / 3 → PDF / list / editor · Ctrl+PgUp / PgDn → previous / next tab |
| Table list (listbox) | ↑ ↓ Home End PgUp PgDn · Shift+↑↓ extend · Space toggle · Ctrl+A all · Enter open in editor · → ← expand / collapse parts · Delete · `v` valid · `x` not a table |
| Menus | ↑ ↓ Home End, Enter, Esc (focus returns to the opener) · Menu or Shift+F10 opens the context menu |
| PDF table area (focused) | arrows move · Shift+arrows resize · steps of 2 pt, Alt for 0.5 pt · re-extract runs 700 ms after the last key |
| Pane dividers | arrows resize · double-click resets |
| Viewer | `+` / `-` zoom |

Single-letter shortcuts (`v`, `x`, `+`, `-`) are ignored while typing in a field.

Rules: every action reachable by mouse is reachable by keyboard; focus is restored after menus close, after redraws,
and after Undo; the focused row in a listbox has a visible outline separate from its selection fill.

## Layout

- Workspace panes: **a** = PDF, **b** = table list, **c** = editor. Default layout is `b | a | c`.
- Below 1100 px wide, multi-column layouts fall back to `list-tabs`; below 720 px, everything goes into tabs
  (`Layout.effective()` in `lib/layout.ts`). The user's chosen layout is kept and comes back when the window grows.
- Dividers have `role="separator"` with `aria-valuenow` and are keyboard-resizable.
- Per-document state (`localStorage` key `tdp.doc.<id>`): last table, page and zoom.
- A skip link and `aria-current` on the active nav item are in `Layout.astro`.

## Motion

- Transitions are short (≤ 200 ms) and only for state changes (toast in/out, hover).
- `prefers-reduced-motion: reduce` makes all transitions and animations instant.

## Checking changes

```sh
mise run check:a11y                        # axe (light + dark) on every page, plus the 900px tabs fallback
BASE=http://127.0.0.1:8010 bun tests/a11y.ts   # against another server (run in frontend/)
```

Before merging UI changes: the a11y check passes, the page works by keyboard alone, and it looks right in the dark
scheme and at 900 px wide.
