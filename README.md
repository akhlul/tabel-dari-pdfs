# Tabel dari PDF

Find BPS publications, extract their tables, edit them, export them.
This rewrites `pdf-table-viewer`: **pdfspine** (Apache-2.0, pure Rust) replaces PyMuPDF, and the UI is new.

```
1. get a PDF   upload it, or search the BPS Web API (Publikasi / Berita Resmi Statistik) and import it
2. extract     "Get all tables" runs a background job over every page (or a page range),
               or "Select region" to draw a box around one table
3. use tables  saved in the library, editable in the browser, export to XLSX / CSV / JSON
```

## Stack

| part      | tech |
|-----------|------|
| backend   | FastAPI, SQLite (`data/library.db`), thread-pool jobs, `pdfspine` for parsing, text, tables and thumbnails |
| frontend  | Astro (static) + Tailwind v4 (square corners only), vanilla TS, pdf.js (legacy build) |
| BPS       | `webapi.bps.go.id/v1/api`, key read from `.env` (`WEBAPI_BPS_KEY`), never sent to the browser |

## Run

```bash
cp .env.example .env          # add WEBAPI_BPS_KEY
mise install                  # bun, python 3.12, uv
mise run install              # uv sync + bun install
mise run dev                  # API :14433 + UI :4321  -> http://localhost:4321
# production: one process serving API + built UI
mise run start                # -> http://localhost:14433
```

Without mise: `cd backend && uv run uvicorn app.main:app --port 14433 --reload` and `cd frontend && bunx astro dev`.

### Docker

```bash
cp .env.example .env          # add WEBAPI_BPS_KEY (read by docker compose)
docker compose up -d --build  # -> http://localhost:14433   (PORT=8080 docker compose up -d to change the port)
```
One image (`Dockerfile`: Astro build stage + Python 3.12 slim, ~370 MB) serving API and UI, running as uid 1000.
The library lives in the volume `tabel-data` mounted at `/data`; see `docker-compose.yml` to use a host folder instead.

### Backup, restore, moving the library

Everything (database: documents, tables, edits, flags, categories, history; plus the PDFs) goes into one `.zip`:

- In the app: **Settings → Backup and Restore** (download with or without PDFs; restore by *adding* to the library or
  *replacing* it; a replace first moves the current library to `data/backups/before-restore-<time>/`).
- API: `GET /api/backup?pdfs=true|false`, `POST /api/backup/restore` (multipart `file`, `mode=merge|replace`), `GET /api/backup/info`.
- CLI: `cd backend && uv run python -m app.backup export [out.zip] [--no-pdfs]` / `... import file.zip --mode merge|replace`;
  in Docker `docker compose exec app python -m app.backup export` (written to `/data/backups/`, copy it out with `docker compose cp`).

A non-Docker library moves into Docker (and back) with a backup + restore. `library.db` is plain SQLite, so it can also
be opened directly (e.g. `sqlite3 data/library.db`); table cells are JSON in `tables.rows`. For the tables themselves
as spreadsheets use the Excel / CSV / JSON exports.

## Extraction engines (`backend/app/extract/`)

| id       | how |
|----------|-----|
| `auto`   | ruled tables from `lines`; a ruled frame around an open body is re-split with `stream`; then `stream` for the remaining borderless tables |
| `lines`  | `page.find_tables(strategy="lines")` for the grid; cell text from our own word index |
| `stream` | borderless tables: words → lines → column bands from numeric rows, with header/caption/footer detection |
| `text`   | pdfspine's built-in `text` strategy, unmodified, for comparison |
| `tatr`   | Table Transformer via pdfspine (`strategy="vision", backend="tatr"`), **not enabled yet** |

The word index (`words.py`) fixes two problems in BPS PDFs:

- **fake-bold shadow glyphs**, which would otherwise read "6.342" as "66..334422";
- **rotated watermarks**, which are dropped.

Accuracy, measured against the old project's hand-checked ground truth (4 PDFs, 164 pages, 351 rows):

```
                       auto     lines   stream
row exact              94.0%    35.3%   83.8%
values in own cell    100.0%    44.5%   95.9%
false-pos tables (pages)  2        0        2
total time            ~1.8 s   ~1.6 s   ~1.2 s
```

(The old `auto` with torch-based TATR scored 98.6% and took 54 s.) Of the rows `auto` misses, 19 are in a text-only schedule table with no numbers, which is the kind of table TATR should handle.

```bash
mise run score -- --gt ../../pdf-table-viewer/groundtruth ../../pdf-table-viewer/*.pdf --verbose
```

### Enabling Table Transformer later

```bash
cd backend && uv add "pdfspine[tatr]"     # torch + transformers
```

The engine then appears automatically in the UI. Pre-fetching the two pinned models is described in pdfspine's tables docs.

## Reviewing tables

**Workspace** (`/doc?id=…&table=…`)

- Each table has a **title** (from the caption: "Tabel 2.1 …", "Lampiran 1.7 …") and **notes** (the "Sumber : …", "Catatan: …" and footnote lines under it). Both are filled in automatically and can be edited.
- Click a box to select it. **Drag** the selected box to move it, or pull its **handles** to resize it. The region is then extracted again.
  If you already edited the table, you're asked first. The previous version stays in history either way.
- Flag a region as **Valid table** (`v`) or **Not a table** (`x`).
- **Delete** is a soft delete: the table can be restored, or deleted permanently.
- Editor:
  - Edit cells.
  - Select cells by dragging, shift-clicking, or clicking row/column headers.
  - **Merge** and **Unmerge** cells.
  - Insert or delete rows and columns.
  - Join wrapped rows.
  - Set the number of header rows.
- Changing cells, merges, rows or columns sets the **edited** flag. **Undo/Redo** also steps back through versions saved in SQLite, so it works after a reload. **Reset** goes back to the extracted version.
- Re-running extraction never overwrites tables that are edited, flagged, deleted or drawn by hand.
- **Join** tables that continue across pages. When the next table repeats the header, a banner suggests the join ("Join p44·T1").
  - The **Join** menu appends any of the next tables, or appends this table to the previous one.
  - When joining, header rows that repeat the first table's header are dropped.
  - Tables split across pages by **width** (same row labels, e.g. columns (1)–(3) on one page and (1), (4), (5) on the next) are joined **as columns**: rows are matched on the first (label) column, and the repeated label column is dropped. This is detected automatically; the Join menu also offers "Join Below" and "Join as Columns" to choose explicitly.
  - The joined table keeps a link to every page region; each continuation is drawn as a dashed "part" box.
  - **Unjoin** puts both tables back.
  - Exports label a joined table with its page range ("halaman 43–44").

- In a joined table, the editor marks where each page's rows begin (a `p69` tag and a divider line).
  - Clicking a part (`p69`) in the list, or its dashed box on the PDF, scrolls the editor to that page's rows and selects them.
  - The status bar shows which page the current row came from.
  - Right-click a cell and choose **Show page N in PDF** to jump to that page's region.
- **Layout** (grid button next to *Get all tables*). The panes are a = PDF, b = table list, c = editor.
  - Layouts: `a | b/c`, `a | tab(b,c)`, `b | a | c`, `b | tab(a,c)`, `b/c | a`, `a / (b | c)`, `tab(a,b,c)`.
  - Drag a divider to resize it; double-click a divider to reset.
  - Double-click a table in the list to bring the editor tab forward.
  - The chosen layout, pane sizes and active tabs are remembered.

**Tables** (`/tables`) lists every table in the library. By default it shows tables flagged valid.

- Filter by flag: any mix of *no flag / valid / not a table / marked as deleted*, plus *edited only*.
- Filter by source type (Publikasi / BRS / upload) and by document. Search titles, notes and document names.
- Each card shows:
  - the PDF region rendered by pdfspine;
  - a preview of the table;
  - where it comes from (document, page, position on the page);
  - **Open in PDF**, which jumps to the table in the workspace.
- Click the image to see the PDF region and the extracted table side by side.
- Bulk actions: flag, delete, restore, and export the selection to XLSX / CSV-zip / JSON.

Exports include the title, the notes (under the table in XLSX) and merged cells (real merged ranges in XLSX, a `merges` list in JSON). Document exports skip deleted tables and "not a table" by default; you can also choose *valid only*.

## Design

The UI follows the [KDE Human Interface Guidelines](https://develop.kde.org/hig/) with square corners. See
[DESIGN.md](DESIGN.md) for colors, components, feedback (undo, errors, progress), wording, keyboard shortcuts, layout
and the accessibility check (`mise run check:a11y`).

## API (short)

```
GET  /api/bps/search?kind=publication|pressrelease&domain=0000&keyword=&year=&month=&page=&lang=
GET  /api/bps/domains
GET  /api/locate[?lat=&lon=]    {domain,region,country,source}: the user's BPS province (browser coordinates via OSM Nominatim, else ip-api.com)
POST /api/bps/import            {kind,id,domain,lang,extract}
GET  /api/documents             POST /api/documents (multipart file, extract)
GET  /api/documents/{id}        PATCH {title?,category?}  DELETE ...   /pdf   /thumb
POST /api/documents/move        {ids,category}        POST /api/documents/{id}/auto-category
POST /api/categories/rename     {path,to}             (rename / move a category with everything below it; to=[] = uncategorise)
POST /api/documents/{id}/jobs   {engine,pages?,replace}     GET /api/jobs/{id}   POST /api/jobs/{id}/cancel
POST /api/documents/{id}/extract {page,bbox?,engine}        (region, bbox in PDF points)
GET  /api/documents/{id}/tables POST (empty table)
GET  /api/tables?state=valid,none,invalid,deleted&edited=&docs=&kinds=&q=&sort=&offset=&limit=   (+ facet counts)
GET  /api/tables/{id}           (with document reference)
PATCH /api/tables/{id} {title,notes,rows,merges,header_rows,status,deleted,bbox,checkpoint}
POST /api/tables/{id}/reextract {bbox?,engine}   POST /api/tables/{id}/reset
GET  /api/tables/{id}/versions  POST /api/tables/{id}/versions/{vid}/restore
DELETE /api/tables/{id}[?purge=true]             POST /api/tables/bulk {ids,action: valid|invalid|none|delete|restore|purge}
GET  /api/tables/{id}/snapshot.png               (PDF region, rendered by pdfspine)
GET  /api/tables/{id}/export?format=csv|xlsx|json
GET  /api/tables-export?ids=a,b&format=xlsx|zip|json
GET  /api/documents/{id}/export?format=xlsx|zip|json&include=usable|valid|all&decimal=auto|,|.&title=false
```
**Categories.** Documents carry a category path of any depth (`["Statistik Ekonomi", "Transportasi"]`, `[]` = Uncategorized),
shown as a tree in the Library. BPS imports are filed automatically under the BPS subject classification (CSA: subject
category / subject, `backend/app/categories.py`); when BPS gives no subject (common for regional publications) the title is
matched against keywords, otherwise `Lainnya`. Uploads start in Uncategorized (or the category open in the Library).

XLSX export: the first sheet is `Index` (one row per table, linked to its sheet: title, document, category, page, flag, …),
then one sheet per table named `s001`, `s002`, … holding only the table (from A1, header rows bold, numbers as numbers).
`title=true` also writes the table title above it and the notes below.
The decimal separator is chosen per table: `decimal=,` / `decimal=.` forces it for every table; `auto` (default) uses the
table's own setting (`PATCH {decimal_sep: "auto"|","|"."}`, the "Decimal" control in the editor), and when that is `auto`
it is detected from the body cells: `10.498,2`, `1.234.567`, `25,9` vote strongly, ambiguous `1.234` only breaks ties,
and with no evidence the Indonesian `,` is kept.

**Local settings** (`/settings`, `frontend/src/lib/settings.ts`) live in the browser's localStorage only: default BPS
region (detected from the browser location, else the internet address; or a fixed region), remembering the last search
filters (never the keyword), result language, Library view, finding tables after upload, and export defaults.
