# Notes for coding agents

Tabel dari PDF: find BPS publications, extract their tables, review and fix them, export them.
Read [`../README.md`](../README.md) (what the app does, API) and [`../DESIGN.md`](../DESIGN.md) (UI rules) first.

## Repository

| path | what |
|---|---|
| `backend/app/main.py` | FastAPI app: documents, uploads, BPS search, jobs, backup; serves `frontend/dist` |
| `backend/app/tables_api.py` | tables: CRUD, flags (+ validator), search with facets, join / unjoin, exports |
| `backend/app/imports.py`, `queue_api.py` | the import queue (one BPS import at a time, stored in SQLite) and the Queue page API |
| `backend/app/jobs.py` | table-finding jobs (thread pool, `JOB_WORKERS`); `limit=False` skips `JOBS_PER_USER` |
| `backend/app/db.py` | schema; new columns go **at the end of `MIGRATIONS`** (never edit old entries); `init()` repairs state after a restart |
| `backend/app/auth.py` | login, roles, CSRF; every `/api` route needs an editor unless listed in `PUBLIC_GET` / `PUBLIC_POST` |
| `frontend/src/layouts/Layout.astro` | top bar (text only, Queue badge), login state, tooltips |
| `frontend/src/pages/` | `index` Library · `tables` · `import-from-bps` BPS catalog (`search` only redirects) · `queue` · `doc` workspace · `settings` · `users` |
| `frontend/src/lib/` | `api.ts` (types + calls) · `ui.ts` (toast, menus, context menu, tooltips) · `layout.ts` (workspace panes A/B/M/G and presets) · `editor.ts` (cell grid) · `viewer.ts` (pdf.js) · `split.ts` (resizable panes) · `tags.ts` (tag input) · `settings.ts` (browser-only settings, theme) |
| `frontend/src/styles/global.css` | tokens (light + dark), utilities, component classes |
| `docs/screenshots/` | images used in the README |

## Commands

```sh
mise run dev            # API :14433 + UI :4321
mise run build          # frontend -> frontend/dist (the API serves it)
mise run check:a11y     # axe, light + dark, plus the 900 px fallback (needs a running server; CHROME=… for the browser)
```

The frontend has no `astro check` installed: `bunx astro build` is the syntax check. The backend has no test suite:
check with `uv run python -m py_compile app/*.py` and by calling the API.

## Testing without touching the user's library

Never run experiments against `data/`. Copy it and point a second server at the copy:

```sh
cp -r data /tmp/tdp-data
cd backend && DATA_DIR=/tmp/tdp-data uv run uvicorn app.main:app --port 14499
```

To act as a user in scripts, create a session in the copy (`auth.create_session(user_id, 'password', ip, ua)`) and send
it as the `tdp_session` cookie. Playwright (`playwright-core`, its Chromium in `~/.cache/ms-playwright`) works for
screenshots and UI flows. Look at every screenshot you take before reporting a change as done.

## Conventions

- Follow `DESIGN.md`: square corners, color only with meaning (blue = selection/focus, orange = edited, red = delete,
  green = valid), undo instead of confirmation, every error actionable, everything reachable by keyboard.
- The **top bar has no icons**. Elsewhere icons come from `lib/icons.ts` (`icon(name)`); add new ones there.
- Use tokens (`ink-*`, `brand-*`, `--nav-*`…), never hex values, so the dark scheme works. Dark is chosen by the system
  or by Settings → Appearance (`html[data-theme]`, set before the first paint by `EARLY` in `lib/auth.ts`).
- Custom component classes in `global.css` go inside `@layer components`, otherwise they override Tailwind utilities
  such as `hidden`.
- Role-dependent elements use `data-need="editor" | "admin" | "public"`; the server checks the same rules again
  (for example: editors change year and tags only on uploaded documents, admins on all).
- Visitors (not logged in) see only tables flagged **valid**; they never see user ids or names.
- Validation stays manual: joins, re-extractions and imports never change a table's flag.
- User-visible text is English, Title Case for buttons and menu items; BPS content stays in Indonesian.
- Keep `README.md` (features, API) and `DESIGN.md` (UI rules) up to date with every change, and refresh the README
  screenshots when a page changes visibly.

## Astro

When starting the dev server, use background mode:

```
astro dev --background
```

Manage the background server with `astro dev stop`, `astro dev status`, and `astro dev logs`.

Full documentation: https://docs.astro.build

- [Adding pages, dynamic routes, or middleware](https://docs.astro.build/en/guides/routing/)
- [Working with Astro components](https://docs.astro.build/en/basics/astro-components/)
- [Adding styles or using Tailwind](https://docs.astro.build/en/guides/styling/)
