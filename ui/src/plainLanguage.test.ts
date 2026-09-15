import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { checkWord, explainError, explainReason, steps } from "./derive";
import { initialState, reduce, replayAll } from "./reducer";
import type { DrydockEvent } from "./types";

const fixture: DrydockEvent[] = readFileSync(new URL("../../tests/fixtures/run_replay.jsonl", import.meta.url), "utf8")
  .split("\n").filter(Boolean).map((l: string) => JSON.parse(l));

const ev = (type: string, payload: Record<string, unknown> = {}, run_id: string | null = null): DrydockEvent =>
  ({ type, ts: "2026-09-15T10:00:00.000+00:00", run_id, branch_id: null, payload }) as DrydockEvent;

describe("gate reasons in plain language", () => {
  it("explains the reasons a beginner will meet first", () => {
    expect(explainReason("ROWS_NEED_REVIEW (20 panel-split rows)")).toMatch(/disagreed on 20 rows/);
    expect(explainReason("DELETE_PCT (1.44% > 1.0%)")).toMatch(/delete 1\.44% of your list.*up to 1\.0%/);
    expect(explainReason("WITHIN_TIER")).toMatch(/merged automatically/);
    expect(explainReason("TIER_0_CANNOT_MERGE")).toMatch(/never merges on its own/);
    expect(explainReason("DELETE_PCT_EXCEEDED")).toMatch(/Blocked at every tier/);
  });

  it("passes an unknown reason through rather than inventing one", () => {
    expect(explainReason("SOMETHING_NEW")).toBe("SOMETHING_NEW");
  });
});

describe("errors in plain language", () => {
  it("turns an unsigned-verification failure into the fix that works", () => {
    const x = explainError("NotVerified: required verification not PASS: V6=TAMPERED");
    expect(x.title).toMatch(/safety checks/);
    expect(x.fix).toMatch(/same terminal/);
  });

  it("describes check statuses without jargon", () => {
    expect(checkWord("TAMPERED")).toMatch(/different secret word/);
    expect(checkWord("UNSIGNED-KEY-MISSING")).toMatch(/no secret word/);
  });
});

describe("a run that stops", () => {
  const started = ev("run.started", { tier: 2, gate_enabled: true, mode: "scripted", seed: 1, min_confidence: 0 }, "r1");
  const stopped = ev("run.ended", { status: "ERROR", summary: { error: "NotVerified: V6=UNKNOWN" } }, "r1");

  it("keeps the error for the screen", () => {
    const s = [started, stopped].reduce(reduce, initialState);
    expect(s.runs.r1.ended).toEqual({ status: "ERROR", error: "NotVerified: V6=UNKNOWN" });
  });

  it("is not shown as complete", () => {
    const st = steps([started, stopped].reduce(reduce, initialState));
    expect(st.at(-1)!.status).not.toBe("done");
    expect(st.some((x) => x.status === "failed" && x.note === "Stopped")).toBe(true);
  });
});

describe("resetting the stream", () => {
  it("drops everything built so far but keeps what Exasol last reported", () => {
    const built = replayAll(fixture);
    const snap = { ...built, system: { snapshot: { version: "x" } as any } };
    const after = reduce(snap, ev("__reset__"));
    expect(after.narrative).toEqual([]);
    expect(Object.keys(after.branches)).toEqual([]);
    expect(after.system.snapshot).toEqual({ version: "x" });
  });

  it("replaying the same history after a reset gives the same screen, not a doubled one", () => {
    const once = replayAll(fixture);
    const twice = [...fixture, ev("__reset__"), ...fixture].reduce(reduce, initialState);
    expect(twice.narrative.length).toBe(once.narrative.length);
    expect(Object.keys(twice.branches)).toEqual(Object.keys(once.branches));
  });
});

describe("a new dataset", () => {
  it("is announced in the narrative", () => {
    const s = reduce(initialState, ev("dataset.activated", { name: "upload", label: "crm.csv + shop.csv", rows_a: 400,
      rows_b: 391, golden_rows: 400, scored: false, files: null, quality: null }));
    expect(s.narrative.at(-1)!.text).toMatch(/crm\.csv \+ shop\.csv.*no answer key/);
  });
});
