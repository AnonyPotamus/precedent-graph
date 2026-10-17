# Cypher queries and graph algorithms

These are the FalkorDB queries the product depends on, copied from `app/graph_tools.py` (the agent's tools) and `pipeline/build_graph.py` (the loader). Parameters are written `$k`, `$neg` and so on. `$neg` is always `['NEGATIVE','PARTIAL','CAUTION']`.

All agent-facing reads go through `GRAPH.RO_QUERY` (`graph.ro_query` in the Python client). This means the agent, its `run_cypher` tool and the MCP server cannot write to the graph.

## Data model

```
(:Case {key, scr, insc, short, date, year, bench_size, reported, stub, held, keywords, pagerank, community})
(:Judge {id, name, aliases})-[:ON_BENCH]->(:Case)
(:Judge)-[:AUTHORED]->(:Case)
(:Case)-[:UNDER]->(:Act {name})
(:Case)-[:INTERPRETS]->(:Provision {id, act, ref})-[:OF]->(:Act)
(:Case)-[:CITES {treatment, polarity, paras, context}]->(:Case)
(:Meta {dump_version, loaded_at})
```

**`CITES.treatment`** is the court editors' own label from the Supreme Court Reports (*relied on*, *distinguished*, *overruled*, *per incuriam*, …). **`polarity`** groups those labels into six values: POSITIVE, NEGATIVE, PARTIAL, CAUTION, DISTINGUISHED and NEUTRAL. **`context`** holds the paragraph where the citing court discussed the case.

---

## 1. Loading (batched `UNWIND`)

The whole graph (29k cases, 63k citations) loads in about 45 seconds.

```cypher
UNWIND $rows AS r
MERGE (c:Case {key: r.key})
SET c.scr = r.scr, c.insc = r.insc, c.short = r.short, c.date = r.date, c.year = r.year,
    c.bench_size = r.bench_size, c.reported = r.reported, c.stub = false, c.held = r.held, c.keywords = r.keywords
```

```cypher
UNWIND $rows AS r
MATCH (s:Case {key: r.s}), (t:Case {key: r.t})
CREATE (s)-[:CITES {treatment: r.tr, polarity: r.pol, paras: r.paras, context: r.ctx}]->(t)
```

```cypher
UNWIND $rows AS r
MATCH (c:Case {key: r.k})
MERGE (a:Act {name: r.a})
MERGE (p:Provision {id: r.id}) SET p.act = r.a, p.ref = r.ref
MERGE (p)-[:OF]->(a)
MERGE (c)-[:INTERPRETS]->(p)
```

## 2. Indexes

```cypher
CREATE INDEX FOR (c:Case) ON (c.key)
CREATE INDEX FOR (c:Case) ON (c.year)
CREATE INDEX FOR (j:Judge) ON (j.id)
CREATE INDEX FOR (p:Provision) ON (p.id)
CALL db.idx.fulltext.createNodeIndex('Case', 'short', 'title', 'keywords', 'held', 'headnote', 'scr', 'insc')
CALL db.idx.fulltext.createNodeIndex('Provision', 'id')
```

## 3. Graph algorithms, computed at load time

**Authority score.** PageRank over the citation graph identifies landmark judgments and ranks search results and provision lookups.

```cypher
CALL algo.pageRank('Case', 'CITES') YIELD node, score
SET node.pagerank = score
```

**Doctrine communities.** Label propagation groups cases into clusters of related doctrine.

```cypher
CALL algo.labelPropagation() YIELD node, communityId
WITH node, communityId WHERE node:Case
SET node.community = communityId
```

---

## 4. The good-law engine (`check_good_law`)

This is the core of the product: five graph patterns, combined into one verdict with an evidence graph.

### 4a. Direct negative treatment

```cypher
MATCH (c:Case)-[e:CITES]->(t:Case {key: $k})
WHERE e.polarity IN $neg
RETURN c.key, c.short, c.scr, c.date, c.bench_size, e.treatment, e.polarity, e.paras, e.context
ORDER BY c.date
```

The code then compares bench strength: `c.bench_size >= t.bench_size`. An "overruled" label from a smaller bench records the precedent's status; it doesn't mean that bench did the overruling.

### 4b. Second hop: was the overruling judgment itself overruled?

```cypher
MATCH (z:Case)-[e:CITES]->(o:Case {key: $overruling_case})
WHERE e.polarity IN ['NEGATIVE','PARTIAL']
  AND z.key <> $k
  AND (o.bench_size = 0 OR z.bench_size >= o.bench_size)
RETURN z.key, z.short, z.scr, z.bench_size, z.date, e.treatment
ORDER BY z.bench_size DESC LIMIT 1
```

Example: *Garware Wall Ropes* was overruled by the 5-judge *N.N. Global*, which the 7-judge *In Re: Interplay* later overruled. The verdict becomes "Caution: overruling judgment was itself overruled" instead of "not good law".

### 4c. Inferring the overruling judgment when the labels don't say

Sometimes none of the judgments that mark a case "overruled" had the bench strength to do it. In that case, find the later, larger-bench judgment that those noting judgments all cite, preferring one that also cites the target:

```cypher
MATCH (n:Case)-[e:CITES]->(t:Case {key: $k})
WHERE e.polarity IN ['NEGATIVE','PARTIAL']
MATCH (n)-[:CITES]->(o:Case)
WHERE o.key <> t.key AND o.year >= t.year AND o.bench_size >= $bench
WITH o, t, count(DISTINCT n) AS support
OPTIONAL MATCH (o)-[x:CITES]->(t)
RETURN o.key, o.short, o.scr, o.bench_size, support,
       count(x) > 0 AS cites_target, head(collect(x.treatment)) AS own_label
ORDER BY cites_target DESC, support DESC, o.bench_size DESC LIMIT 3
```

Example: *Pune Municipal Corporation v. Harakchand* (3 judges) is marked overruled by six 2-judge benches. All six cite *Indore Development Authority* (5 judges, 2020), which also cites *Pune Municipal*, so Indore is inferred as the overruling judgment. Indore's own editorial label for *Pune Municipal* says only "referred to".

### 4d. Later judgments still relying on it

```cypher
MATCH (c:Case)-[e:CITES]->(:Case {key: $k})
WHERE e.polarity = 'POSITIVE'
RETURN c.key, c.short, c.scr, c.date, c.bench_size, e.treatment
ORDER BY c.date DESC
```

The code flags positive citations dated after the first negative treatment as *still relied on after overruling*.

### 4e. Upstream foundations: does it rest on overruled authority? (1–2 hops)

```cypher
MATCH (t:Case {key: $k})-[r1:CITES]->(y:Case)
WHERE r1.polarity = 'POSITIVE'
MATCH (z:Case)-[n:CITES]->(y)
WHERE n.polarity IN ['NEGATIVE','PARTIAL'] AND z.key <> $k
RETURN 1 AS hops, y.key AS via_key, y.short AS via, z.key AS by_key, z.short AS by, z.date AS by_date, n.treatment
UNION
MATCH (t:Case {key: $k})-[r1:CITES]->(m:Case)-[r2:CITES]->(y:Case)
WHERE r1.polarity = 'POSITIVE' AND r2.polarity = 'POSITIVE' AND y.key <> $k
MATCH (z:Case)-[n:CITES]->(y)
WHERE n.polarity IN ['NEGATIVE','PARTIAL'] AND z.key <> $k AND z.key <> m.key
RETURN 2 AS hops, y.key AS via_key, y.short AS via, z.key AS by_key, z.short AS by, z.date AS by_date, n.treatment
```

If `by_date` is earlier than the target's own date, the target relied on authority that had *already* been overruled, which is a *per incuriam* risk.

---

## 5. Other agent tools

**Connection path between two lines of authority** (`connection_path`):

```cypher
MATCH (a:Case {key: $a}), (b:Case {key: $b})
CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: ['CITES'],
                   relDirection: 'both', maxLen: $m, pathCount: 3})
YIELD path
RETURN [n IN nodes(path) | {key: n.key, name: n.short, scr: n.scr}] AS nodes,
       [e IN relationships(path) | {from: startNode(e).key, to: endNode(e).key,
                                     treatment: e.treatment, polarity: e.polarity}] AS hops
```

**Case search** (`search_cases`): full-text candidates, then re-ranked in Python by name match and PageRank.

```cypher
CALL db.idx.fulltext.queryNodes('Case', $q) YIELD node AS c, score
WITH c, score ORDER BY score DESC LIMIT 200
OPTIONAL MATCH (c)<-[n:CITES]-() WHERE n.polarity IN $neg
WITH c, score, count(n) AS negative
RETURN c.key, c.short, c.scr, c.year, c.bench_size, c.pagerank, score, negative
```

**Leading judgments on a statutory provision** (`cases_on_provision`):

```cypher
CALL db.idx.fulltext.queryNodes('Provision', $q) YIELD node AS p, score
WITH p, score ORDER BY score DESC LIMIT 5
MATCH (c:Case)-[:INTERPRETS]->(p)
OPTIONAL MATCH (c)<-[n:CITES]-() WHERE n.polarity IN $neg
WITH p, c, count(n) AS negative
RETURN p.id, c.key, c.short, c.scr, c.year, c.bench_size, c.pagerank, negative
ORDER BY c.pagerank DESC LIMIT $lim
```

**Judge profile** (`judge_profile`): benches, precedents those benches overruled, and frequent co-judges.

```cypher
MATCH (:Judge {id: $j})-[:ON_BENCH]->(c:Case)-[e:CITES]->(t:Case)
WHERE e.polarity IN ['NEGATIVE','PARTIAL'] AND c.bench_size >= coalesce(t.bench_size, 0)
RETURN c.short, c.scr, c.bench_size, e.treatment, t.short, t.scr
```

```cypher
MATCH (:Judge {id: $j})-[:ON_BENCH]->(c:Case)<-[:ON_BENCH]-(p:Judge)
WHERE p.id <> $j   // FalkorDB can match the same edge twice in this pattern; exclude the judge themself
RETURN p.name, count(c) AS shared ORDER BY shared DESC LIMIT 5
```

**Read-only Cypher escape hatch** (`run_cypher`): the agent may write its own read query. Write keywords are rejected before execution, and the query runs with `GRAPH.RO_QUERY`.

## 6. Keeping the deployment in sync

When the app starts, it compares the version of the bundled graph dump with the version stored in the graph:

```cypher
OPTIONAL MATCH (m:Meta) RETURN m.dump_version
```

If they differ, it reloads from the dump. It then records the new version, and only reports itself ready once the full-text indexes answer queries:

```cypher
MERGE (m:Meta) SET m.dump_version = $v, m.loaded_at = timestamp()
CALL db.idx.fulltext.queryNodes('Case', 'Supreme') YIELD node RETURN count(node)
```
