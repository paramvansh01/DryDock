// Reviewer actions. These exist on the orchestrator's REST API and nowhere on the
// agent's MCP transport. Results come back as events on the stream; nothing here writes state.

import type { CustomerHistory, FileProfile, QualityReport, SideMapping } from "./types";

export class ApiError extends Error {
  constructor(message: string, public status: number, public body: any) { super(message); }
}

/** The server is protected (DRYDOCK_ACCESS_TOKEN) and this browser has no valid token: App asks for one. */
export const AUTH_EVENT = "drydock:auth";

async function parse(r: Response) {
  const j = await r.json().catch(() => ({}));
  if (r.status === 401) window.dispatchEvent(new CustomEvent(AUTH_EVENT));
  if (!r.ok) throw new ApiError(j.message || j.detail || `${r.status}`, r.status, j);
  return j;
}

async function call(method: string, url: string, body?: unknown) {
  return parse(await fetch(url, { method, headers: { "Content-Type": "application/json" },
                                  body: body === undefined ? undefined : JSON.stringify(body) }));
}

/** Keep the access token in a same-site cookie: every request, download and the live stream then carry it. */
export function setAccessToken(token: string): void {
  const secure = location.protocol === "https:" ? "; Secure" : "";
  document.cookie = `drydock_token=${encodeURIComponent(token.trim())}; path=/; SameSite=Strict; max-age=31536000${secure}`;
}

// ------------------------------------------------------------------ who is deciding
// Every approval, rejection and undo is recorded under this name (MERGES.RESOLVED_BY, the audit log).
const REVIEWER_KEY = "drydock.reviewer";

export function reviewer(): string {
  try { return localStorage.getItem(REVIEWER_KEY)?.trim() || "human"; } catch { return "human"; }
}

export function setReviewer(name: string): void {
  try { localStorage.setItem(REVIEWER_KEY, name.trim()); } catch { /* private mode: the default name is used */ }
}

const by = () => `by=${encodeURIComponent(reviewer())}`;

export const api = {
  setRows: (branchId: string, keys: string[], approved: boolean) =>
    call("POST", `/branches/${branchId}/rows`, { keys, approved }),
  approve: (mergeId: number) => call("POST", `/merges/${mergeId}/approve?${by()}`),
  reject: (mergeId: number, code: string, note: string) => call("POST", `/merges/${mergeId}/reject?${by()}`, { code, note }),
  unmerge: (mergeId: number) => call("POST", `/merges/${mergeId}/unmerge?${by()}`),
  discard: (branchId: string, reason: string) => call("POST", `/branches/${branchId}/discard`, { reason }),
  readjudicate: (pairId: number) => call("POST", `/pairs/${pairId}/readjudicate`),
  deletePrecedent: (id: number) => call("DELETE", `/precedents/${id}`),
  snapshot: () => call("POST", "/system/snapshot"),
  // Read-only page of a table, straight from Exasol. The browser sends no SQL: it names a
  // table, a page, a sort column and a filter, and the orchestrator composes the SELECT.
  browse: (p: { schema: string; table: string; limit: number; offset: number; order?: string | null;
                dir?: string; q?: string | null }) =>
    call("GET", `/db/rows?${new URLSearchParams({
      schema: p.schema, table: p.table, limit: String(p.limit), offset: String(p.offset),
      ...(p.order ? { order: p.order, dir: p.dir ?? "ASC" } : {}),
      ...(p.q ? { q: p.q } : {}),
    })}`) as Promise<import("./types").Page>,
  startRun: (body: { run_id: string; mode: string; gate_enabled: boolean; tier: number; plan_from_run?: string | null }) =>
    call("POST", "/runs", body),

  // ---------------------------------------------------------------- your own data
  uploadFile: async (side: "a" | "b", file: File): Promise<FileProfile> => {
    return parse(await fetch(`/uploads/${side}?filename=${encodeURIComponent(file.name)}`, { method: "POST", body: file }));
  },
  uploaded: () => call("GET", "/uploads") as Promise<{ a: FileProfile | null; b: FileProfile | null }>,
  forgetUpload: (side: "a" | "b") => call("DELETE", `/uploads/${side}`),
  checkUploads: (a: SideMapping, b: SideMapping) => call("POST", "/uploads/check", { a, b }) as Promise<QualityReport>,
  loadUploads: (a: SideMapping, b: SideMapping, label: string) =>
    call("POST", "/uploads/load", { a, b, label }) as Promise<{ ok: boolean; golden_rows: number; rows_a: number; rows_b: number; seconds: number; report: QualityReport }>,
  useDemo: () => call("POST", "/dataset/demo"),
  history: (goldenId: string) => call("GET", `/golden/${encodeURIComponent(goldenId)}/history`) as Promise<CustomerHistory>,
};

// Downloads are plain links: the browser saves the CSV the orchestrator streams.
export const downloads = {
  golden: "/export/golden.csv",
  audit: "/export/audit.csv",
  changes: (branchId: string) => `/export/changes/${encodeURIComponent(branchId)}.csv`,
  table: (schema: string, table: string) => `/export/table.csv?${new URLSearchParams({ schema, table })}`,
  sampleA: "/examples/sample_system_a.csv",
  sampleB: "/examples/sample_system_b.csv",
};

export const REJECT_CODES = ["WRONG_MATCH", "TOO_BROAD", "INSUFFICIENT_EVIDENCE", "POLICY", "OTHER"];
