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
| Surfaces | `--color-surface`, `--color-header` | panels, menus |
| Top bar | `--nav-bg`, `--nav-hover`, `--nav-active`, `--nav-text`, `--nav-badge` | the navy strip at the top (darker navy in the dark scheme) |

- Pressed filter chips (Tables status, Queue status) are a blue fill with white text: they are a selection.
- Selection is a **fill**, focus is an **outline** (`:focus-visible`, 2px `brand-500`), so the two never look alike.
- Orange is not an action color: there is no orange button. Primary buttons are `btn-primary` (blue).
- **Dark scheme** follows `prefers-color-scheme`, unless Settings → Appearance picks Light or Dark (`html[data-theme]`,
  set before the first paint): the `ink`, `brand`, `accent`, `red`, `emerald` scales, the surface and the `--nav-*`
  tokens are redefined for dark. The light/dark switch lives in Settings only, not in the top bar.
  Use tokens, never hard-coded hex values, so both schemes work.
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
| `.count-badge` | counts next to tree items and filters |
| `.src-tag` (`.is-upload`) | where a document comes from: `BPS` outlined, `UPLOAD` filled |
| `.tag-year`, `.tag-chip`, `.tag-add`, `.tag-miss` | publication year, tags, "+ Tag", "Publication year missing" (dashed) |
| `.splitter` | the vertical divider of resizable panes (`lib/split.ts`) |
| `.lp-opt`, `.lp-switch` | Layout presets menu: option with a mini wireframe icon, and the Focus Mode switch |
| `.meta-label` | small uppercase labels in the details pane (STATUS, TITLE, NOTES) |
| `tagInput()` (`lib/tags.ts`) | tags as chips: Enter or comma adds, Backspace removes the last, suggestions from tags in use |

- Buttons use the default cursor; only real links (`a[href]`) get the pointer.
- **Icon-only buttons** need a `title`. `initTooltips()` turns it into a tooltip (hover, keyboard focus, long-press)
  and an `aria-label` (or `aria-description` when the button has visible text). End the title with the shortcut in
  parentheses, e.g. `title="Zoom in (+)"`; it is shown as a key cap and left out of the accessible name.
- **Toolbars** show the few most-used actions; the rest go into a **More** menu. The editor groups its actions into
  Cells / Rows / Columns / Table menus. Toolbars wrap on narrow windows instead of clipping.
- **Dialogs** are a last resort. Prefer inline panes (the Tables page uses a preview pane, not a modal). Modals
  (`<dialog>`) are used for forms that need several fields at once: *Upload PDFs* (year per file) and *Edit Details and
  Tags*. `confirmDialog` is only used for *Delete Permanently…*.
- **Context menus on trees** (Library categories, Tables documents) also fold: Expand / Collapse, This and All Below,
  Expand All / Collapse All.
- **Column choice**: the Tables list hides columns from its *Columns* menu or by right-clicking the column headers; the
  choice is remembered.

## Feedback

Use `toast(msg, kind, { action })` from `ui.ts`.

- **Undoable actions** (delete document/table, Reset, Extract Again, Join, Unjoin, moving or resizing a table area)
  show a toast with **Undo**, visible for 8 s. The backend supports this: documents are soft-deleted and purged after
  one hour (`POST /api/documents/{id}/restore`); table changes snapshot a version and return `undo_version`.
- **Errors** use `toastError(e, retry)`: 7 s, `role="alert"`, with Retry when a retry function is given.
- Toasts pause while hovered or focused.
- **Long operations** show determinate progress ("Downloading 4.7 MB of 8.1 MB", "Page 318 of 512") and can be
  cancelled. BPS imports go through the **Queue** (one at a time, stages Queued › Download › Find tables › Done); the
  top bar shows how many are waiting or running. Uploads show percentage per file.
- **Suggestions, not surprises**: detected continuation tables are offered in a bar (Join All Automatically / Review
  One by One / Not Now / Don't Ask Again); automatic steps never set a flag, validating stays manual.
- **Show what *Auto* chose**: an automatic setting names its result and the reason (Decimal: "Auto: comma ·
  1.234,5 · detected from 55 numbers"); an unsure result is orange and asks for a choice.
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
| Anywhere in the workspace | Alt+1 / 2 / 3 / 4 → PDF / list / details / cells · Ctrl+PgUp / PgDn → previous / next tab · `F` single pane (focus mode) |
| Tables page list | ↑ ↓ move and preview · Shift+↑↓ extend · Space toggle · Shift+click range · Ctrl+click add / remove · Menu key actions · Esc closes the preview |
| Trees (Library, Tables) | ↑ ↓ · → ← expand / collapse · Enter select · Menu key or Shift+F10: folding and other actions |
| Workspace table list (listbox) | ↑ ↓ Home End PgUp PgDn · Shift+↑↓ extend · Space toggle · Ctrl+A all · Enter open in editor · → ← expand / collapse parts · Delete · `v` valid · `x` not a table |
| Menus | ↑ ↓ Home End, Enter, Esc (focus returns to the opener) · Menu or Shift+F10 opens the context menu |
| PDF table area (focused) | arrows move · Shift+arrows resize · steps of 2 pt, Alt for 0.5 pt · re-extract runs 700 ms after the last key |
| Pane dividers | arrows resize · double-click resets |
| Viewer | `+` / `-` zoom |

Single-letter shortcuts (`v`, `x`, `+`, `-`) are ignored while typing in a field.

Rules: every action reachable by mouse is reachable by keyboard; focus is restored after menus close, after redraws,
and after Undo; the focused row in a listbox has a visible outline separate from its selection fill.

## Layout

- Top bar: one navy strip (`--nav-*` tokens, a darker navy in the dark scheme), **text only, no icons**: brand,
  Library · Tables · BPS Catalog (editors), then Queue with a count badge (editors; hidden at 0), Settings | user name
  in small caps (or Log In). The active link has a lighter fill and a white bottom border.
- Pages: `/` Library, `/tables`, `/import-from-bps` BPS catalog (`/search` redirects there), `/queue`, `/doc`
  workspace, `/settings`, `/users`.
- Workspace panes: **A** = PDF (with the page rail; its header is a page icon), **B** = table list (one flat list in
  reading order, no tabs), **M** = details (flag, title, notes), **G** = cells. Default preset *Standard*: `B | ((A | M) / G)`. Presets and *Single Pane (Focus Mode)* are in the
  Layout menu; each preset has a mini wireframe icon, the chosen one a blue fill and a check.
- Below 1100 px wide, multi-column layouts fall back to `list-tabs`; below 720 px, everything goes into tabs
  (`Layout.effective()` in `lib/layout.ts`). The user's chosen layout is kept and comes back when the window grows.
- The Tables page has resizable panes too (`lib/split.ts`); modals (upload, edit details) are `<dialog>`s.
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

Check against a server on a **copy** of the library (`DATA_DIR=/tmp/…`, see `frontend/AGENTS.md`), as an editor, an
admin and a visitor. When a page changes visibly, refresh its screenshot in `docs/screenshots/`.

Before merging UI changes: the a11y check passes, the page works by keyboard alone, and it looks right in the dark
scheme and at 900 px wide.
