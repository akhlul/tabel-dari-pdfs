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
  parts: Part[];
  joined_into: string | null;
  state?: State;
  pages?: number[];
  document?: TableRef;
}

/** a continuation table appended to this one (see join) */
export interface Part { table_id: string; page: number; bbox: BBox; rows: number; skipped: number }

export interface TableRef {
  id: string; title: string; kind: string; page_count: number; page_size: [number, number];
  source_pdf: string | null; date: string | null; bps_id: string | null; domain: string | null;
}

export interface Version { id: number; rows: string[][]; merges: Merge[]; header_rows: number; reason: string; created_at: number }

export interface TableSearch {
  total: number; offset: number; limit: number; items: Table[];
  facets: {
    state: Record<State, number>; edited: number; kinds: Record<string, number>;
    docs: { id: string; title: string; kind: string; category: string[]; count: number }[];
  };
}

export interface BackupCounts { documents: number; tables: number; valid_tables: number }
export interface RestoreResult {
  mode: 'merge' | 'replace'; counts: BackupCounts;
  added_documents?: number; added_tables?: number; skipped_existing?: number; skipped_without_pdf?: number;
  missing_pdfs?: number; previous?: string; pdfs_in_backup?: number;
}

export type DecimalSep = 'auto' | ',' | '.';

export type TablePatch = Partial<Pick<Table, 'title' | 'notes' | 'rows' | 'merges' | 'header_rows' | 'status' | 'deleted' | 'bbox' | 'decimal_sep'>> & { checkpoint?: boolean };

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
}

export interface BpsResult { page: number; pages: number; total: number; items: BpsItem[] }
export interface ImportState {
  id: string; status: 'starting' | 'downloading' | 'processing' | 'done' | 'error' | 'cancelled';
  done: number; total: number | null; document: Doc | null; job: Job | null; error: string | null;
}
export interface Domain { id: string; name: string; url: string }

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
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
    try {
      const j = await res.json();
      if (j.detail) msg = typeof j.detail === 'string' ? j.detail : j.detail.map((d: any) => d.msg).join('; ');
    } catch { /* not json */ }
    throw new ApiError(res.status, msg);
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
  setCategory: (id: string, category: string[]) => req<Doc>('PATCH', `/api/documents/${id}`, { category }),
  autoCategory: (id: string) => req<Doc>('POST', `/api/documents/${id}/auto-category`),
  moveDocuments: (ids: string[], category: string[]) => req<{ ok: true; changed: number }>('POST', '/api/documents/move', { ids, category }),
  renameCategory: (path: string[], to: string[]) => req<{ ok: true; changed: number }>('POST', '/api/categories/rename', { path, to }),
  deleteDocument: (id: string, purge = false) => req<{ ok: true }>('DELETE', `/api/documents/${id}?${q({ purge: purge || '' })}`),
  restoreDocument: (id: string) => req<Doc>('POST', `/api/documents/${id}/restore`),
  /** upload with progress (XHR: fetch has no upload progress) */
  upload: (file: File, extract = true, engine = 'auto', onProgress?: (done: number, total: number) => void) =>
    new Promise<{ document: Doc; job: Job | null }>((resolve, reject) => {
      const f = new FormData();
      f.append('file', file);
      f.append('extract', String(extract));
      f.append('engine', engine);
      const x = new XMLHttpRequest();
      x.open('POST', '/api/documents');
      x.upload.onprogress = (e) => onProgress?.(e.loaded, e.total || file.size);
      x.onload = () => {
        let body: any = null;
        try { body = JSON.parse(x.responseText); } catch { /* not json */ }
        if (x.status >= 200 && x.status < 300) resolve(body);
        else reject(new ApiError(x.status, body?.detail ?? `Upload failed (HTTP ${x.status})`));
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
  searchTables: (p: { state?: string; edited?: boolean | ''; docs?: string; kinds?: string; q?: string; sort?: string; offset?: number; limit?: number }) =>
    req<TableSearch>('GET', `/api/tables?${q({ ...p, state: p.state ?? '' })}`),
  createTable: (docId: string, page: number, rows?: string[][]) =>
    req<Table>('POST', `/api/documents/${docId}/tables`, { page, ...(rows ? { rows } : {}) }),
  patchTable: (id: string, patch: TablePatch) => req<Table>('PATCH', `/api/tables/${id}`, patch),
  resetTable: (id: string) => req<Table & { undo_version: number }>('POST', `/api/tables/${id}/reset`),
  reextract: (id: string, bbox: BBox | null, engine = 'auto') =>
    req<Table & { undo_version: number }>('POST', `/api/tables/${id}/reextract`, { bbox, engine }),
  versions: (id: string) => req<Version[]>('GET', `/api/tables/${id}/versions`),
  restoreVersion: (id: string, vid: number) => req<Table & { undo_version: number }>('POST', `/api/tables/${id}/versions/${vid}/restore`),
  join: (id: string, other: string, skip: number | null = null) =>
    req<Table & { skipped: number }>('POST', `/api/tables/${id}/join`, { other, skip }),
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
};

/** plain-language names for extraction methods (HIG: no jargon) */
export const ENGINE_NAME: Record<string, string> = {
  auto: 'Automatic', lines: 'Ruled table', stream: 'Borderless table', 'lines+stream': 'Ruled frame, borderless body',
  text: 'Text layout', tatr: 'Table Transformer', manual: 'Added by hand',
};
