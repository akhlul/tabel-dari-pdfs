import { sessionLost, type User } from './auth';

export type BBox = [number, number, number, number];

export interface Doc {
  id: string;
  title: string;
  filename: string;
  source: 'upload' | 'bps';
  source_key: string | null;
  meta: Record<string, any>;
  /** category path, e.g. ["Statistik Ekonomi", "Transportasi"]; [] = uncategorised */
  category: string[];
  page_count: number;
  pages: [number, number][];
  size: number;
  created_at: number;
  updated_at: number;
  table_count?: number;
  valid_count?: number;
  last_job_status?: string | null;
  jobs?: Job[];
  /** publication year (uploads: set by the uploader; others: guessed from the title / BPS release date) */
  year: number | null;
  tags: string[];
  created_by_name?: string | null;
}

export interface Job {
  id: string;
  document_id: string;
  engine: string;
  pages: number[] | null;
  status: 'queued' | 'running' | 'done' | 'error' | 'cancelled';
  done: number;
  total: number;
  found: number;
  error: string | null;
  created_at: number;
  finished_at: number | null;
  created_by_name?: string | null;
}

export type Status = 'none' | 'valid' | 'invalid';
export type State = Status | 'deleted';
export type Merge = [number, number, number, number]; // row, col, row_span, col_span

export interface Table {
  id: string;
  document_id: string;
  page: number;
  idx: number;
  bbox: BBox;
  original_bbox: BBox | null;
  engine: string;
  origin: 'job' | 'region' | 'manual';
  title: string;
  notes: string;
  header_rows: number;
  /** how numbers are read on export: 'auto' = detected from the cells */
  decimal_sep?: DecimalSep;
  rows: string[][];
  original_rows: string[][];
  merges: Merge[];
  original_merges: Merge[];
  edited: boolean;
  status: Status;
  deleted: boolean;
  deleted_at: number | null;
  updated_at: number;
  /** who created / last changed it (staff only; null = before logins existed) */
  created_by_name?: string | null;
  updated_by_name?: string | null;
  /** who flagged it valid / not a table, and when (staff only) */
  validated_by?: string | null;
  validated_by_name?: string | null;
  validated_at?: number | null;
  parts: Part[];
  joined_into: string | null;
  state?: State;
  pages?: number[];
  document?: TableRef;
}

/** a continuation table appended to this one (see join) */
export interface Part {
  table_id: string; page: number; bbox: BBox; rows: number; skipped: number;
  /** rows = appended below; columns = appended to the right, rows matched on the label column */
  mode?: 'rows' | 'columns'; cols?: number; extra_rows?: number;
}
export type JoinMode = 'auto' | 'rows' | 'columns';

export interface TableRef {
  id: string; title: string; kind: string; page_count: number; page_size: [number, number];
  source_pdf: string | null; date: string | null; bps_id: string | null; domain: string | null;
}

export interface Version { id: number; rows: string[][]; merges: Merge[]; header_rows: number; reason: string; created_at: number; created_by_name?: string | null }

export interface TableSearch {
  total: number; offset: number; limit: number; items: Table[];
  facets: {
    state: Record<State, number>; edited: number; kinds: Record<string, number>;
    docs: { id: string; title: string; kind: string; category: string[]; count: number }[];
    validators: { id: string; name: string; count: number }[];
  };
}

export interface BackupCounts { documents: number; tables: number; valid_tables: number }
export interface RestoreResult {
  mode: 'merge' | 'replace'; counts: BackupCounts;
  added_documents?: number; added_tables?: number; skipped_existing?: number; skipped_without_pdf?: number;
  missing_pdfs?: number; previous?: string; pdfs_in_backup?: number;
}

export type DecimalSep = 'auto' | ',' | '.';

export type TablePatch = Partial<Pick<Table, 'title' | 'notes' | 'rows' | 'merges' | 'header_rows' | 'status' | 'deleted' | 'bbox' | 'decimal_sep'>> & {
  checkpoint?: boolean;
  /** updated_at of the copy the edit is based on: 409 (ApiError.data.conflict) when someone else saved meanwhile */
  base?: number;
};

export interface Engine { id: string; label: string; description: string; available: boolean; note: string }

export interface BpsItem {
  kind: 'publication' | 'pressrelease';
  id: string;
  title: string;
  abstract: string;
  date: string | null;
  size: string | null;
  cover: string | null;
  pdf: string | null;
  subject: string | null;
  issn: string | null;
  document_id: string | null;
  /** waiting / running / failed in the import queue */
  queue: { id: string; status: ImportStatus; position: number | null; done: number; total: number | null; error: string | null } | null;
}

export type ImportStatus = 'queued' | 'downloading' | 'processing' | 'extracting' | 'done' | 'error' | 'cancelled';
export interface Import {
  id: string; source_key: string; kind: string; bps_id: string; domain: string; title: string; status: ImportStatus;
  position: number; done: number; total: number | null; document_id: string | null; job_id: string | null; error: string | null;
}
/** one row of the queue page: a BPS import, or a table search (upload / run again) */
export interface QueueItem {
  key: string; type: 'import' | 'job'; id: string; title: string; source: 'import' | 'upload' | 'rerun'; kind: string;
  cover: string | null; document_id: string | null; status: ImportStatus; group: 'running' | 'waiting' | 'finished';
  position: number | null; bytes_done: number | null; bytes_total: number | null;
  pages_done: number | null; pages_total: number | null; found: number | null; error: string | null;
  created_by: string | null; created_by_name: string | null; created_at: number; started_at: number | null;
  finished_at: number | null; job_id: string | null; source_key?: string; pages?: number[] | null;
}
export interface QueueList {
  items: QueueItem[]; paused: boolean; users: { id: string; name: string }[];
  counts: { running: number; waiting: number; done_today: number; found_today: number; active: number; done: number; error: number; cancelled: number; all: number };
}
export type EnqueueResult = { existing: true; document_id: string; source_key: string } | { existing: false; import: Import };

export interface BpsResult { page: number; pages: number; total: number; items: BpsItem[] }
export interface ImportState {
  id: string; status: 'starting' | 'downloading' | 'processing' | 'done' | 'error' | 'cancelled';
  done: number; total: number | null; document: Doc | null; job: Job | null; error: string | null;
}
export interface Domain { id: string; name: string; url: string }

export class ApiError extends Error {
  /** data: the rest of the error body, e.g. {conflict, table} for 409 on a table, {job} for a running job */
  constructor(public status: number, message: string, public data: any = null) { super(message); }
}

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: {} };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) {
    init.body = JSON.stringify(body);
    (init.headers as Record<string, string>)['Content-Type'] = 'application/json';
  }
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch {
    throw new ApiError(0, 'The server is not reachable. Check that it is running, then retry.');
  }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    let data: any = null;
    try {
      const j = await res.json();
      if (typeof j.detail === 'string') msg = j.detail;
      else if (Array.isArray(j.detail)) msg = j.detail.map((d: any) => d.msg).join('; ');
      else if (j.detail?.message) { msg = j.detail.message; data = j.detail; }
      if (j.job) data = { ...data, job: j.job };
    } catch { /* not json */ }
    if (res.status === 401) sessionLost(); // logged out meanwhile (expired, or blocked by an admin)
    throw new ApiError(res.status, msg, data);
  }
  return res.json() as Promise<T>;
}

const q = (params: Record<string, unknown>) => {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') s.set(k, String(v));
  return s.toString();
};

export const api = {
  health: () => req<{ ok: boolean; bps_key: boolean; engines: string[] }>('GET', '/api/health'),
  engines: () => req<Engine[]>('GET', '/api/engines'),

  documents: (search = '') => req<Doc[]>('GET', `/api/documents?${q({ q: search })}`),
  document: (id: string) => req<Doc>('GET', `/api/documents/${id}`),
  renameDocument: (id: string, title: string) => req<Doc>('PATCH', `/api/documents/${id}`, { title }),
  patchDocument: (id: string, p: { title?: string; year?: number; tags?: string[]; category?: string[] }) => req<Doc>('PATCH', `/api/documents/${id}`, p),
  setCategory: (id: string, category: string[]) => req<Doc>('PATCH', `/api/documents/${id}`, { category }),
  autoCategory: (id: string) => req<Doc>('POST', `/api/documents/${id}/auto-category`),
  moveDocuments: (ids: string[], category: string[]) => req<{ ok: true; changed: number }>('POST', '/api/documents/move', { ids, category }),
  renameCategory: (path: string[], to: string[]) => req<{ ok: true; changed: number }>('POST', '/api/categories/rename', { path, to }),
  deleteDocument: (id: string, purge = false) => req<{ ok: true }>('DELETE', `/api/documents/${id}?${q({ purge: purge || '' })}`),
  restoreDocument: (id: string) => req<Doc>('POST', `/api/documents/${id}/restore`),
  /** upload with progress (XHR: fetch has no upload progress) */
  upload: (file: File, extract = true, engine = 'auto', onProgress?: (done: number, total: number) => void,
    meta: { year?: number; tags?: string[]; category?: string[] | null } = {}) =>
    new Promise<{ document: Doc; job: Job | null }>((resolve, reject) => {
      const f = new FormData();
      f.append('file', file);
      f.append('extract', String(extract));
      f.append('engine', engine);
      if (meta.year) f.append('year', String(meta.year));
      if (meta.tags?.length) f.append('tags', JSON.stringify(meta.tags));
      if (meta.category) f.append('category', JSON.stringify(meta.category));
      const x = new XMLHttpRequest();
      x.open('POST', '/api/documents');
      x.upload.onprogress = (e) => onProgress?.(e.loaded, e.total || file.size);
      x.onload = () => {
        let body: any = null;
        try { body = JSON.parse(x.responseText); } catch { /* not json */ }
        if (x.status >= 200 && x.status < 300) resolve(body);
        else {
          if (x.status === 401) sessionLost();
          reject(new ApiError(x.status, body?.detail ?? `Upload failed (HTTP ${x.status})`));
        }
      };
      x.onerror = () => reject(new ApiError(0, 'The server is not reachable. Check that it is running, then retry.'));
      x.send(f);
    }),
  pdfUrl: (id: string) => `/api/documents/${id}/pdf`,
  thumbUrl: (id: string) => `/api/documents/${id}/thumb`,

  startJob: (id: string, engine: string, pages: number[] | null, replace = true) =>
    req<Job>('POST', `/api/documents/${id}/jobs`, { engine, pages, replace }),
  job: (id: string) => req<Job>('GET', `/api/jobs/${id}`),
  cancelJob: (id: string) => req<{ ok: true }>('POST', `/api/jobs/${id}/cancel`),
  extractRegion: (id: string, page: number, bbox: BBox | null, engine = 'auto') =>
    req<{ tables: Table[]; ms: number }>('POST', `/api/documents/${id}/extract`, { page, bbox, engine }),

  tables: (docId: string) => req<Table[]>('GET', `/api/documents/${docId}/tables`),
  table: (id: string) => req<Table>('GET', `/api/tables/${id}`),
  searchTables: (p: { state?: string; edited?: boolean | ''; docs?: string; kinds?: string; validator?: string; q?: string; sort?: string; offset?: number; limit?: number }) =>
    req<TableSearch>('GET', `/api/tables?${q({ ...p, state: p.state ?? '' })}`),
  createTable: (docId: string, page: number, rows?: string[][]) =>
    req<Table>('POST', `/api/documents/${docId}/tables`, { page, ...(rows ? { rows } : {}) }),
  patchTable: (id: string, patch: TablePatch) => req<Table>('PATCH', `/api/tables/${id}`, patch),
  resetTable: (id: string) => req<Table & { undo_version: number }>('POST', `/api/tables/${id}/reset`),
  reextract: (id: string, bbox: BBox | null, engine = 'auto') =>
    req<Table & { undo_version: number }>('POST', `/api/tables/${id}/reextract`, { bbox, engine }),
  versions: (id: string) => req<Version[]>('GET', `/api/tables/${id}/versions`),
  restoreVersion: (id: string, vid: number) => req<Table & { undo_version: number }>('POST', `/api/tables/${id}/versions/${vid}/restore`),
  join: (id: string, other: string, skip: number | null = null, mode: JoinMode = 'auto') =>
    req<Table & { skipped: number; mode: 'rows' | 'columns'; cols?: number }>('POST', `/api/tables/${id}/join`, { other, skip, mode }),
  joinMany: (ids: string[]) => req<Table & { skipped: number; joined: number }>('POST', '/api/tables/join-many', { ids }),
  unjoin: (id: string, other: string) => req<Table>('POST', `/api/tables/${id}/unjoin`, { other }),
  deleteTable: (id: string, purge = false) => req<{ ok: true }>('DELETE', `/api/tables/${id}?${q({ purge: purge || '' })}`),
  bulk: (ids: string[], action: 'valid' | 'invalid' | 'none' | 'delete' | 'restore' | 'purge') =>
    req<{ ok: true; changed: number }>('POST', '/api/tables/bulk', { ids, action }),
  snapshotUrl: (id: string, bbox: BBox, scale = 1.5, part = 0) =>
    `/api/tables/${id}/snapshot.png?${q({ scale, part: part || '', v: bbox.map((v) => Math.round(v)).join('_') })}`,
  // title: also write each table's title (and notes) on its sheet; by default a sheet holds only the table
  tableExportUrl: (id: string, format: 'csv' | 'xlsx' | 'json', decimal = 'auto', title = false) =>
    `/api/tables/${id}/export?${q({ format, decimal, title: title || '' })}`,
  docExportUrl: (id: string, format: 'xlsx' | 'json' | 'zip', include = 'usable', decimal = 'auto', title = false) =>
    `/api/documents/${id}/export?${q({ format, include, decimal, title: title || '' })}`,
  manyExportUrl: (ids: string[], format: 'xlsx' | 'json' | 'zip', decimal = 'auto', title = false) =>
    `/api/tables-export?${q({ ids: ids.join(','), format, decimal, title: title || '' })}`,

  backupInfo: () => req<{ counts: BackupCounts; db_bytes: number; pdf_bytes: number }>('GET', '/api/backup/info'),
  backupUrl: (pdfs = true) => `/api/backup?${q({ pdfs })}`,
  restoreBackup: (file: File, mode: 'merge' | 'replace') => {
    const f = new FormData();
    f.append('file', file);
    f.append('mode', mode);
    return req<RestoreResult>('POST', '/api/backup/restore', f);
  },

  domains: () => req<Domain[]>('GET', '/api/bps/domains'),
  locate: (p: { lat?: number; lon?: number }) =>
    req<{ domain: string | null; region: string; country: string; source: 'location' | 'ip' }>('GET', `/api/locate?${q(p)}`),
  bpsSearch: (p: { kind: string; domain: string; keyword?: string; year?: number | ''; month?: number | ''; page?: number; lang?: string }) =>
    req<BpsResult>('GET', `/api/bps/search?${q(p)}`),
  bpsImport: (p: { kind: string; id: string; domain: string; lang?: string; extract?: boolean }) =>
    req<{ existing: true; document: Doc } | { existing: false; import: ImportState }>('POST', '/api/bps/import', p),
  importStatus: (id: string) => req<ImportState>('GET', `/api/bps/import/${id}`),
  importCancel: (id: string) => req<{ ok: true }>('POST', `/api/bps/import/${id}/cancel`),

  // the queue: BPS imports and table searches
  queue: (p: { status?: string; source?: string; by?: string } = {}) => req<QueueList>('GET', `/api/queue?${q(p)}`),
  queueCount: () => req<{ active: number; paused: boolean }>('GET', '/api/queue/count'),
  enqueue: (items: { kind: string; id: string; domain: string; lang?: string; extract?: boolean; title?: string; cover?: string | null; size?: string | null }[]) =>
    req<{ results: EnqueueResult[] }>('POST', '/api/queue/imports', { items }),
  cancelImport: (id: string) => req<Import>('POST', `/api/queue/imports/${id}/cancel`),
  cancelImports: (ids: string[]) => req<{ ok: true }>('POST', '/api/queue/imports/cancel', { ids }),
  retryImport: (id: string) => req<Import>('POST', `/api/queue/imports/${id}/retry`),
  moveImport: (id: string, to: 'up' | 'top') => req<{ ok: true }>('POST', `/api/queue/imports/${id}/move`, { to }),
  removeImport: (id: string) => req<{ ok: true }>('DELETE', `/api/queue/imports/${id}`),
  retryJob: (id: string) => req<Job>('POST', `/api/queue/jobs/${id}/retry`),
  dismissJob: (id: string) => req<{ ok: true }>('DELETE', `/api/queue/jobs/${id}`),
  pauseQueue: (paused: boolean) => req<{ paused: boolean }>('POST', '/api/queue/pause', { paused }),
  clearQueue: () => req<{ ok: true; removed: number }>('POST', '/api/queue/clear'),

  // admin: users (password login and / or Google login by e-mail)
  users: () => req<User[]>('GET', '/api/users'),
  createUser: (u: UserInput) => req<User>('POST', '/api/users', u),
  patchUser: (id: string, u: UserPatch) => req<User>('PATCH', `/api/users/${id}`, u),
  deleteUser: (id: string) => req<{ ok: true }>('DELETE', `/api/users/${id}`),
  logoutUser: (id: string) => req<{ ok: true }>('POST', `/api/users/${id}/logout`),
};

export interface UserInput { username?: string | null; email?: string | null; name?: string; role: 'editor' | 'admin'; password?: string | null }
export type UserPatch = Partial<Omit<UserInput, 'role'>> & { role?: 'editor' | 'admin'; active?: boolean; remove_password?: boolean; unlink_google?: boolean };

/** plain-language names for extraction methods (HIG: no jargon) */
export const ENGINE_NAME: Record<string, string> = {
  auto: 'Automatic', lines: 'Ruled table', stream: 'Borderless table', 'lines+stream': 'Ruled frame, borderless body',
  text: 'Text layout', tatr: 'Table Transformer', manual: 'Added by hand',
};
