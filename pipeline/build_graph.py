"""Load SCR metadata + extracted judgments into FalkorDB.

Graph model
-----------
(:Case {key, scr, insc, title, short, petitioner, respondent, date, year, disposal,
        author, bench_size, reported, stub, headnote, held, pagerank, community})
(:Judge {name})-[:AUTHORED]->(:Case)      (:Judge)-[:ON_BENCH]->(:Case)
(:Act {name})<-[:OF]-(:Provision {id, act, ref})<-[:INTERPRETS]-(:Case)
(:Case)-[:UNDER]->(:Act)
(:Case)-[:CITES {treatment, polarity, paras, context}]->(:Case)

Usage: python build_graph.py --meta meta_all.parquet --extracted dir_with_jsonl [--graph precedents]
Env:   FALKORDB_HOST, FALKORDB_PORT, FALKORDB_USERNAME, FALKORDB_PASSWORD
"""
import argparse
import gzip
import lzma
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

from falkordb import FalkorDB

sys.path.insert(0, os.path.dirname(__file__))
from extract import cite_key  # noqa: E402

POLARITY = {
    "overruled": "NEGATIVE", "held per incuriam": "NEGATIVE", "per incuriam": "NEGATIVE",
    "not good law": "NEGATIVE", "reversed": "NEGATIVE", "set aside": "NEGATIVE",
    "partly overruled": "PARTIAL", "modified": "PARTIAL",
    "doubted": "CAUTION", "disapproved": "CAUTION", "not followed": "CAUTION",
    "distinguished": "DISTINGUISHED", "held inapplicable": "DISTINGUISHED",
    "inapplicable": "DISTINGUISHED", "held not applicable": "DISTINGUISHED",
    "relied on": "POSITIVE", "relied upon": "POSITIVE", "followed": "POSITIVE", "approved": "POSITIVE",
    "affirmed": "POSITIVE", "upheld": "POSITIVE", "applied": "POSITIVE", "explained": "POSITIVE",
}


def polarity(t):
    return POLARITY.get(t, "NEUTRAL")


def short_title(title):
    t = re.sub(r"\s+", " ", title or "").strip()
    parts = re.split(r"\s+versus\s+|\s+v(?:s)?\.?\s+", t, maxsplit=1, flags=re.I)
    clean = lambda s: re.sub(r"\s*(&|AND)\s*(ORS?|ANR|OTHERS)\.?$", "", s.strip(), flags=re.I).title()
    if len(parts) == 2 and re.search(r"[A-Za-z]{2}", parts[1]):
        return f"{clean(parts[0])[:60]} v. {clean(parts[1])[:60]}"
    if len(parts) == 2:
        return clean(parts[0])[:110]
    return clean(t)[:120]


def parse_date(s):
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return None


def split_provisions(prov):
    """'ss. 482 and 397' -> ['s. 482', 's. 397'];  'Art. 226' -> ['Art. 226']"""
    m = re.match(r"(ss?\.|Arts?\.|rr?\.|O\.|Order|Section|Article|Sch(?:edule)?\.?)\s*(.*)", prov, re.I)
    if not m:
        return [prov]
    kind = m.group(1).lower()
    single = {"ss.": "s.", "s.": "s.", "arts.": "Art.", "art.": "Art.", "rr.": "r.", "r.": "r.",
              "section": "s.", "article": "Art."}.get(kind, m.group(1))
    nums = [n.strip() for n in re.split(r",|\band\b|&|r/w", m.group(2)) if n.strip()]
    out = []
    for n in nums:
        n = re.sub(r"\s+to\s+.*", "", n)
        base = re.match(r"[\dA-Z]+[A-Z]?", n)
        if base:
            out.append(f"{single} {base.group(0)}")
    return out or [prov]


PARA_RE = re.compile(r"^\s{0,12}(\d{1,3}(?:\.\d{1,2})?)\.?\s+(?=[A-Z“\"\(])")


def paragraphs(body):
    paras, cur, buf = {}, None, []
    for ln in body.splitlines():
        m = PARA_RE.match(ln)
        if m:
            if cur:
                paras.setdefault(cur, " ".join(buf))
            cur, buf = m.group(1), [ln[m.end():].strip()]
        elif cur:
            buf.append(ln.strip())
    if cur:
        paras.setdefault(cur, " ".join(buf))
    return {k: re.sub(r"\s+", " ", v)[:1500] for k, v in paras.items()}


def para_context(paras, para_field, limit=2):
    nums = re.findall(r"\d{1,3}(?:\.\d{1,2})?", para_field or "")
    out = []
    for n in nums[:limit]:
        if n in paras:
            out.append(f"[Para {n}] {paras[n]}")
    return "\n".join(out)[:2500]


ACT_ALIASES = {
    "Penal Code, 1860": "Indian Penal Code, 1860", "IPC, 1860": "Indian Penal Code, 1860",
    "Rehabilitation and Resettlement Act, 2013": "Right to Fair Compensation and Transparency in Land Acquisition, Rehabilitation and Resettlement Act, 2013",
    "Evidence Act, 1872": "Indian Evidence Act, 1872", "Contract Act, 1872": "Indian Contract Act, 1872",
    "Stamp Act, 1899": "Indian Stamp Act, 1899", "Constitution of India": "Constitution of India, 1950",
    "Criminal Procedure Code, 1973": "Code of Criminal Procedure, 1973", "Civil Procedure Code, 1908": "Code of Civil Procedure, 1908",
}


def normalise_act(a):
    a = re.sub(r"\s+", " ", a).strip().replace("Of ", "of ")
    a = re.sub(r"^The\s+", "", a)
    return ACT_ALIASES.get(a, a)


def judge_key(name):
    n = (name or "").upper().strip()
    n = re.sub(r"^((DR|HON'?BLE|JUSTICE|MR|MRS|MS|SHRI)\.?\s+)+", "", n)
    n = re.sub(r"[,\s]+(CJI|C\.J\.I\.|CJ|JJ|J)\.?\s*$", "", n)
    toks = re.findall(r"[A-Z]+", n)
    if not toks:
        return None
    return " ".join([t[0] for t in toks[:-1]] + [toks[-1]])


def name_context(paras, name, limit=2):
    """For citations without para numbers: find paragraphs that mention the cited party."""
    first = re.split(r"\s+(?:v\.|vs\.?|versus)\s+", name or "", flags=re.I)[0]
    words = [w for w in re.findall(r"[A-Za-z]{4,}", first) if w.lower() not in
             {"state", "union", "india", "others", "limited", "private", "through", "corporation", "company", "and"}][:2]
    if not words:
        return ""
    pat = re.compile(r"\b" + r"\W+".join(map(re.escape, words)), re.I)
    hits = [f"[Para {k}] {v}" for k, v in paras.items() if pat.search(v)]
    return "\n".join(hits[:limit])[:2500]


PARTY_STOP = {"m", "s", "ms", "ltd", "limited", "pvt", "private", "and", "ors", "anr", "others", "another", "the", "etc",
              "of", "co", "corporation", "corpn", "through", "its", "thr", "by", "lrs", "dead", "smt", "shri", "sri", "dr", "mr"}


def party_norm(s):
    toks = re.findall(r"[a-z0-9]+", (s or "").lower())
    return " ".join(t for t in toks if t not in PARTY_STOP and len(t) > 1)


class NameMatcher:
    """Resolve an SCC/AIR citation with a known case name to the SCR-reported judgment of the same parties."""

    def __init__(self, cases):
        from rapidfuzz import fuzz, process  # noqa: F401
        self.by_year = defaultdict(list)
        for k, c in cases.items():
            if c.get("year"):
                self.by_year[c["year"]].append((k, party_norm(c["petitioner"]), party_norm(c["respondent"])))

    def match(self, name, year):
        from rapidfuzz import fuzz
        parts = re.split(r"\s+(?:v\.?|vs\.?|versus)\s+", clean_stub_name(name) or "", maxsplit=1, flags=re.I)
        if len(parts) != 2 or not year:
            return None
        p, r = party_norm(parts[0]), party_norm(parts[1])
        if len(p) < 3 or len(r) < 3:
            return None
        scored = []
        for y in (year, year - 1, year - 2):
            for k, cp, cr in self.by_year.get(y, []):
                sp = fuzz.token_set_ratio(p, cp)
                if sp < 85:
                    continue
                sr = fuzz.token_set_ratio(r, cr)
                scored.append((min(sp, sr) - 6 * (year - y), k))
        scored.sort(reverse=True)
        if scored and scored[0][0] >= 86 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 4):
            return scored[0][1]
        return None


def clean_stub_name(n):
    n = re.sub(r"^.*?REFERENCES\s*", "", n, flags=re.I)
    n = re.sub(r"^.*?judgment of [^:]{0,60}?(?:CJI|JJ?\.)\s*", "", n, flags=re.I)
    n = re.sub(r"\s+", " ", n).strip(" .,:;-")
    return n if re.search(r"\s(v\.|vs\.?|versus)\s", n, re.I) and len(n) < 200 else ""


def open_dump(path):
    path = str(path)
    return lzma.open(path, "rt") if path.endswith(".xz") else gzip.open(path, "rt")


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta")
    ap.add_argument("--extracted")
    ap.add_argument("--save-dump", help="write the assembled graph to a .json.gz for loading elsewhere")
    ap.add_argument("--from-dump", help="load a previously saved .json.gz instead of re-parsing")
    ap.add_argument("--graph", default=os.getenv("FALKORDB_GRAPH", "precedents"))
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--compact", action="store_true", help="fit FalkorDB Cloud free tier (100 MB)")
    a = ap.parse_args()

    db = FalkorDB(host=os.getenv("FALKORDB_HOST", "localhost"), port=int(os.getenv("FALKORDB_PORT", 6379)),
                  username=os.getenv("FALKORDB_USERNAME") or None, password=os.getenv("FALKORDB_PASSWORD") or None)
    g = db.select_graph(a.graph)
    if a.reset:
        try:
            g.delete()
        except Exception:
            pass
        g = db.select_graph(a.graph)

    if a.from_dump:
        with open_dump(a.from_dump) as f:
            write_graph(g, json.load(f))
        return

    # ---------------- metadata universe ----------------
    import pandas as pd
    meta = pd.read_parquet(a.meta)
    cases = {}
    by_year_page = defaultdict(list)
    path_to_key = {}
    for r in meta.itertuples():
        k = cite_key(r.citation or "")
        if not k or k in cases:
            continue
        date = parse_date(r.decision_date)
        cases[k] = {
            "key": k, "scr": r.citation, "insc": r.case_id or "", "title": r.title or "",
            "short": short_title(r.title), "petitioner": (r.petitioner or "")[:200],
            "respondent": (r.respondent or "")[:200], "date": date or "",
            "year": int(date[:4]) if date else int(k.split(":")[1]),
            "disposal": r.disposal_nature or "", "author": (r.judge or "").strip(),
            "bench_size": 0, "reported": False, "stub": False, "headnote": "", "held": "", "keywords": "",
        }
        _, y, s, v, p = k.split(":")
        by_year_page[(y, s, p)].append(k)
        if r.path:
            path_to_key[r.path] = k
    print("metadata cases:", len(cases))

    # ---------------- extracted judgments ----------------
    recs = []
    for f in sorted(glob.glob(os.path.join(a.extracted, "*.jsonl"))):
        recs += [json.loads(l) for l in open(f)]
    print("extracted judgments:", len(recs))

    alias = {}  # SCC/AIR key -> SCR key, from headnote pairs
    names = {}  # key -> case name seen in headnotes
    for r in recs:
        for nc in r.get("named_cases", []):
            scr = [k for k in nc["keys"] if k.startswith("SCR")]
            for k in nc["keys"]:
                names.setdefault(k, nc["name"])
                if scr and not k.startswith("SCR"):
                    alias.setdefault(k, scr[0])

    matcher = NameMatcher(cases)
    name_hits = {}

    def resolve(k):
        if not k:
            return None
        if k in cases:
            return k
        if k in alias and alias[k] in cases:
            return alias[k]
        if k.startswith("SCR"):
            _, y, s, v, p = k.split(":")
            cands = by_year_page.get((y, s, p), [])
            if len(cands) == 1:
                return cands[0]
            if alias.get(k) in cases:
                return alias[k]
        if k not in name_hits:  # SCC/AIR (or unmatched SCR) citation: match the parties' names
            yr = re.search(r":(\d{4}):", k)
            name_hits[k] = matcher.match(names.get(k, ""), int(yr.group(1))) if yr and names.get(k) else None
        return name_hits[k]

    judge_names = {}
    bench_rows, author_rows, prov_rows, act_rows, cite_rows = [], [], [], [], []
    stubs = {}
    for r in recs:
        k = path_to_key.get(r["path"]) or resolve(r.get("key"))
        if not k:
            continue
        c = cases[k]
        c.update(reported=True, bench_size=len(r["bench"]), headnote=r["headnote"][:6000], held=r["held"][:3000],
                 keywords="; ".join(r.get("keywords", []))[:1500])
        if r.get("author_marked"):
            c["author"] = r["author_marked"]
        if r.get("insc") and not c["insc"]:
            c["insc"] = r["insc"]
        for j in r["bench"]:
            jk = judge_key(j)
            if jk:
                judge_names.setdefault(jk, Counter())[j.title()] += 1
                bench_rows.append({"j": jk, "k": k})
        paras = paragraphs(r["body"])
        for st in r["statutes"]:
            act = normalise_act(st["act"])
            act_rows.append({"a": act, "k": k})
            for p in st["provisions"]:
                for ref in split_provisions(p):
                    prov_rows.append({"a": act, "id": f"{act} | {ref}", "ref": ref, "k": k})
        seen = set()
        for ct in r["cites"]:
            tk = resolve(ct["key"])
            if not tk:
                tk = alias.get(ct["key"], ct["key"])
                if not tk:
                    continue
                if tk not in stubs and tk not in cases:
                    nm = clean_stub_name(ct.get("name") or names.get(ct["key"]) or names.get(tk) or "") or ct["cite_raw"]
                    yr = re.search(r"\d{4}", ct["cite_raw"])
                    stubs[tk] = {"key": tk, "scr": ct["cite_raw"], "title": nm, "short": short_title(nm),
                                 "year": int(yr.group(0)) if yr else 0, "stub": True}
            if tk == k or (tk, ct["treatment"]) in seen:
                continue
            seen.add((tk, ct["treatment"]))
            cite_rows.append({"s": k, "t": tk, "tr": ct["treatment"], "pol": polarity(ct["treatment"]),
                              "paras": ct["paras"],
                              "ctx": para_context(paras, ct["paras"]) or name_context(paras, ct.get("name") or names.get(ct["key"], ""))})

    print("resolved by party-name match:", sum(1 for v in name_hits.values() if v))
    print(f"citations: {len(cite_rows)}  stubs: {len(stubs)}  bench links: {len(bench_rows)}  provisions: {len(prov_rows)}")

    if a.compact:
        for c in cases.values():
            c["headnote"] = ""
            c["held"] = (c.get("held") or "")[:350]
            c["keywords"] = (c.get("keywords") or "")[:300]
            c["respondent"] = c["respondent"][:80]
            c["petitioner"] = c["petitioner"][:80]
            c["title"] = ""
        for r in cite_rows:
            r["ctx"] = r["ctx"][:600] if r["pol"] in ("NEGATIVE", "PARTIAL", "CAUTION") else r["ctx"][:250] if r["pol"] == "POSITIVE" else ""

    # quality filters (always): drop OCR-noise Acts/provisions/judge spellings seen fewer than 3 times
    act_n = Counter(r["a"] for r in act_rows)
    act_rows = [r for r in act_rows if act_n[r["a"]] >= 3]
    prov_n = Counter(r["id"] for r in prov_rows)
    prov_rows = [r for r in prov_rows if act_n[r["a"]] >= 3 and prov_n[r["id"]] >= 2]
    jn = Counter(r["j"] for r in bench_rows)
    bench_rows = [r for r in bench_rows if jn[r["j"]] >= 3]
    judge_names = {k: v for k, v in judge_names.items() if jn[k] >= 3}

    # ---------------- assemble ----------------
    # Only load the part of the universe that is connected or reported (keeps the cloud graph small)
    keep = {r["s"] for r in cite_rows} | {r["t"] for r in cite_rows} | {r["k"] for r in bench_rows}
    rows = [c for k, c in cases.items() if k in keep]
    author = []
    for c in rows:
        jk = judge_key(c["author"])
        if jk and jk in judge_names:
            author.append({"j": jk, "k": c["key"]})
    jrows = [{"id": jk, "name": max(cnt, key=lambda v: (len(re.findall(r"[A-Za-z]{3,}", v)), cnt[v])),
              "aliases": sorted(cnt)[:8]} for jk, cnt in judge_names.items()]
    data = {"cases": rows, "stubs": list(stubs.values()), "judges": jrows, "bench": bench_rows, "author": author,
            "acts": act_rows, "provisions": prov_rows, "cites": cite_rows}
    if a.save_dump:
        opener = lzma.open if str(a.save_dump).endswith(".xz") else gzip.open
        with opener(a.save_dump, "wt") as f:
            json.dump(data, f)
        print("dump saved:", a.save_dump)
    write_graph(g, data)


def write_graph(g, d):
    rows, stubs, jrows, bench_rows, author = d["cases"], d["stubs"], d["judges"], d["bench"], d["author"]
    act_rows, prov_rows, cite_rows = d["acts"], d["provisions"], d["cites"]
    # ---------------- write ----------------
    for q in [
        "CREATE INDEX FOR (c:Case) ON (c.key)", "CREATE INDEX FOR (c:Case) ON (c.year)",
        "CREATE INDEX FOR (j:Judge) ON (j.id)", "CREATE INDEX FOR (a:Act) ON (a.name)",
        "CREATE INDEX FOR (p:Provision) ON (p.id)",
    ]:
        try:
            g.query(q)
        except Exception:
            pass

    for b in chunks(rows, 500):
        g.query("""UNWIND $rows AS r MERGE (c:Case {key:r.key})
                   SET c.scr=r.scr, c.insc=r.insc, c.title=r.title, c.short=r.short, c.petitioner=r.petitioner,
                       c.respondent=r.respondent, c.date=r.date, c.year=r.year, c.disposal=r.disposal,
                       c.author=r.author, c.bench_size=r.bench_size, c.reported=r.reported, c.stub=false,
                       c.headnote=r.headnote, c.held=r.held, c.keywords=r.keywords""", {"rows": b})
    for b in chunks(stubs, 500):
        g.query("""UNWIND $rows AS r MERGE (c:Case {key:r.key})
                   SET c.scr=r.scr, c.title=r.title, c.short=r.short, c.year=r.year, c.stub=true,
                       c.reported=false, c.bench_size=0, c.headnote='', c.held='', c.date=''""", {"rows": b})
    print("cases written:", len(rows) + len(stubs))

    for b in chunks(jrows, 1000):
        g.query("UNWIND $rows AS r MERGE (j:Judge {id:r.id}) SET j.name=r.name, j.aliases=r.aliases", {"rows": b})
    for b in chunks(bench_rows, 1000):
        g.query("UNWIND $rows AS r MATCH (c:Case {key:r.k}), (j:Judge {id:r.j}) MERGE (j)-[:ON_BENCH]->(c)", {"rows": b})
    for b in chunks(author, 1000):
        g.query("UNWIND $rows AS r MATCH (c:Case {key:r.k}), (j:Judge {id:r.j}) MERGE (j)-[:AUTHORED]->(c)", {"rows": b})
    for b in chunks(act_rows, 1000):
        g.query("UNWIND $rows AS r MATCH (c:Case {key:r.k}) MERGE (a:Act {name:r.a}) MERGE (c)-[:UNDER]->(a)", {"rows": b})
    for b in chunks(prov_rows, 1000):
        g.query("""UNWIND $rows AS r MATCH (c:Case {key:r.k}) MERGE (a:Act {name:r.a})
                   MERGE (p:Provision {id:r.id}) SET p.act=r.a, p.ref=r.ref
                   MERGE (p)-[:OF]->(a) MERGE (c)-[:INTERPRETS]->(p)""", {"rows": b})
    for b in chunks(cite_rows, 1000):
        g.query("""UNWIND $rows AS r MATCH (s:Case {key:r.s}), (t:Case {key:r.t})
                   CREATE (s)-[:CITES {treatment:r.tr, polarity:r.pol, paras:r.paras, context:r.ctx}]->(t)""", {"rows": b})
    print("edges written")

    # ---------------- graph algorithms, materialised ----------------
    g.query("""CALL algo.pageRank('Case','CITES') YIELD node, score SET node.pagerank = score""")
    try:
        g.query("""CALL algo.labelPropagation() YIELD node, communityId
                   WITH node, communityId WHERE node:Case SET node.community = communityId""")
    except Exception as e:
        print("community detection skipped:", e)
    try:
        g.query("CALL db.idx.fulltext.createNodeIndex('Case', 'short', 'title', 'keywords', 'held', 'headnote', 'scr', 'insc')")
    except Exception as e:
        print("fulltext:", e)
    try:
        g.query("CALL db.idx.fulltext.createNodeIndex('Provision', 'id')")
    except Exception as e:
        print("fulltext prov:", e)
    print(g.query("MATCH (n) RETURN labels(n)[0] AS l, count(*) ORDER BY l").result_set)
    print(g.query("MATCH ()-[e]->() RETURN type(e), count(*)").result_set)


if __name__ == "__main__":
    main()
