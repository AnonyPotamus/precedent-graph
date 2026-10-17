"""Graph tools over the FalkorDB precedent graph.

Every tool returns plain JSON-able dicts. Tools that produce explainable evidence also return
`graph`: {nodes:[...], edges:[...]} so the UI can draw exactly the paths the agent reasoned over.
"""
import os
import re
from functools import lru_cache

from falkordb import FalkorDB

NEG = ["NEGATIVE", "PARTIAL", "CAUTION"]
CASE_FIELDS = "c.key AS key, c.short AS name, c.scr AS scr, c.insc AS insc, c.date AS date, c.year AS year, c.bench_size AS bench, c.stub AS stub, c.reported AS reported, c.pagerank AS pagerank"


@lru_cache(maxsize=1)
def graph():
    db = FalkorDB(
        host=os.getenv("FALKORDB_HOST", "localhost"),
        port=int(os.getenv("FALKORDB_PORT", 6379)),
        username=os.getenv("FALKORDB_USERNAME") or None,
        password=os.getenv("FALKORDB_PASSWORD") or None,
    )
    return db.select_graph(os.getenv("FALKORDB_GRAPH", "precedents"))


def ro(query, params=None):
    res = graph().ro_query(query, params or {})
    cols = [h[1] if isinstance(h, (list, tuple)) else h for h in res.header]
    return [dict(zip(cols, row)) for row in res.result_set]


# ---------------------------------------------------------------- helpers

class Evidence:
    """Accumulates nodes/edges for the UI graph."""

    def __init__(self):
        self.nodes, self.edges = {}, {}

    def node(self, c, role=None):
        if not c or not c.get("key"):
            return
        n = self.nodes.setdefault(c["key"], {"id": c["key"], "label": c.get("name") or c["key"],
                                              "scr": c.get("scr"), "year": c.get("year"),
                                              "bench": c.get("bench"), "type": "Case"})
        rank = ["target", "negative", "inferred", "weak-foundation", "stale", "path", "positive", None]
        if rank.index(role if role in rank else None) < rank.index(n.get("role") if n.get("role") in rank else None):
            n["role"] = role

    def edge(self, s, t, treatment, polarity, **kw):
        self.edges[(s, t, treatment)] = {"from": s, "to": t, "label": treatment, "polarity": polarity, **kw}

    def dump(self):
        return {"nodes": list(self.nodes.values()), "edges": list(self.edges.values())}


def _fulltext_query(text):
    words = re.findall(r"[A-Za-z0-9]{2,}", text or "")
    stop = {"the", "of", "and", "in", "to", "vs", "v", "for", "on", "by", "is", "was", "case", "judgment", "state"}
    words = [w for w in words if w.lower() not in stop][:8]
    return " ".join(words)


def _case(key):
    rows = ro(f"MATCH (c:Case {{key:$k}}) RETURN {CASE_FIELDS}, c.title AS title, c.author AS author, c.disposal AS disposal, c.held AS held, c.community AS community", {"k": key})
    return rows[0] if rows else None


def resolve_case(ref):
    """Accept a graph key, an SCR/INSC citation, or a case name; return the best-matching key."""
    ref = (ref or "").strip()
    if not ref:
        return None
    if re.match(r"^(SCR|SCC|AIR|INSC):", ref):
        return ref if _case(ref) else None
    from pipeline.extract import cite_key
    k = cite_key(ref)
    if k:
        if _case(k):
            return k
        if k.startswith("INSC"):
            y, n = k.split(":")[1:]
            r = ro("MATCH (c:Case) WHERE c.insc = $i RETURN c.key AS key", {"i": f"{y} INSC {n}"})
            if r:
                return r[0]["key"]
        if k.startswith("SCR"):
            _, y, s, v, p = k.split(":")
            r = ro("MATCH (c:Case) WHERE c.key STARTS WITH $pre AND c.key ENDS WITH $suf RETURN c.key AS key", {"pre": f"SCR:{y}:{s}:", "suf": f":{p}"})
            if len(r) == 1:
                return r[0]["key"]
    hits = search_cases(ref, limit=1).get("results", [])
    return hits[0]["key"] if hits else None


# ---------------------------------------------------------------- tools

def search_cases(query, limit=8, year_from=None, year_to=None):
    """Full-text search over case names, citations, keywords and editorial headnotes.
    Candidates come from the FalkorDB full-text index; they are re-ranked so that a match on the
    case name beats a passing mention in some other judgment's headnote."""
    ft = _fulltext_query(query)
    if not ft:
        return {"results": []}
    rows = ro(f"""CALL db.idx.fulltext.queryNodes('Case', $q) YIELD node AS c, score
                 WHERE ($yf IS NULL OR c.year >= $yf) AND ($yt IS NULL OR c.year <= $yt)
                 WITH c, score ORDER BY score DESC LIMIT 200
                 OPTIONAL MATCH (c)<-[n:CITES]-() WHERE n.polarity IN $neg
                 WITH c, score, count(n) AS negative
                 RETURN {CASE_FIELDS}, score, negative""",
             {"q": ft, "yf": year_from, "yt": year_to, "neg": NEG})
    if not rows:  # all-words query found nothing: retry with any word
        rows = ro(f"""CALL db.idx.fulltext.queryNodes('Case', $q) YIELD node AS c, score
                     WITH c, score ORDER BY score DESC LIMIT 200
                     OPTIONAL MATCH (c)<-[n:CITES]-() WHERE n.polarity IN $neg
                     WITH c, score, count(n) AS negative
                     RETURN {CASE_FIELDS}, score, negative""", {"q": "|".join(ft.split()), "neg": NEG})
    words = [w.lower() for w in ft.split()]

    def rank(r):
        name = (r.get("name") or "").lower() + " " + (r.get("scr") or "").lower()
        name_frac = sum(1 for w in words if w.lower() in name) / max(1, len(words))
        base = min(r["score"] or 0, 20) / 20
        auth = 1 + min((r.get("pagerank") or 0) * 2000, 3)
        return (name_frac * 10 + base) * auth * (0.4 if r.get("stub") else 1) * (1.3 if r.get("reported") else 1)

    rows.sort(key=rank, reverse=True)
    from pipeline.extract import cite_key
    if cite_key(query or ""):  # a pasted citation goes straight to that case
        k = resolve_case(query)
        if k:
            exact = ro(f"MATCH (c:Case {{key:$k}}) OPTIONAL MATCH (c)<-[n:CITES]-() WHERE n.polarity IN $neg "
                       f"WITH c, count(n) AS negative RETURN {CASE_FIELDS}, 100.0 AS score, negative", {"k": k, "neg": NEG})
            rows = exact + [r for r in rows if r["key"] != k]
    return {"query": ft, "results": rows[:int(limit)]}


def get_case(case):
    """Details of one case: bench, statutes, headnote holding, how it treats others and how others treat it."""
    key = resolve_case(case)
    if not key:
        return {"error": f"No case found for '{case}'"}
    c = _case(key)
    bench = [r["j"] for r in ro("MATCH (j:Judge)-[:ON_BENCH]->(:Case {key:$k}) RETURN j.name AS j", {"k": key})]
    provisions = [r["p"] for r in ro("MATCH (:Case {key:$k})-[:INTERPRETS]->(p:Provision) RETURN p.id AS p LIMIT 15", {"k": key})]
    cites = ro("""MATCH (:Case {key:$k})-[e:CITES]->(c:Case)
                  RETURN c.key AS key, c.short AS name, c.scr AS scr, e.treatment AS treatment, e.polarity AS polarity
                  ORDER BY CASE e.polarity WHEN 'NEGATIVE' THEN 0 WHEN 'PARTIAL' THEN 1 WHEN 'CAUTION' THEN 2 WHEN 'POSITIVE' THEN 3 ELSE 4 END LIMIT 25""", {"k": key})
    cited_by = ro("""MATCH (s:Case)-[e:CITES]->(:Case {key:$k})
                     RETURN e.polarity AS polarity, count(*) AS n""", {"k": key})
    c["held"] = (c.get("held") or "")[:1500]
    return {**c, "bench_judges": bench, "provisions": provisions, "treats": cites,
            "treated_by_counts": {r["polarity"]: r["n"] for r in cited_by}}


def check_good_law(case):
    """Is this precedent still good law? Multi-hop: direct negative treatment, reliance on overruled
    authority (1-2 hops upstream), and later cases still relying on it (downstream)."""
    key = resolve_case(case)
    if not key:
        return {"error": f"No case found for '{case}'"}
    ev = Evidence()
    c = _case(key)
    ev.node(c, role="target")

    direct = ro(f"""MATCH (c:Case)-[e:CITES]->(t:Case {{key:$k}}) WHERE e.polarity IN $neg
                   RETURN {CASE_FIELDS}, e.treatment AS treatment, e.polarity AS polarity, e.paras AS paras, e.context AS context
                   ORDER BY c.date""", {"k": key, "neg": NEG})
    tb = c.get("bench") or 0
    for d in direct:
        ev.node(d, role="negative")
        d["larger_or_equal_bench"] = (d["bench"] or 0) >= tb if tb else None
        ev.edge(d["key"], key, d["treatment"], d["polarity"], paras=d["paras"])

    # Second hop: was the overruling judgment itself overruled later (by an equal or larger bench)?
    for d in direct:
        d["itself_overruled_by"] = None
        if d["polarity"] not in ("NEGATIVE", "PARTIAL"):
            continue
        up = ro("""MATCH (z:Case)-[e:CITES]->(o:Case {key:$o}) WHERE e.polarity IN ['NEGATIVE','PARTIAL']
                   AND z.key <> $k AND (o.bench_size = 0 OR z.bench_size >= o.bench_size)
                   RETURN z.key AS key, z.short AS name, z.scr AS scr, z.bench_size AS bench, z.date AS date, e.treatment AS treatment
                   ORDER BY z.bench_size DESC LIMIT 1""", {"o": d["key"], "k": key})
        if up and (not d["date"] or not up[0]["date"] or up[0]["date"] >= d["date"]):
            d["itself_overruled_by"] = up[0]
            ev.node(up[0], role="negative")
            ev.edge(up[0]["key"], d["key"], up[0]["treatment"], "NEGATIVE")
    effective = [d for d in direct if not d["itself_overruled_by"]]

    neg_dates = [d["date"] for d in direct if d["polarity"] in ("NEGATIVE", "PARTIAL") and d["date"]]
    first_neg = min(neg_dates) if neg_dates else None

    # Who most likely did the overruling: earliest negative citing case with bench >= target bench
    likely = None
    able = [d for d in effective if d["polarity"] in ("NEGATIVE", "PARTIAL") and (not tb or (d["bench"] or 0) >= tb)]
    if able:
        # most authoritative: a full overruling beats a partial one, then the largest bench, then the earliest
        d = sorted(able, key=lambda d: (d["polarity"] != "NEGATIVE", -(d["bench"] or 0), d["date"] or "9999"))[0]
        likely = {"key": d["key"], "name": d["name"], "scr": d["scr"], "bench": d["bench"], "date": d["date"],
                  "treatment": d["treatment"]}

    # If no noting judgment had the bench strength to overrule, infer the overruling authority from the graph:
    # a later, larger-bench judgment that the noting judgments themselves cite, ideally one that cites the target.
    inferred = None
    if not likely and any(d["polarity"] in ("NEGATIVE", "PARTIAL") for d in direct):
        cand = ro("""MATCH (n:Case)-[e:CITES]->(t:Case {key:$k}) WHERE e.polarity IN ['NEGATIVE','PARTIAL']
                     MATCH (n)-[:CITES]->(o:Case) WHERE o.key <> t.key AND o.year >= t.year AND o.bench_size >= $b
                     WITH o, t, count(DISTINCT n) AS support
                     OPTIONAL MATCH (o)-[x:CITES]->(t)
                     RETURN o.key AS key, o.short AS name, o.scr AS scr, o.bench_size AS bench, o.date AS date, o.year AS year,
                            support, count(x) > 0 AS cites_target, head(collect(x.treatment)) AS own_label
                     ORDER BY cites_target DESC, support DESC, o.bench_size DESC LIMIT 3""", {"k": key, "b": max(tb, 3)})
        noting = sum(1 for d in direct if d["polarity"] in ("NEGATIVE", "PARTIAL"))
        if cand and cand[0]["support"] >= max(2, noting // 2):
            inferred = {**cand[0], "noting_judgments": noting,
                        "basis": f"cited by {cand[0]['support']} of the {noting} judgments that note the overruling"
                                 + (", and it cites the target itself" + (f" (its own table says '{cand[0]['own_label']}')" if cand[0]["own_label"] else "") if cand[0]["cites_target"] else "")}
            ev.node(inferred, role="inferred")
            ev.edge(inferred["key"], key, "overruled (inferred)", "NEGATIVE", inferred=True)
            for d in direct:
                if d["polarity"] in ("NEGATIVE", "PARTIAL"):
                    ev.edge(d["key"], inferred["key"], "cites", "NEUTRAL")

    positive = ro("""MATCH (c:Case)-[e:CITES]->(:Case {key:$k}) WHERE e.polarity = 'POSITIVE'
                     RETURN c.key AS key, c.short AS name, c.scr AS scr, c.date AS date, c.year AS year, c.bench_size AS bench, e.treatment AS treatment
                     ORDER BY c.date DESC""", {"k": key})
    stale_reliance = [p for p in positive if first_neg and p["date"] and p["date"] > first_neg]
    for p in positive[:6] + stale_reliance:
        ev.node(p, role="stale" if p in stale_reliance else "positive")
        ev.edge(p["key"], key, p["treatment"], "POSITIVE")

    # Upstream foundations: the target relies on X (1-2 hops) and X has negative treatment
    foundations = ro("""MATCH (t:Case {key:$k})-[r1:CITES]->(y:Case)
                        WHERE r1.polarity = 'POSITIVE'
                        MATCH (z:Case)-[n:CITES]->(y) WHERE n.polarity IN ['NEGATIVE','PARTIAL'] AND z.key <> $k
                        RETURN 1 AS hops, y.key AS via_key, y.short AS via, y.scr AS via_scr, null AS mid_key, null AS mid,
                               r1.treatment AS reliance, z.key AS by_key, z.short AS by, z.scr AS by_scr, z.date AS by_date,
                               z.bench_size AS by_bench, n.treatment AS treatment
                        UNION
                        MATCH (t:Case {key:$k})-[r1:CITES]->(m:Case)-[r2:CITES]->(y:Case)
                        WHERE r1.polarity = 'POSITIVE' AND r2.polarity = 'POSITIVE' AND y.key <> $k
                        MATCH (z:Case)-[n:CITES]->(y) WHERE n.polarity IN ['NEGATIVE','PARTIAL'] AND z.key <> $k AND z.key <> m.key
                        RETURN 2 AS hops, y.key AS via_key, y.short AS via, y.scr AS via_scr, m.key AS mid_key, m.short AS mid,
                               r1.treatment AS reliance, z.key AS by_key, z.short AS by, z.scr AS by_scr, z.date AS by_date,
                               z.bench_size AS by_bench, n.treatment AS treatment""", {"k": key})
    foundations = foundations[:12]
    tdate = c.get("date") or ""
    for f in foundations:
        f["overruled_before_target"] = bool(tdate and f["by_date"] and f["by_date"] < tdate)
        src = key
        if f["mid_key"]:
            ev.node({"key": f["mid_key"], "name": f["mid"]}, role="path")
            ev.edge(key, f["mid_key"], f["reliance"], "POSITIVE")
            src = f["mid_key"]
        ev.node({"key": f["via_key"], "name": f["via"], "scr": f["via_scr"]}, role="weak-foundation")
        ev.edge(src, f["via_key"], "relied on" if f["mid_key"] else f["reliance"], "POSITIVE")
        ev.node({"key": f["by_key"], "name": f["by"], "scr": f["by_scr"], "bench": f["by_bench"]}, role="negative")
        ev.edge(f["by_key"], f["via_key"], f["treatment"], "NEGATIVE")

    pols = {d["polarity"] for d in effective}
    overturned = [d for d in direct if d["itself_overruled_by"]]
    if "NEGATIVE" in pols:
        verdict = "NOT GOOD LAW"
    elif "PARTIAL" in pols:
        verdict = "PARTIALLY OVERRULED / MODIFIED"
    elif "CAUTION" in pols:
        verdict = "CAUTION: DOUBTED OR NOT FOLLOWED"
    elif overturned:
        verdict = "CAUTION: OVERRULING JUDGMENT WAS ITSELF OVERRULED"
    elif foundations:
        verdict = "CAUTION: RELIES ON AUTHORITY LATER OVERRULED"
    else:
        verdict = "NO NEGATIVE TREATMENT FOUND"
    for d in direct:
        d["context"] = (d.get("context") or "")[:900]
    return {
        "case": {k: c.get(k) for k in ("key", "name", "scr", "insc", "date", "bench", "author", "disposal")},
        "verdict": verdict,
        "likely_overruled_by": likely,
        "target_bench_known": bool(tb),
        "inferred_overruling_authority": inferred,
        "negative_treatment": direct,
        "positive_treatment_count": len(positive),
        "recent_positive": positive[:5],
        "still_relied_on_after_negative_treatment": stale_reliance[:8],
        "weak_foundations": foundations,
        "overruling_reversed_note": ("Every negative treatment here comes from a judgment that a later, equal or larger bench "
                                     "itself overruled; the precedent may be restored in whole or part - read the later judgment."
                                     if overturned and not any(d["polarity"] == "NEGATIVE" for d in effective) else None),
        "coverage_note": "Treatments come from the editorial 'Case Law Reference' tables of SCR-reported Supreme Court judgments in the loaded corpus; a label such as 'overruled' records the precedent's status as noted by the citing judgment, which is not necessarily the judgment that overruled it (see larger_or_equal_bench).",
        "graph": ev.dump(),
    }


def citing_cases(case, polarity=None, limit=15):
    """Later judgments that cite this case, optionally filtered by polarity (POSITIVE, NEGATIVE, PARTIAL, CAUTION, DISTINGUISHED, NEUTRAL)."""
    key = resolve_case(case)
    if not key:
        return {"error": f"No case found for '{case}'"}
    rows = ro(f"""MATCH (c:Case)-[e:CITES]->(:Case {{key:$k}}) WHERE $p IS NULL OR e.polarity = $p
                 RETURN {CASE_FIELDS}, e.treatment AS treatment, e.polarity AS polarity, e.paras AS paras, left(e.context, 600) AS context
                 ORDER BY c.date DESC LIMIT $lim""", {"k": key, "p": polarity, "lim": int(limit)})
    ev = Evidence()
    ev.node(_case(key), role="target")
    for r in rows:
        ev.node(r)
        ev.edge(r["key"], key, r["treatment"], r["polarity"])
    return {"case": key, "citing": rows, "graph": ev.dump()}


def connection_path(case_a, case_b, max_len=6):
    """Shortest citation chain linking two cases (either direction), with the treatment on each hop."""
    a, b = resolve_case(case_a), resolve_case(case_b)
    if not a or not b:
        return {"error": "Could not resolve both cases", "a": a, "b": b}
    rows = ro("""MATCH (a:Case {key:$a}), (b:Case {key:$b})
                 CALL algo.SPpaths({sourceNode:a, targetNode:b, relTypes:['CITES'], relDirection:'both', maxLen:$m, pathCount:3})
                 YIELD path
                 RETURN [n IN nodes(path) | {key:n.key, name:n.short, scr:n.scr, year:n.year}] AS nodes,
                        [e IN relationships(path) | {from:startNode(e).key, to:endNode(e).key, treatment:e.treatment, polarity:e.polarity}] AS hops""",
              {"a": a, "b": b, "m": int(max_len)})
    ev = Evidence()
    for r in rows:
        for n in r["nodes"]:
            ev.node(n, role="target" if n["key"] in (a, b) else "path")
        for h in r["hops"]:
            ev.edge(h["from"], h["to"], h["treatment"], h["polarity"])
    return {"from": a, "to": b, "paths": rows, "graph": ev.dump()}


def cases_on_provision(provision, limit=12):
    """Most authoritative judgments interpreting a statutory provision, e.g. 'Code of Criminal Procedure s. 482'
    or 'Constitution Art. 21'. Ranked by citation PageRank, with negative-treatment counts."""
    ft = _fulltext_query(provision)
    rows = ro("""CALL db.idx.fulltext.queryNodes('Provision', $q) YIELD node AS p, score
                 WITH p, score ORDER BY score DESC LIMIT 5
                 MATCH (c:Case)-[:INTERPRETS]->(p)
                 OPTIONAL MATCH (c)<-[n:CITES]-() WHERE n.polarity IN $neg
                 WITH p, c, count(n) AS negative
                 RETURN p.id AS provision, c.key AS key, c.short AS name, c.scr AS scr, c.year AS year,
                        c.bench_size AS bench, c.pagerank AS pagerank, negative
                 ORDER BY c.pagerank DESC LIMIT $lim""", {"q": ft, "neg": NEG, "lim": int(limit)})
    return {"query": ft, "results": rows}


def landmark_cases(act=None, limit=10):
    """Most-cited (PageRank) precedents overall or under a given Act."""
    if act:
        rows = ro("""CALL db.idx.fulltext.queryNodes('Provision', $q) YIELD node AS p
                     MATCH (p)-[:OF]->(a:Act) WITH DISTINCT a LIMIT 3
                     MATCH (c:Case)-[:UNDER]->(a)
                     OPTIONAL MATCH (c)<-[n:CITES]-() WHERE n.polarity IN $neg
                     WITH a, c, count(n) AS negative
                     RETURN a.name AS act, c.key AS key, c.short AS name, c.scr AS scr, c.year AS year, c.pagerank AS pagerank, negative
                     ORDER BY c.pagerank DESC LIMIT $lim""", {"q": _fulltext_query(act), "neg": NEG, "lim": int(limit)})
    else:
        rows = ro("""MATCH (c:Case) OPTIONAL MATCH (c)<-[n:CITES]-() WHERE n.polarity IN $neg
                     WITH c, count(n) AS negative
                     RETURN c.key AS key, c.short AS name, c.scr AS scr, c.year AS year, c.pagerank AS pagerank, negative
                     ORDER BY c.pagerank DESC LIMIT $lim""", {"neg": NEG, "lim": int(limit)})
    return {"results": rows}


def judge_profile(name):
    """A judge's footprint: benches, authored judgments, precedents their benches overruled, most frequent Acts."""
    j = ro("""MATCH (j:Judge) WHERE toLower(j.name) CONTAINS toLower($n) OR toLower(j.id) CONTAINS toLower($n)
                 OR any(a IN j.aliases WHERE toLower(a) CONTAINS toLower($n))
              OPTIONAL MATCH (j)-[:ON_BENCH]->(c) WITH j, count(c) AS n
              RETURN j.id AS id, j.name AS name ORDER BY n DESC LIMIT 5""", {"n": name.strip()})
    if not j:
        return {"error": f"No judge matching '{name}'"}
    jid, nm = j[0]["id"], j[0]["name"]
    stats = ro("""MATCH (j:Judge {id:$n})
                  OPTIONAL MATCH (j)-[:ON_BENCH]->(b:Case) WITH j, count(DISTINCT b) AS benches
                  OPTIONAL MATCH (j)-[:AUTHORED]->(a:Case) RETURN benches, count(DISTINCT a) AS authored""", {"n": jid})[0]
    overruled = ro("""MATCH (:Judge {id:$n})-[:ON_BENCH]->(c:Case)-[e:CITES]->(t:Case)
                      WHERE e.polarity IN ['NEGATIVE','PARTIAL'] AND c.bench_size >= coalesce(t.bench_size, 0)
                      RETURN c.short AS in_case, c.scr AS in_scr, c.bench_size AS bench, e.treatment AS treatment, t.short AS target, t.scr AS target_scr
                      LIMIT 15""", {"n": jid})
    acts = ro("""MATCH (:Judge {id:$n})-[:ON_BENCH]->(:Case)-[:UNDER]->(a:Act)
                 RETURN a.name AS act, count(*) AS n ORDER BY n DESC LIMIT 6""", {"n": jid})
    peers = ro("""MATCH (:Judge {id:$n})-[:ON_BENCH]->(c:Case)<-[:ON_BENCH]-(p:Judge)
                  WHERE p.id <> $n
                  RETURN p.name AS judge, count(c) AS shared ORDER BY shared DESC LIMIT 5""", {"n": jid})
    return {"judge": nm, "other_matches": [x["name"] for x in j[1:]], **stats,
            "negative_treatments_by_benches": overruled, "top_acts": acts, "frequent_co_judges": peers}


SCHEMA = """(:Case {key, scr, insc, title, short, petitioner, respondent, date 'YYYY-MM-DD', year, disposal, author,
        bench_size, reported, stub, headnote, held, pagerank, community})
(:Judge {id, name, aliases})-[:ON_BENCH]->(:Case); (:Judge)-[:AUTHORED]->(:Case)
(:Case)-[:UNDER]->(:Act {name}); (:Case)-[:INTERPRETS]->(:Provision {id 'Act | s. 482', act, ref})-[:OF]->(:Act)
(:Case)-[:CITES {treatment, polarity in POSITIVE|NEGATIVE|PARTIAL|CAUTION|DISTINGUISHED|NEUTRAL, paras, context}]->(:Case)
Full-text indexes: db.idx.fulltext.queryNodes('Case', q), db.idx.fulltext.queryNodes('Provision', q)
Algorithms: algo.pageRank, algo.SPpaths, algo.BFS, algo.WCC, algo.labelPropagation, algo.betweenness"""

WRITE_RE = re.compile(r"\b(CREATE|MERGE|SET|DELETE|REMOVE|DROP|DETACH|LOAD\s+CSV)\b|createNodeIndex", re.I)


KEY_RE = re.compile(r"^(SCR|SCC|AIR|INSC):")


def _graph_from_rows(rows, max_nodes=40):
    """Best-effort evidence graph for free-form query results: any case keys (or Case nodes) in a row become
    nodes; a row with exactly two cases becomes an edge, labelled by a treatment/polarity column if present."""
    ev, seen = Evidence(), []
    pairs = []
    for r in rows:
        keys = []
        for v in r.values():
            if isinstance(v, str) and KEY_RE.match(v):
                keys.append(v)
            elif hasattr(v, "properties") and isinstance(getattr(v, "properties", None), dict) and KEY_RE.match(str(v.properties.get("key", ""))):
                keys.append(v.properties["key"])
        for k in keys:
            if k not in seen:
                seen.append(k)
        if len(keys) == 2:
            lab = next((str(v) for c, v in r.items() if "treat" in c.lower() and isinstance(v, str)), "cites")
            pol = next((str(v) for c, v in r.items() if "polar" in c.lower() and isinstance(v, str)),
                       POLARITY_OF.get(lab.lower(), "NEUTRAL"))
            pairs.append((keys[0], keys[1], lab, pol))
    seen = seen[:max_nodes]
    if not seen:
        return None
    for c in ro(f"MATCH (c:Case) WHERE c.key IN $ks RETURN {CASE_FIELDS}", {"ks": seen}):
        ev.node(c)
    for a, b, lab, pol in pairs:
        if a in ev.nodes and b in ev.nodes:
            ev.edge(a, b, lab, pol)
    return ev.dump()


POLARITY_OF = {"overruled": "NEGATIVE", "per incuriam": "NEGATIVE", "held per incuriam": "NEGATIVE", "not good law": "NEGATIVE",
               "partly overruled": "PARTIAL", "doubted": "CAUTION", "not followed": "CAUTION", "disapproved": "CAUTION",
               "relied on": "POSITIVE", "followed": "POSITIVE", "approved": "POSITIVE", "distinguished": "DISTINGUISHED"}


def run_cypher(query):
    """Run a READ-ONLY Cypher query for questions the other tools don't cover. Max 50 rows.
    Return case keys (c.key) in the results so the UI can draw them."""
    if WRITE_RE.search(query):
        return {"error": "Only read-only queries are allowed."}
    try:
        rows = ro(query)
    except Exception as e:
        return {"error": str(e), "schema": SCHEMA}
    out = []
    for r in rows[:50]:
        out.append({k: (str(v)[:400] if not isinstance(v, (int, float, str, type(None), list, dict, bool)) else v) for k, v in r.items()})
    res = {"rows": out, "truncated": len(rows) > 50}
    try:
        g = _graph_from_rows(rows[:50])
        if g:
            res["graph"] = g
    except Exception:
        pass
    return res


def treatment_search(treatment, min_bench=None, year_from=None, limit=25):
    """Judgments that received a given treatment, e.g. 'per incuriam', 'overruled', 'doubted', 'not good law',
    'partly overruled', 'distinguished'. Returns who applied it, both bench sizes and the paragraph."""
    t = (treatment or "").lower().strip()
    rows = ro(f"""MATCH (s:Case)-[e:CITES]->(c:Case)
                 WHERE toLower(e.treatment) CONTAINS $t
                   AND ($mb IS NULL OR s.bench_size >= $mb) AND ($yf IS NULL OR s.year >= $yf)
                 RETURN {CASE_FIELDS}, s.key AS by_key, s.short AS by_name, s.scr AS by_scr, s.date AS by_date,
                        s.bench_size AS by_bench, e.treatment AS treatment, e.polarity AS polarity, left(e.context, 400) AS context
                 ORDER BY s.date DESC LIMIT $lim""",
              {"t": t, "mb": min_bench, "yf": year_from, "lim": int(limit)})
    ev = Evidence()
    for r in rows:
        ev.node(r, role="target")
        ev.node({"key": r["by_key"], "name": r["by_name"], "scr": r["by_scr"], "bench": r["by_bench"]}, role="negative"
                if r["polarity"] in NEG else None)
        ev.edge(r["by_key"], r["key"], r["treatment"], r["polarity"])
    total = ro("MATCH ()-[e:CITES]->() WHERE toLower(e.treatment) CONTAINS $t RETURN count(e) AS n", {"t": t})[0]["n"]
    return {"treatment": t, "total_in_corpus": total, "results": rows, "graph": ev.dump()}


def stats():
    r = ro("""MATCH (c:Case) WITH count(c) AS cases, sum(CASE WHEN c.reported THEN 1 ELSE 0 END) AS reported,
              min(CASE WHEN c.reported THEN c.year END) AS y0, max(CASE WHEN c.reported THEN c.year END) AS y1
              MATCH ()-[e:CITES]->() WITH cases, reported, y0, y1, count(e) AS cites
              MATCH (j:Judge) WITH cases, reported, y0, y1, cites, count(j) AS judges
              MATCH ()-[n:CITES]->() WHERE n.polarity IN ['NEGATIVE','PARTIAL']
              RETURN cases, reported, y0, y1, cites, judges, count(n) AS overrulings""")
    return r[0] if r else {}


TOOLS = {
    "search_cases": search_cases,
    "get_case": get_case,
    "check_good_law": check_good_law,
    "citing_cases": citing_cases,
    "connection_path": connection_path,
    "cases_on_provision": cases_on_provision,
    "landmark_cases": landmark_cases,
    "judge_profile": judge_profile,
    "run_cypher": run_cypher,
    "treatment_search": treatment_search,
}
