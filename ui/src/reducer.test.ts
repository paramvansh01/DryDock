import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { initialState, reduce, replayAll } from "./reducer";
import type { DrydockEvent } from "./types";

const fixture: DrydockEvent[] = readFileSync(new URL("../../tests/fixtures/run_replay.jsonl", import.meta.url), "utf8")
  .split("\n").filter(Boolean).map((l: string) => JSON.parse(l));

describe("reducer", () => {
  it("is a pure function of the stream: same events, same state", () => {
    expect(JSON.stringify(replayAll(fixture))).toEqual(JSON.stringify(replayAll(fixture)));
  });

  it("does not mutate the previous state", () => {
    const frozen = Object.freeze({ ...initialState });
    expect(() => reduce(frozen, fixture[0])).not.toThrow();
    expect(frozen.events).toBe(0);
  });

  it("marks the fixture as illustrative", () => {
    expect(replayAll(fixture).illustrative).toBe(true);
  });

  it("tracks the FUZZY_NAME branch through gate, deselection and unmerge", () => {
    const s = replayAll(fixture);
    const b = s.branches["BR_9C1E77F0"];
    expect(b.gate?.decision).toBe("PENDING");
    expect(b.applied?.rows_applied).toBe(824);
    expect(b.unmerged?.fingerprint_match).toBe(true);
    expect(b.status).toBe("UNMERGED");
  });

  it("orders nothing by random: split rows come first in the diff", () => {
    const b = replayAll(fixture).branches["BR_9C1E77F0"];
    expect(b.diff?.rows[0].pair?.panel).toBe("SPLIT");
  });

  it("keeps a scoreboard per run", () => {
    const s = replayAll(fixture);
    expect(s.scores["fixture-control"].false_merges_in_golden).toBe(23);
    expect(s.scores["fixture-gated"].false_merges_in_golden).toBe(0);
  });
});
