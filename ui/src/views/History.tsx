// One customer's full story: what the clean list holds now, where each detail came from, the source
// records behind it, why Drydock judged them the same person, and every change that touched the row —
// who decided, when, and whether it was later undone. Read from GET /golden/{id}/history (read-only).

import { Clock, GitBranch, History as HistoryIcon, ShieldCheck, Table2, Undo2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import type { CustomerHistory } from "../types";
import { Chip } from "../ui";

const SHOWN = ["FULL_NAME", "EMAIL", "PHONE", "ADDR_LINE", "CITY", "POSTCODE", "COUNTRY", "DATE_OF_BIRTH"];
const LABEL: Record<string, string> = {
  FULL_NAME: "Name", EMAIL: "Email", PHONE: "Phone", ADDR_LINE: "Address", ADDRESS: "Address", CITY: "City",
  POSTCODE: "Postcode", COUNTRY: "Country", DATE_OF_BIRTH: "Date of birth", ALL: "Every detail",
};
const ORIGIN: Record<string, string> = { A: "System A", B: "System B", B_newer: "System B (the newer record)" };
const VOTE: Record<string, string> = { MERGE: "same person", REJECT: "different people", KEEP: "different people" };

export function HistoryDrawer({ goldenId, onClose }: { goldenId: string; onClose: () => void }) {
  const [h, setH] = useState<CustomerHistory | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setH(null);
    setErr(null);
    api.history(goldenId).then((x) => live && setH(x)).catch((e) => live && setErr(String(e.message ?? e)));
    return () => { live = false; };
  }, [goldenId]);

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-fog/25" onClick={onClose}>
      <aside className="scroll-thin h-full w-[560px] max-w-full overflow-y-auto bg-hull shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <header className="sticky top-0 z-10 flex items-center gap-3 border-b border-rule bg-hull px-5 py-3.5">
          <HistoryIcon className="h-5 w-5 text-navy" />
          <div className="min-w-0">
            <div className="text-[15px] font-bold text-fog">Customer history</div>
            <div className="font-mono text-[12px] text-mist">{goldenId}</div>
          </div>
          <button onClick={onClose} className="ml-auto text-mist hover:text-fog" aria-label="Close"><X className="h-5 w-5" /></button>
        </header>
        {err && <div className="m-5 rounded-lg border border-flare/30 bg-flare/5 px-3 py-2 text-[13px] text-flare">{err}</div>}
        {!h && !err && <div className="px-5 py-8 text-[13px] text-mist">Reading this customer's history from Exasol…</div>}
        {h && <Body h={h} />}
      </aside>
    </div>
  );
}

function Body({ h }: { h: CustomerHistory }) {
  const s = h.survivorship ?? {};
  return (
    <div className="space-y-5 px-5 py-4 text-[13px]">
      <Section icon={Table2} title={h.exists ? "In the clean list now" : "Not in the clean list any more"}>
        {h.exists && h.record ? (
          <dl className="grid grid-cols-[120px_1fr] gap-x-3 gap-y-1">
            {SHOWN.map((c) => (
              <div key={c} className="contents">
                <dt className="text-mist">{LABEL[c]}</dt>
                <dd className="flex items-center gap-2 break-words text-fog">
                  {h.record![c] ?? <span className="italic text-mist/70">empty</span>}
                  {originOf(s, c) && <span className="shrink-0 rounded bg-deck px-1.5 py-0.5 text-[10.5px] text-mist">from {originOf(s, c)}</span>}
                </dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="text-mist">This record was merged into another one, or removed by a change below.</p>
        )}
        {h.exists && !h.survivorship && (
          <p className="mt-2 text-[12px] text-mist">Every detail is still as System A had it: no change has merged anything into this customer.</p>
        )}
      </Section>

      <Section icon={GitBranch} title={`Source records (${h.sources.length})`}>
        {h.sources.length === 0 && <p className="text-mist">No source record found for this id.</p>}
        <div className="space-y-2">
          {h.sources.map((src) => (
            <div key={`${src.system}-${src.id}`} className="rounded-lg border border-rule px-3 py-2">
              <div className="flex items-center gap-2">
                <Chip tone={src.system === "System A" ? "tide" : "sky"}>{src.system}</Chip>
                <span className="font-mono text-[12px] text-fog">{src.id}</span>
              </div>
              <div className="mt-1 text-[12px] leading-relaxed text-fog/80">
                {Object.entries(src.record).filter(([k, v]) => v && !/^(CUST_ID|CLIENT_REF)$/.test(k)).map(([k, v]) => `${k.toLowerCase().replace(/_/g, " ")}: ${v}`).join(" · ")}
              </div>
            </div>
          ))}
        </div>
      </Section>

      {h.evidence.length > 0 && (
        <Section icon={ShieldCheck} title="Why Drydock linked these records">
          <div className="space-y-2">
            {h.evidence.map((e) => (
              <div key={e.pair_id} className="rounded-lg border border-rule px-3 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-[12px]">{e.a_id} ↔ {e.b_id}</span>
                  <Chip tone={e.panel_result === "SPLIT" ? "brass" : e.verdict === "MERGE" ? "kelp" : "mist"}>
                    {e.panel_result === "SPLIT" ? "matchers disagreed" : e.panel_result === "UNANIMOUS_MERGE" ? "all 3 matchers agreed" : e.panel_result?.toLowerCase().replace(/_/g, " ")}
                  </Chip>
                  <span className="ml-auto text-[12px] text-mist">score {Number(e.score_total ?? 0).toFixed(2)}</span>
                </div>
                <div className="mt-1 text-[12px] text-fog/80">
                  Deterministic: {VOTE[e.vote_determ ?? ""] ?? e.vote_determ} · Probabilistic: {VOTE[e.vote_prob ?? ""] ?? e.vote_prob} ·
                  Skeptic: {VOTE[e.vote_skeptic ?? ""] ?? e.vote_skeptic}
                  {e.ai_verdict && <> · AI helper ({e.adjudicator}): {e.ai_verdict.toLowerCase().replace(/_/g, " ")}</>}
                </div>
                {e.rationale && <div className="mt-1 text-[12px] italic text-mist">{e.rationale}</div>}
              </div>
            ))}
          </div>
        </Section>
      )}

      <Section icon={Clock} title={`Every change to this customer (${h.changes.length})`}>
        {h.changes.length === 0 && <p className="text-mist">No change has touched this customer yet.</p>}
        <ol className="relative space-y-3 border-l border-rule pl-4">
          {h.changes.map((c) => (
            <li key={c.branch} className="relative">
              <span className={`absolute -left-[21px] top-1 h-2.5 w-2.5 rounded-full ${c.undone_at ? "bg-mist" : c.decision === "MERGED" && c.included ? "bg-kelp" : "bg-brass"}`} />
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold text-fog">{c.kind}</span>
                <Chip tone={c.change === "Deleted" ? "flare" : c.change === "Added" ? "sky" : "brass"}>{c.change.toLowerCase()}</Chip>
                {!c.included && <Chip tone="mist">left out by the reviewer</Chip>}
              </div>
              <div className="mt-0.5 text-[12px] text-fog/80">
                {c.decision === "MERGED"
                  ? <>Merged{c.decided_by ? <> by <b>{c.decided_by === "gate" ? "the gate (automatically)" : c.decided_by}</b></> : null}{c.decided_at ? ` on ${new Date(c.decided_at).toLocaleString("en-GB")}` : ""}</>
                  : c.decision === "REJECTED" ? <>Rejected{c.decided_by ? ` by ${c.decided_by}` : ""}{c.reject_code ? ` (${c.reject_code.toLowerCase().replace(/_/g, " ")})` : ""}</>
                  : c.decision ? <>Waiting: {c.decision.toLowerCase()}</> : <>Not submitted</>}
                {c.columns.length > 0 && <> · changes {c.columns.map((x) => LABEL[x] ?? x.toLowerCase()).join(", ")}</>}
              </div>
              {c.undone_at && (
                <div className="mt-0.5 flex items-center gap-1 text-[12px] text-mist"><Undo2 className="h-3 w-3" /> undone on {new Date(c.undone_at).toLocaleString("en-GB")}</div>
              )}
            </li>
          ))}
        </ol>
      </Section>
    </div>
  );
}

function originOf(s: Record<string, string>, col: string): string | null {
  if (s.ALL) return ORIGIN[s.ALL] ?? s.ALL;
  const key = col === "ADDR_LINE" || col === "CITY" || col === "POSTCODE" ? "ADDRESS" : col;
  return s[key] ? ORIGIN[s[key]] ?? s[key] : null;
}

function Section({ icon: Icon, title, children }: { icon: React.ElementType; title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 flex items-center gap-2 text-[12px] font-semibold uppercase tracking-[0.12em] text-mist"><Icon className="h-4 w-4" />{title}</h3>
      {children}
    </section>
  );
}
