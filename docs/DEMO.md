# Demo video script: Precedent Graph (about 3½ minutes)

**Before recording**
- Open https://precedents.cyruslogic.net in Chrome and press **Cmd+Shift+R** for a hard refresh.
- Zoom the browser to about 110%. Close other tabs and notifications.
- Have a terminal ready with Claude Code installed, for the MCP segment.
- Record one take per scene, then join them. Pause about a second after each action so the graph settles.
- Use the **case check** box above the graph for scenes 2–4 (it's instant and uses no LLM), and the **chat box** on the left for scenes 5–6.

---

## Scene 1: The problem (0:00–0:25)
**Screen:** the landing page.

> "In Indian litigation, citing a judgment that's no longer good law can sink an argument. Checking isn't simple. The overruling might be buried in any later judgment, the labels can mislead, and sometimes the overruling itself gets overruled.
>
> Precedent Graph maps how 12,600 Supreme Court judgments from 2010 to 2025 treated each other, using the court's own editorial notes. That's about 63,000 links in a FalkorDB graph. An AI agent uses that graph as its context."

---

## Scene 2: The three verdicts (0:25–1:15)

**2a. Clean: Lalita Kumari**
**Action:** type `Lalita Kumari` in the case check and pick *Lalita Kumari v. Govt. of U.P.* [2013] 14 S.C.R. 713.
**Screen:** green stamp **NO NEGATIVE TREATMENT FOUND**, with green "relied on" and "followed" links fanning in.

> "Start with a landmark: Lalita Kumari, the 5-judge ruling that police must register an FIR. Twenty-five later judgments rely on it or follow it, right up to 2025, and nothing in the corpus overrules or doubts it. Green."

**2b. Overruled: SMS Tea Estates**
**Action:** type `SMS Tea Estates` and pick [2011] 9 S.C.R. 382.
**Screen:** red stamp **NOT GOOD LAW**, *by In Re: Interplay… (7-judge bench)*.

> "Now SMS Tea Estates, on unstamped arbitration agreements. Not good law. It was overruled by the 7-judge bench in *In Re: Interplay* in December 2023."

**2c. The trap: Garware Wall Ropes**
**Action:** type `Garware Wall Ropes` and pick [2019] 5 S.C.R. 579.
**Screen:** amber stamp **CAUTION: OVERRULING JUDGMENT WAS ITSELF OVERRULED**. Point at the chain: Garware ← *N.N. Global* (5 judges) ← *In Re: Interplay* (7 judges).

> "This is where most checks go wrong. Garware was overruled by a 5-judge bench in April 2023, so a quick check calls it dead. But eight months later a 7-judge bench overruled *that* judgment. The graph walks one more hop and flags it as a caution: Garware may be partly revived, so read the 7-judge judgment before you rely on it, or before you dismiss it."

---

## Scene 3: The graph finds what the labels miss (1:15–2:00)
**Action:** type `Pune Municipal Harakchand` and pick [2014] 1 S.C.R. 783.
**Screen:** red stamp **NOT GOOD LAW**, *inferred: Indore Development Authority… (5-judge bench)*. Point at the six red 2-judge nodes, then at the **dashed** Indore node.

> "Pune Municipal Corporation was a 3-judge decision. Six later judgments mark it 'overruled', but all six are 2-judge benches, and a 2-judge bench can't overrule a 3-judge bench. They're reporting an overruling, not doing it. So who actually overruled it?
>
> The graph works it out. All six of those judgments cite one 5-judge Constitution Bench, *Indore Development Authority* from 2020, and Indore cites Pune Municipal itself. Indore's own editorial note only says 'referred to'. The labels never connect the two; the graph does. The dashed box marks that inference."

**Action:** click one of the red 2-judge nodes.
**Screen:** the side card shows the paragraph where the court noted the overruling.

> "And every claim is traceable: click any case to read the paragraph where the court said it."

---

## Scene 4: The agent, multi-hop (2:00–2:45)
**Action:** in the chat box, click the sample question *"What did the 7-judge bench on unstamped arbitration agreements overrule, and who is affected downstream?"*
**Screen:** tool-call chips appear one by one (`search cases`, `get case`, `check good law` ×3, `citing cases`), the answer streams in, the graph grows on the right, and the stamp appears.

> "Now the agent. Ask it a research question in plain English. It decides which graph tools to call: find the bench, check each case it touched, then trace who downstream still relies on them. The answer cites every case with its SCR citation and bench size, and the graph on the right shows exactly what it reasoned over. Nothing here comes from the model's memory; every fact comes from a FalkorDB query."

**Optional, if time allows:** ask *"Which judgments have been held per incuriam, and by how large a bench?"* to show a search across the whole corpus (34 results, grouped by bench size).

---

## Scene 5: MCP, your own AI assistant (2:45–3:15)
**Screen:** a terminal.
**Action:**
```bash
claude mcp add --transport http precedents https://precedents.cyruslogic.net/mcp/
claude
```
Then type: `Use precedents to check whether Garware Wall Ropes is still good law.`
**Screen:** Claude Code calls `check_good_law` and answers with the same caution verdict.

> "The same ten graph tools are exposed as an MCP server. Any MCP client, here Claude Code, can plug this graph in as context. A firm's own AI assistant can check precedents without ever opening our website."

---

## Scene 6: Close (3:15–3:30)
**Screen:** back to the app, or the GitHub README.

> "Precedent Graph: FalkorDB as the context layer, with multi-hop Cypher, PageRank and shortest paths, explainable evidence, and MCP. It's live at precedents.cyruslogic.net, and the code is on GitHub. It's a research aid, not legal advice, but it catches the traps that manual citation checking misses."

---

## If something goes wrong while recording
- **The stamp shows the wrong case:** do a hard refresh (Cmd+Shift+R) and repeat.
- **The agent is slow:** answers take 15–40 seconds. Cut the wait in editing, or speed that part up 2×.
- **The graph looks cramped:** scroll to zoom, or drag nodes apart before you narrate.
