# Drydock: 3-minute pitch script

About 370 spoken words, roughly 2 min 40 s at a calm pace. The slides are in
[Drydock-Pitch-Deck.pdf](Drydock-Pitch-Deck.pdf). Every number comes from a run on
Exasol Personal 2026.2; a fresh run gives slightly different figures, so read them off the screen.

| Time | SHOW (screen + action) | SAY (word for word) |
|---|---|---|
| **0:00–0:18** Problem | Title card "DRYDOCK — safe writes for AI agents", then the **Overview** tab. | "AI agents can now talk to your database. Reading is easy. Writing is where it gets dangerous — one wrong UPDATE and your customer master is gone. That's why Exasol's own MCP server ships with writes switched off." |
| **0:18–0:40** Solution | **Live System** tab. Trace with the cursor: *Drydock MCP → Branches → Orchestrator's merge gate → GOLDEN*. | "Drydock is the missing piece: a governed write path for AI agents on Exasol. The agent never touches production. Every change runs on its own branch — a real copy inside Exasol. A person sees the exact rows that would change, approves them row by row, and any merge can be undone exactly." |
| **0:40–1:05** Live run | *Pre-recorded, sped up:* **Start New Run** (scripted, gate on, tier 2) with **Live System** open in **Slow motion**: dots labelled "copy-on-write · 36,600 rows", "row-level diff · … changed", "gate held … for a person". | "Here a scripted, reproducible playbook drives it, reconciling two messy customer systems — 69,000 records, with 400 look-alike traps planted: fathers and sons, spouses, people and their companies. Everything moving is a real event: Exasol copies production in about a second, hashes every row for the exact diff, and the gate decides." |
| **1:05–1:28** Matching + AI | **Reconcile** → filter **Split votes** → open a pair. Hover the three dots, then the **Gemini** verdict pill (tooltip: "saw a comparison summary only"). | "Three matchers vote in SQL, inside Exasol. They settled 98.5% of 30,409 pairs on their own. Only the 461 they disagreed on went to Gemini — and Gemini sees a comparison summary, never a name or an email. Its answer is advice. Those rows still wait for a person." |
| **1:28–2:05** Gate, proof, undo | **Merge Gate**: click through the held requests and their reasons (split rows; *DELETE_PCT 1.46% > 1.0%*). **Diff Viewer**: split rows unticked, untick one more → **MERGE N OF M**. *Split screen:* Exasol console `SELECT COUNT(*) FROM GOLDEN.CUSTOMERS;` before and after merging INTERNAL_DEDUP → **↩ unmerge** → count restored, **FP_MATCH ✓**. | "The agent proposed around 28,000 merges. The gate let the safe batch through and held the rest: these for split votes, this one because it would delete 1.46 percent of production. I review, untick, merge — staged, fingerprint-checked, then swapped in atomically. Exasol's own console confirms the count. And undo is exact: the fingerprint matches, byte for byte." |
| **2:05–2:30** Backend | **Live System**: click the **Exasol** box (inspector: version, session, tiers from TIER_CONFIG), then the **Orchestrator** box. Optional 2-s cut to the terminal showing the live tests passing. | "Under the hood, Exasol does the work: the copies, SHA-256 row hashing, matching with EDIT_DISTANCE and SOUNDEX, transactional renames. A Python orchestrator hosts the gate. Agents connect over MCP — Exasol's official server for reads, Drydock's for writes — and Gemini 3.5 Flash adjudicates. Every statement passes a dialect firewall, and 38 tests prove the guarantees on the live instance." |
| **2:30–2:48** Results | **Runs** scoreboard, or the README's *Results on the live instance* table. | "The results: 27,980 of 27,981 proposed merges correct, 99.9% recall, all 600 duplicates found. One look-alike got through — a family all three matchers agreed on. We report it, because agreement isn't proof. That's exactly why the gate exists." |
| **2:48–3:00** Why Exasol, close | Wide shot of **Live System**, then the title card. | "Exasol's speed turns 'branch production for every AI change' from a luxury into the default — and an engine-level zero-copy clone would make it near-free at any scale. Drydock: governed changes, observed impact." |

## Likely questions

- **"Isn't a full copy per branch expensive?"** "At 36,600 rows it's about a second on a laptop. At warehouse scale
  you'd want an engine-level clone — that's the feature this makes the case for."
- **"What stops the agent writing to production?"** "Exasol does: the agent's user has no write grant anywhere. Fifteen
  forbidden actions are tested live, and GOLDEN is written by exactly one module."
- **"Why an LLM at all?"** "Only for the ambiguous 1.5%, as advice. The 98.5% is plain SQL, and every split row
  still waits for a person."
- **"How do we know the numbers are real?"** "Open Live System and press Refresh — that's Exasol's own session and
  row counts, right now. Or run `SELECT COUNT(*)` in the Exasol console, as in the video."
