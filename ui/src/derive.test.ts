import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { compareFields, filterSort, kpis, matchRows, risk, steps, suggested } from "./derive";
import { replayAll } from "./reducer";
import type { DrydockEvent } from "./types";

const fixture: DrydockEvent[] = readFileSync(new URL("../../tests/fixtures/run_replay.jsonl", import.meta.url), "utf8")
  .split("\n").filter(Boolean).map((l: string) => JSON.parse(l));
const s = replayAll(fixture);

describe("derive", () => {
  it("KPIs come from panel.voted / candidates.ready / golden.fingerprint, not constants", () => {
    const k = kpis(s);
    const panel = fixture.find((e) => e.type === "panel.voted")!.payload;
    expect(k.autoMatched).toBe(panel.unanimous_merge);
    expect(k.needsReview).toBe(panel.split);
    expect(k.rejected).toBe(panel.unanimous_reject);
    expect(k.golden).toBe(s.golden.rows);
  });

  it("KPIs are null (shown as —) before any event", () => {
    const k = kpis(replayAll([]));
    expect([k.candidates, k.autoMatched, k.needsReview, k.rejected, k.golden]).toEqual([null, null, null, null, null]);
  });

  it("steps are derived and ordered", () => {
    const st = steps(s);
    expect(st.map((x) => x.label)).toEqual(["Profile Schemas", "Find Candidates", "Review Matches", "See Diff", "Merge", "Complete"]);
    expect(st[0].status).toBe("done");
    expect(steps(replayAll([])).every((x) => x.status === "pending")).toBe(true);
  });

  it("split-vote pairs are High risk and a decoy the adjudicator rejected is suggested No match", () => {
    const rows = matchRows(s);
    const smith = rows.find((r) => r.pair.ai_verdict === "DIFFERENT_PERSON")!;
    expect(risk(smith.pair)).toBe("High");
    expect(suggested(smith.pair)).toBe("No match");
    expect(filterSort(rows, "", "all", "risk")[0].risk).toBe("High");
  });

  it("field comparison uses the declared mapping for SOURCE_B", () => {
    const r = matchRows(s)[0];
    const name = compareFields(r.pair, s.mapping).find((c) => c.field === "FULL_NAME")!;
    expect(name.b).toBe(`${r.pair.b!.FIRST_NAME} ${r.pair.b!.LAST_NAME}`);
  });
});

describe("adjudicator labels (the adjudicator is always shown)", () => {
  it("gives every adjudicator a readable label and marks an unavailable one as such", async () => {
    const { adjudicatorInfo, ADJUDICATORS } = await import("./derive");
    for (const rung of ["gemini_fallback", "gemini_unavailable", "exasol_ai", "exasol_ai_precedent"]) {
      expect(ADJUDICATORS[rung]?.label).toBeTruthy();
    }
    expect(adjudicatorInfo("gemini_fallback")?.label).toBe("Gemini");
    expect(adjudicatorInfo("gemini_unavailable")?.unavailable).toBe(true);
    expect(adjudicatorInfo("gemini_fallback")?.unavailable).toBeFalsy();
    expect(adjudicatorInfo("some_new_rung")?.label).toBe("some_new_rung");   // unknown adjudicators are never hidden
    expect(adjudicatorInfo(null)).toBeNull();
  });
});
