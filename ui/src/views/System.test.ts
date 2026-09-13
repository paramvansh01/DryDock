import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { replayAll } from "../reducer";
import type { DrydockEvent } from "../types";
import { H, W, flowsFor } from "./System";

// The Live System view may only draw what the stream says. These pin that: every flow is built from its
// event, stays on the canvas, and its label carries the event's own numbers rather than canned text.
const events: DrydockEvent[] = readFileSync(new URL("../../../tests/fixtures/run_replay.jsonl", import.meta.url), "utf8")
  .split("\n").filter(Boolean).map((l) => JSON.parse(l));
const state = replayAll(events);
const n = (v: number) => v.toLocaleString("en-GB");

describe("Live System flows", () => {
  it("maps every event without throwing, and every hop stays inside the canvas", () => {
    for (const e of events) {
      for (const f of flowsFor(e, state)) {
        for (const hop of f.hops) {
          expect(hop.length).toBeGreaterThanOrEqual(2);
          for (const [x, y] of hop) { expect(x).toBeGreaterThanOrEqual(0); expect(x).toBeLessThanOrEqual(W); expect(y).toBeGreaterThanOrEqual(0); expect(y).toBeLessThanOrEqual(H); }
        }
      }
    }
  });

  it("labels carry the payload's real numbers", () => {
    const pick = (t: string) => events.find((e) => e.type === t)!;
    const label = (e: DrydockEvent) => flowsFor(e, state).map((f) => f.label ?? "").join(" | ");
    const mat = pick("branch.materialised");
    expect(label(mat)).toContain(n(mat.payload.rows));
    const diff = pick("diff.computed");
    expect(label(diff)).toContain(n(diff.payload.changed));
    const applied = pick("merge.applied");
    expect(label(applied)).toContain(n(applied.payload.rows_applied));
    expect(label(applied)).toContain(`swap ${Math.round(applied.payload.swap_ms)} ms`);
  });

  it("draws nothing for an event type it does not know", () => {
    expect(flowsFor({ type: "made.up", ts: "", run_id: null, branch_id: null, payload: {} }, state)).toEqual([]);
  });
});
