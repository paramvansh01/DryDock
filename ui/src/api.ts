// Reviewer actions. These exist on the orchestrator's REST API and nowhere on the
// agent's MCP transport. Results come back as events on the stream; nothing here writes state.

async function call(method: string, url: string, body?: unknown) {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" },
                               body: body === undefined ? undefined : JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.message || j.detail || `${r.status}`);
  return j;
}

export const api = {
  setRows: (branchId: string, keys: string[], approved: boolean) =>
    call("POST", `/branches/${branchId}/rows`, { keys, approved }),
  approve: (mergeId: number) => call("POST", `/merges/${mergeId}/approve?by=human`),
  reject: (mergeId: number, code: string, note: string) => call("POST", `/merges/${mergeId}/reject?by=human`, { code, note }),
  unmerge: (mergeId: number) => call("POST", `/merges/${mergeId}/unmerge?by=human`),
  discard: (branchId: string, reason: string) => call("POST", `/branches/${branchId}/discard`, { reason }),
  readjudicate: (pairId: number) => call("POST", `/pairs/${pairId}/readjudicate`),
  deletePrecedent: (id: number) => call("DELETE", `/precedents/${id}`),
  snapshot: () => call("POST", "/system/snapshot"),
  startRun: (body: { run_id: string; mode: string; gate_enabled: boolean; tier: number; plan_from_run?: string | null }) =>
    call("POST", "/runs", body),
};

export const REJECT_CODES = ["WRONG_MATCH", "TOO_BROAD", "INSUFFICIENT_EVIDENCE", "POLICY", "OTHER"];
