// Event sources. The UI never talks to Exasol: it only consumes the event stream.
//   live     WebSocket /ws on the orchestrator
//   fixture  the illustrative fixture, played client-side (no backend needed) — always labelled REPLAY
//   server   /ws?replay=<path> — a recorded real run, played by the orchestrator — labelled REPLAY

import type { DrydockEvent } from "./types";

export type Source = { kind: "live" } | { kind: "fixture" } | { kind: "server"; path: string };

export function connect(src: Source, onEvent: (e: DrydockEvent) => void, onStatus: (s: string) => void,
                        delayMs = 350): () => void {
  let stopped = false;
  if (src.kind === "fixture") {
    onEvent({ type: "__replay__", ts: "", run_id: null, branch_id: null, payload: {}, ...({ path: "fixture (illustrative)" } as any) });
    onStatus("replay");
    fetch("/fixtures/run_replay.jsonl").then((r) => r.text()).then(async (text) => {
      const events = text.split("\n").filter(Boolean).map((l) => JSON.parse(l) as DrydockEvent);
      for (const e of events) {
        if (stopped) return;
        onEvent(e);
        await new Promise((res) => setTimeout(res, e.type === "diff.computed" || e.type === "merge.gated" ? delayMs * 4 : delayMs));
      }
      onStatus("replay-done");
    });
    return () => { stopped = true; };
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const qs = src.kind === "server" ? `?replay=${encodeURIComponent(src.path)}&delay=${delayMs / 1000}` : "";
  let ws: WebSocket | null = null;
  let retry: number | undefined;
  const open = () => {
    ws = new WebSocket(`${proto}://${location.host}/ws${qs}`);
    ws.onopen = () => {
      // The orchestrator replays its whole history to every connection. After a reconnect, applying it on top of
      // what is already on screen would double every entry and keep cards the server no longer has.
      if (src.kind === "live") onEvent({ type: "__reset__", ts: "", run_id: null, branch_id: null, payload: {} });
      onStatus(src.kind === "live" ? "live" : "replay");
    };
    ws.onmessage = (m) => onEvent(JSON.parse(m.data));
    ws.onclose = (ev) => {
      if (stopped) return;
      if (ev.code === 4401) {                     // protected server, no valid token: ask, don't hammer it
        onStatus("locked");
        window.dispatchEvent(new CustomEvent("drydock:auth"));
        return;
      }
      onStatus("offline");
      if (src.kind === "live") retry = window.setTimeout(open, 2000);
    };
  };
  open();
  return () => { stopped = true; clearTimeout(retry); ws?.close(); };
}
