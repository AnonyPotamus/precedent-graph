# The label said "referred to". The graph said "overruled".

*How we built Precedent Graph, an AI agent that checks whether Indian Supreme Court precedents are still good law, on a FalkorDB citation graph. Built for Graph Hacks: Context for AI Agents.*

## The problem with asking an LLM about case law

Ask a language model whether a judgment is still good law and you get a fluent answer that is often wrong. It's wrong because the question isn't about language at all. It's about the structure of the case law:
- Who cited whom?
- With what treatment?
- On how large a bench?
- In what order?

That is a graph question, so we gave the agent a graph.

## The data was hiding in plain sight

The Supreme Court's own law reports (SCR) are on AWS Open Data, under CC-BY-4.0, thanks to Dattam Labs. Every reported judgment carries an editorial list of the cases it cites, and each entry has a treatment: *relied on*, *followed*, *distinguished*, *overruled*, *per incuriam*. So the edges of a citation graph already exist, written by the court's editors. We just had to parse them.

The format has changed three times since 2010:
- OCR'd headnote lists
- a "Case Law Reference" table
- a "List of Citations" section

We wrote one parser per layout and normalised every citation (`[2011] 9 S.C.R. 382`, `2006 (6) Suppl. SCR 126`, `(2014) 4 SCC 282`) to a canonical key. We then joined the result to metadata for every reported judgment since 1950.

Citations in other law reports (SCC, AIR) don't match SCR keys directly. For those, we fuzzy-match both party names within the citation year, preferring the exact year. That resolved 2,400 more citations to real judgments. It's how the 7-judge bench's citation of "(2023) 7 SCC 1" lands on the 5-judge *N.N. Global* judgment instead of a dead-end stub.

The result is 12,783 judgments, about 63,000 citations labelled by treatment, and 30,000 cases, all loaded into FalkorDB.

## Modelling it

```
(:Judge)-[:ON_BENCH]->(:Case)-[:CITES {treatment, polarity, paras, context}]->(:Case)
(:Case)-[:INTERPRETS]->(:Provision)-[:OF]->(:Act)
```

Two choices mattered most:

1. **Polarity on the edge.** Twenty-odd treatment phrases collapse into POSITIVE, NEGATIVE, PARTIAL, CAUTION, DISTINGUISHED and NEUTRAL. That keeps queries simple, while the original phrase stays on the edge for the answer.
2. **The paragraph on the edge.** For each treated citation we store the paragraph where the court discussed it. When the agent says "overruled", the UI can show the court's own words.

## The bug that became the feature

Our first "who overruled this?" query was naive: return the judgment that labelled it *overruled*. For *Pune Municipal Corporation v. Harakchand* (a 3-judge bench), that returned six different **2-judge** benches from 2023. A 2-judge bench cannot overrule a 3-judge bench. Those editors were recording the precedent's status, not describing their own holding.

So the label isn't enough on its own, but the graph has the rest of the story. We asked FalkorDB a different question: *which later, larger-bench judgment do all of these noting judgments cite, and which itself cites Pune Municipal?*

```cypher
MATCH (n:Case)-[e:CITES]->(t:Case {key:$k}) WHERE e.polarity IN ['NEGATIVE','PARTIAL']
MATCH (n)-[:CITES]->(o:Case) WHERE o.year >= t.year AND o.bench_size >= $bench
WITH o, t, count(DISTINCT n) AS support
OPTIONAL MATCH (o)-[x:CITES]->(t)
RETURN o, support, count(x) > 0 AS cites_target
ORDER BY cites_target DESC, support DESC
```

The answer was unanimous: *Indore Development Authority v. Manoharlal* (2020), a 5-judge bench, cited by all six and citing Pune Municipal itself. Its own editorial label for Pune Municipal says only "referred to". The graph found the right answer even though the label pointed elsewhere.

## Multi-hop: is my precedent built on sand?

The second question lawyers actually need answered: *does the case I rely on itself rest on overruled authority?* That is a two-hop pattern:

```cypher
MATCH (t:Case {key:$k})-[r:CITES {polarity:'POSITIVE'}]->(y:Case)<-[n:CITES]-(z:Case)
WHERE n.polarity IN ['NEGATIVE','PARTIAL']
RETURN y, z, n.treatment
```

We added a second two-hop check that turned out to matter just as much: *was the overruling judgment itself overruled?* *Garware Wall Ropes* (2019) is marked overruled by the 5-judge *N.N. Global* bench (April 2023). In December 2023, a 7-judge bench overruled *N.N. Global*. Reading the label alone, you would call Garware dead. The graph flags it as "caution: overruling judgment was itself overruled" and points you to the 7-judge judgment to read.

## The agent

Claude gets nine tools over the graph:
- the good-law check, which runs the queries above;
- full-text search over cases and provisions;
- PageRank-ranked landmark cases;
- `algo.SPpaths` to connect two lines of authority;
- judge profiles;
- a read-only Cypher escape hatch, run with `GRAPH.RO_QUERY`.

The UI streams each tool call and draws exactly the nodes and edges the agent used, with the verdict stamped on top. The same tools are exposed over MCP, so Claude Code or any MCP client can use the graph as its context layer.

## What FalkorDB made easy

- Batched `UNWIND` loads: the whole graph loads in about 45 seconds.
- Native `algo.pageRank` and `algo.SPpaths`, no separate graph-analytics stack.
- Full-text indexes for search inside the same database.
- It's small enough to fit the 100 MB free cloud tier after compaction. There's also an embedded mode, so the app boots with FalkorDB inside the container and loads a 6 MB dump.

## What's next

- High Court judgments, which are in the same open dataset.
- Vector embeddings of holdings for doctrinal search.
- A "brief checker" that takes a draft and flags every cited authority with negative treatment.

*Code: [github.com/AnonyPotamus/precedent-graph](https://github.com/AnonyPotamus/precedent-graph) · Live demo: [precedents.cyruslogic.net](https://precedents.cyruslogic.net) · MCP: `https://precedents.cyruslogic.net/mcp/`*
