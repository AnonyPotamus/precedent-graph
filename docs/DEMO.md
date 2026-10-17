# Demo video script (about 3 minutes)

**0:00–0:20: The problem.**
"In India, citing an overruled judgment can sink an argument. Editorial reports do note how later cases treated each precedent, but that information is scattered across thousands of judgments, and the labels can mislead. Precedent Graph turns 12,600 Supreme Court judgments into a FalkorDB citation graph and gives an AI agent that graph as its context."

**0:20–1:00: Direct check, no LLM.**
Type *SMS Tea Estates* into the case check.
- The stamp reads **NOT GOOD LAW**: the 7-judge *In Re: Interplay* bench overruled it in December 2023.

Then check *Garware Wall Ropes*.
- The stamp reads **Caution: overruling judgment was itself overruled**.
- Garware was overruled by the 5-judge *N.N. Global* bench (April 2023), but the 7-judge bench overruled *N.N. Global* eight months later.
- Say: "A label-reader would tell you Garware is dead. The graph walks one more hop and tells you to go and read the 7-judge judgment."

**1:00–1:45: The inference moment.**
Check *Pune Municipal Corporation v. Harakchand*.
- Six 2-judge benches mark it overruled, but a 2-judge bench can't overrule a 3-judge decision.
- The agent finds the one 5-judge judgment that all six cite and that cites *Pune Municipal* itself: *Indore Development Authority* (2020).
- That judgment's own editorial label only says "referred to". The graph recovers what the label misses. (Click a red node to read the paragraph where the court noted the overruling.)

**1:45–2:35: The agent.**
Ask: *"What did the 7-judge bench on unstamped arbitration agreements overrule, and who is affected downstream?"*
- Show the tool calls streaming as chips: `search_cases`, then `get_case`, then `check_good_law` for each overruled case, then `citing_cases`.
- Show the answer, with SCR citations, bench sizes and the evidence graph.

**2:35–2:55: MCP.**
Run `claude mcp add --transport http precedents <url>/mcp/` in a terminal, then ask Claude Code: "is Vidya Drolia still good law?" The same graph tools answer.

**2:55–3:00: Close.**
"FalkorDB is the context layer: multi-hop Cypher, PageRank, shortest paths and full-text search, all queried live by the agent."
