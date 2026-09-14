import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { initialState } from "../reducer";
import type { Snapshot, State } from "../types";
import { DatabaseView } from "./Database";

// The Database view draws the object tree from db.snapshot — what Exasol reported — and nothing else.
// Rows are read on demand, which is why a replay shows a notice rather than invented rows.

const snapshot: Snapshot = {
  db_time: "2026-09-15 12:00:00", session: "17", version: "8.32.0", ms: 12, trigger: "test",
  ts: "2026-09-15T12:00:00+00:00",
  tables: [
    { schema: "GOLDEN", table: "CUSTOMERS", rows: 12486, kind: "TABLE" },
    { schema: "GOLDEN", table: "CUSTOMERS__ARCH_7", rows: 12400, kind: "TABLE" },
    { schema: "SOURCE_A", table: "CUSTOMERS", rows: 8000, kind: "TABLE" },
    { schema: "BENCH", table: "TRUTH", rows: 30, kind: "TABLE" },
    { schema: "GOLDEN_V", table: "CUSTOMERS", rows: null, kind: "VIEW" },
  ],
  columns: { "GOLDEN.CUSTOMERS": [["CUSTOMER_ID", "DECIMAL(18,0)"], ["FULL_NAME", "VARCHAR(200) UTF8"]] },
  keys: [{ schema: "GOLDEN", table: "CUSTOMERS", column: "CUSTOMER_ID", kind: "PRIMARY KEY", ref: null }],
  config: { tiers: [], planner_model: "m", adjudicator_model: "m", adjudicator_rung: "r", mcp_row_limit: 50, mcp_schema_pattern: "x" },
};

const state = (snap: Snapshot | null): State => ({ ...initialState, system: { snapshot: snap } });
const html = (snap: Snapshot | null, replay = false) => renderToStaticMarkup(<DatabaseView s={state(snap)} replay={replay} />);

describe("Database view", () => {
  it("lists the schemas and tables Exasol reported", () => {
    const out = html(snapshot);
    for (const name of ["GOLDEN", "SOURCE_A", "BENCH", "GOLDEN_V", "CUSTOMERS", "TRUTH"]) expect(out).toContain(name);
    expect(out).toContain("12,486");
  });

  it("hides GOLDEN's __ARCH_/__NEW_ working copies, which are not tables a reviewer browses", () => {
    expect(html(snapshot)).not.toContain("CUSTOMERS__ARCH_7");
  });

  it("says it is waiting rather than inventing a catalogue when no snapshot has arrived", () => {
    const out = html(null);
    expect(out).toContain("Waiting for Exasol");
    expect(out).not.toContain("SOURCE_A");          // no tree, and no placeholder schema either
  });

  it("keeps GOLDEN's fingerprint history and the branch copies on the overview", () => {
    const out = html(snapshot);
    expect(out).toContain("GOLDEN.CUSTOMERS");
    expect(out).toContain("No fingerprint observed yet.");
    expect(out).toContain("No table has been copied into a branch yet.");
  });
});
