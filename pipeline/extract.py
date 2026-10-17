"""Extract structured data from Supreme Court Reports (SCR) judgment PDFs.

For every judgment PDF (pdftotext -layout output) we pull:
  * its own SCR citation + neutral citation (INSC)
  * the bench (judges)
  * the editorial headnote (statutes/provisions touched, held paragraphs)
  * the "Case Law Reference" table: cited authority + treatment + paras
  * SCC <-> SCR alias pairs from the headnote's cited-case list
  * the judgment body (for grounding answers)

Usage:  python extract.py <txt_dir> <out.jsonl>
"""
import json
import os
import re
import sys

# ---------- citation normalisation -------------------------------------------------

SCR_RE = re.compile(
    r"\[(?P<year>1[89]\d\d|20\d\d)\]\s*(?:(?P<vol>\d{1,2})\s*)?(?P<supp>Supp[l]?\.?\s*(?:\(?(?P<svol>\d{1,2})\)?)?\s*)?"
    r"(?:(?P<vol2>\d{1,2})\s*)?S\.?\s?C\.?\s?R\.?\s*(?P<page>\d{1,4})",
    re.I,
)
SCC_RE = re.compile(r"\((?P<year>1[89]\d\d|20\d\d)\)\s*(?:(?P<vol>\d{1,2})\s*)?(?P<supp>Supp\.?\s*)?(?:S\.?\s?C\.?\s?C|sec)\.?\s*(?P<page>\d{1,4})", re.I)
AIR_RE = re.compile(r"AIR\s*\(?(?P<year>1[89]\d\d|20\d\d)\)?\s*SC\s*(?P<page>\d{1,5})", re.I)
SCR_OLD_RE = re.compile(r"(?P<year>1[89]\d\d|20\d\d)\s*\(\s*(?P<vol>\d{1,2})\s*\)\s*(?P<supp>Supp[l]?\.?\s*)?S\.?\s?C\.?\s?R\.?\s*(?P<page>\d{1,4})", re.I)
SCC_OLD_RE = re.compile(r"(?P<year>1[89]\d\d|20\d\d)\s*(?:\(\s*(?P<vol>\d{1,2})\s*\)\s*)?(?P<supp>Supp\.?\s*)?(?:S\.?\s?C\.?\s?C|sec)\.?\s*(?P<page>\d{1,4})", re.I)
INSC_RE = re.compile(r"(?P<year>20\d\d)\s*INSC\s*(?P<num>\d{1,5})", re.I)


def scr_key(m):
    """Canonical key for an SCR citation match: SCR:<year>:<S|>:<vol|0>:<page>."""
    year = m.group("year")
    supp = bool(m.group("supp"))
    vol = m.group("vol") or m.group("svol") or m.group("vol2") or "0"
    return f"SCR:{year}:{'S' if supp else ''}:{int(vol)}:{int(m.group('page'))}"


def scc_key(m):
    vol = m.group("vol") or "0"
    return f"SCC:{m.group('year')}:{'S' if m.group('supp') else ''}:{int(vol)}:{int(m.group('page'))}"


def cite_key(text):
    """Return canonical key for the first recognised citation in text, else None."""
    m = SCR_RE.search(text)
    if m:
        return scr_key(m)
    m = SCR_OLD_RE.search(text)
    if m:
        return f"SCR:{m.group('year')}:{'S' if m.group('supp') else ''}:{int(m.group('vol'))}:{int(m.group('page'))}"
    m = SCC_RE.search(text) or SCC_OLD_RE.search(text)
    if m:
        return scc_key(m)
    m = AIR_RE.search(text)
    if m:
        return f"AIR:{m.group('year')}:{int(m.group('page'))}"
    m = INSC_RE.search(text)
    if m:
        return f"INSC:{m.group('year')}:{int(m.group('num'))}"
    return None


# ---------- text cleanup ---------------------------------------------------------

MARGIN_RE = re.compile(r"^\s{0,3}[A-H]\s{2,}|\s{2,}[A-H]\s*$")
PAGE_HDR_RE = re.compile(r"^\s*(\d{1,4}\s+)?SUPREME COURT REPORTS\s+\[\d{4}\].*$|^\s*\d{1,4}\s*$", re.I)


def clean_lines(text):
    out = []
    for ln in text.splitlines():
        if PAGE_HDR_RE.match(ln):
            continue
        ln = MARGIN_RE.sub(" ", ln)
        ln = MARGIN_RE.sub(" ", ln)
        if re.fullmatch(r"\s*[A-H]\s*", ln):
            continue
        out.append(ln.rstrip())
    return out


def squash(s):
    return re.sub(r"\s+", " ", s).strip()


# ---------- section parsers ------------------------------------------------------

TREATMENTS = [
    "partly overruled", "overruled", "held per incuriam", "per incuriam", "not good law",
    "held inapplicable", "inapplicable", "not followed", "disapproved", "doubted", "distinguished",
    "reversed", "set aside", "modified", "affirmed", "approved", "upheld", "relied on", "relied upon",
    "followed", "explained", "applied", "considered", "referred to", "cited", "discussed", "held not applicable",
]
TREAT_RE = re.compile("|".join(re.escape(t) for t in sorted(TREATMENTS, key=len, reverse=True)), re.I)

ROW_CITE_RE = re.compile(
    r"(\[\d{4}\]\s*(?:\d{1,2}\s*)?(?:Supp[l]?\.?\s*\(?\d{0,2}\)?\s*)?(?:\d{1,2}\s*)?S\.?\s?C\.?\s?R\.?\s*\d{1,4}"
    r"|\(\d{4}\)\s*(?:\d{1,2}\s*)?(?:Supp\.?\s*)?S\.?\s?C\.?\s?C\.?\s*\d{1,4}"
    r"|\d{4}\s*\(\s*\d{1,2}\s*\)\s*(?:Supp[l]?\.?\s*)?S\.?\s?C\.?\s?R\.?\s*\d{1,4}"
    r"|\d{4}\s*(?:\(\s*\d{1,2}\s*\)\s*)?(?:Supp\.?\s*)?(?:S\.?\s?C\.?\s?C|sec)\.?\s*\d{1,4}"
    r"|\(\d{4}\)\s*(?:\d{1,2}\s*)?sec\s*\d{1,4}"
    r"|AIR\s*\(?\d{4}\)?\s*SC\s*\d{1,5}|\d{4}\s*INSC\s*\d{1,5})",
    re.I,
)


def parse_case_law_table(lines):
    """Rows like:  [2014] 13 SCR 149     referred to     Para 2.4"""
    rows = []
    start = None
    for i, ln in enumerate(lines):
        if re.search(r"Case\s+Law\s+Reference", ln, re.I):
            start = i + 1
            break
    if start is None:
        return rows
    for ln in lines[start:start + 400]:
        if re.search(r"(APPELLATE|ORIGINAL|ADVISORY|REVIEW|WRIT|EXTRA\s*ORDINARY|INHERENT)\s+JURISDICTION|delivered by|JUDGMENT", ln):
            break
        m = ROW_CITE_RE.search(ln)
        if not m:
            continue
        rest = ln[m.end():]
        t = TREAT_RE.search(rest)
        paras = re.search(r"Paras?\.?\s*([\d\.,\s\-andto]+)", rest, re.I)
        rows.append({
            "cite_raw": squash(m.group(1)),
            "key": cite_key(m.group(1)),
            "treatment": t.group(0).lower() if t else "referred to",
            "paras": squash(paras.group(1)).strip(" ,") if paras else "",
        })
    return rows


LIST_START_RE = re.compile(r"LIST\s+OF\s+CITATIONS\s+AND\s+OTHER\s+REFERENCES|Case\s+Law\s+Cited", re.I)
LIST_END_RE = re.compile(r"OTHER\s+CASE\s+DETAILS|LIST\s+OF\s+ACTS|LIST\s+OF\s+KEYWORDS|Books\s+and\s+Periodicals|JUDGMENT\s*/\s*ORDER\s+OF|Case\s+Arising\s+From|Other\s+Case\s+Details", re.I)


def parse_citation_list(head, fallback=False):
    """Newer SCR layout: 'LIST OF CITATIONS AND OTHER REFERENCES  A v. B (2010) 11 SCC 175 : [2010] 3 SCR 9;
    C v. D (1988) 3 SCC 751 – referred to. E v. F ... – overruled.'"""
    flat = squash(head)
    m = LIST_START_RE.search(flat)
    if m:
        sec = flat[m.end():]
    elif fallback:  # older SCR: '... Name (1999) 3 SCC 368 : 1990 (2) SCR 468 - relied on.' inside the headnote
        h = re.search(r"HELD\s*:?", flat)
        sec = flat[h.end():] if h else flat
    else:
        return []
    e = LIST_END_RE.search(sec)
    sec = sec[:e.start()] if e else sec[:20000]
    rows = []
    for g in re.finditer(r"(?P<body>.+?)\s*[–—-]\s*(?P<tr>" + TREAT_RE.pattern + r")\s*[\.;]", sec, re.I | re.S):
        tr = g.group("tr").lower()
        for entry in re.split(r";|\s+and\s+(?=[A-Z][^;:]{6,120}?\s(?:v\.|vs\.?|versus)\s)", g.group("body")):
            cites = [c.group(0) for c in ROW_CITE_RE.finditer(entry)]
            keys = [k for k in (cite_key(c) for c in cites) if k]
            if not keys:
                continue
            first = ROW_CITE_RE.search(entry)
            name = squash(entry[:first.start()]).strip(" :,.=")
            name = re.split(r"\]\s+|(?:(?<=[a-z]{4}\.)|(?<=\d\.))\s+(?=[A-Z])", name)[-1]
            primary = next((k for k in keys if k.startswith("SCR")), keys[0])
            rows.append({"cite_raw": " : ".join(cites), "key": primary, "alt_keys": keys, "name": name[-180:],
                         "treatment": tr, "paras": ""})
    return rows


NAME_CITE_RE = re.compile(
    r"(?P<name>[A-Z][^;\n]{3,200}?\s(?:v\.|vs\.?|versus)\s[^;\n]{2,200}?)\s*"
    r"(?P<cites>(?:(?:\(\d{4}\)|\[\d{4}\]|AIR)\s*[^;–—\-]{3,40}?(?:SCC|SCR|SC)\s*\d{1,5}\s*(?::\s*)?)+)",
    re.I,
)


def parse_headnote_cases(head):
    """'Banwasi Seva Ashram vs State Of U.P. (1986) 4 SCC 753 : [1987] 1 SCR 336 – explained.'
    -> name, list of keys, treatment.  Also yields SCC<->SCR alias pairs."""
    flat = squash(head)
    out = []
    # split into segments ending with "– <treatment>."
    for seg in re.split(r"[–—-]\s*(?:" + TREAT_RE.pattern + r")\s*[\.;]", flat, flags=re.I):
        for m in NAME_CITE_RE.finditer(seg):
            keys = [k for k in (cite_key(c) for c in re.split(r":", m.group("cites"))) if k]
            if keys:
                name = squash(m.group("name"))
                chunks = re.split(r"(?<=\])\s+|(?<=[a-z]{4}\.)\s+(?=[A-Z])", name)
                for k in range(len(chunks) - 1, -1, -1):
                    tail = " ".join(chunks[k:])
                    if re.search(r"\s(v\.|vs\.?|versus)\s", tail, re.I):
                        name = tail
                        break
                out.append({"name": name[-180:], "keys": keys})
    return out


STATUTE_RE = re.compile(
    r"(?P<act>[A-Z][A-Za-z\.\'&\(\) ]{2,90}?,\s*(?P<year>1[89]\d\d|20\d\d))"
    r"(?:\s*[:–—-]?\s*(?P<prov>(?:ss?\.|Arts?\.|rr?\.|O\.|Order|Section|Article|Sch(?:edule)?\.?)\s*[\dA-Z][\w\(\)]*(?:\s*(?:,|and|&|to|r/w)\s*[\dA-Z][\w\(\)]*)*))?"
)


def parse_statutes(head):
    flat = squash(head)
    found = {}
    for m in STATUTE_RE.finditer(flat):
        act = squash(m.group("act"))
        act = re.sub(r"^(?:[A-H]\.\s+)?(?:(?:and|the|of|under|r/w|with|in|to)\s+)*", "", act, flags=re.I)
        if len(act.split(",")[0].split()) < 2 or len(act) > 100 or not re.search(r"\b(Act|Code|Rules|Constitution|Regulations?|Sanhita|Adhiniyam|Order|Ordinance)\b", act):
            continue
        prov = squash(m.group("prov") or "")
        found.setdefault(act, set())
        if prov:
            found[act].add(prov)
    return [{"act": a, "provisions": sorted(p)} for a, p in found.items()]


BENCH_RE = re.compile(r"\[\s*((?:Dr\.?\s*|DR\.?\s*)?[A-Z][A-Za-z\.\s,’'\-\*]+?),?\s*(?:JJ|J|C\.?\s?J\.?\s?I?)\s*[\.,]?\s*[\]\)]", re.S)


def parse_lists(head):
    """2024+ layout: 'List of Acts A; B. List of Keywords k1; k2; ... Case Arising From'"""
    flat = squash(head)
    out = {"acts": [], "keywords": []}
    for key, pat in (("acts", r"List\s+of\s+Acts"), ("keywords", r"List\s+of\s+Keywords")):
        m = re.search(pat + r"\s*(.*?)(?=List\s+of\s+(?:Acts|Keywords)|Case\s+Arising\s+From|Other\s+Case\s+Details|OTHER\s+CASE\s+DETAILS|Appearances|$)", flat, re.I)
        if m:
            body = re.sub(r"\[\d{4}\]\s*\d+\s*S\.C\.R\.\s*\d+\s+.*?(?:v\.|vs\.).*?(?:Ors\.|Anr\.|[A-Z][a-z]+\.)\s", " ", m.group(1))
            items = [squash(x).strip(" .") for x in re.split(r";", body)]
            out[key] = [x for x in items if 2 < len(x) < 90][:40]
    return out


def parse_bench(lines):
    head = "\n".join(lines[:60])
    m = BENCH_RE.search(head)
    if not m:
        return []
    raw = squash(m.group(1))
    parts = re.split(r",\s*|\s+(?:AND|and)\s+|\s*&\s*", raw)
    judges = []
    for p in parts:
        p = re.sub(r"\b(CJI|C\.J\.I\.|CJ|JJ|J)\b\.?$", "", p.strip()).strip(" .,")
        p = p.replace("*", "").strip()
        if len(p) > 3:
            judges.append(p)
    return judges


def split_sections(lines):
    """Return (headnote_lines, body_lines)."""
    body_start = None
    for i, ln in enumerate(lines):
        if re.search(r"Judgment of the Court was delivered by|delivered the following|^\s*J\s*U\s*D\s*G\s*M\s*E\s*N\s*T\s*$|^\s*O\s*R\s*D\s*E\s*R\s*$", ln):
            body_start = i
            break
    if body_start is None:
        body_start = min(len(lines), 120)
    clr = next((i for i, ln in enumerate(lines[:body_start]) if re.search(r"Case\s+Law\s+Reference", ln, re.I)), body_start)
    return lines[:clr], lines[body_start:]


def extract(path):
    raw = open(path, encoding="utf-8", errors="ignore").read()
    if len(raw) < 200:
        return None
    first = raw[:600]
    m = SCR_RE.search(first)
    lines = clean_lines(raw)
    head_lines, body_lines = split_sections(lines)
    head = "\n".join(head_lines)
    table = parse_case_law_table(lines)
    listed = parse_citation_list(head) or ([] if table else parse_citation_list(head, fallback=True))
    # merge: table rows win; list rows add names / alias keys
    seen = {r["key"] for r in table}
    cites = table + [r for r in listed if r["key"] not in seen]
    insc = INSC_RE.search(first)
    lists = parse_lists(head)
    bm = BENCH_RE.search("\n".join(lines[:60]))
    am = re.search(r"([A-Z][A-Za-z\.\s’'\-]+?)\*", bm.group(1)) if bm else None
    author = re.split(r",\s*|\s+(?:AND|and)\s+", am.group(1))[-1].strip() if am else ""
    held = re.search(r"HELD\s*:?(.*)", squash(head), re.S)
    return {
        "file": os.path.basename(path),
        "path": re.sub(r"_EN$", "", os.path.basename(path)[:-4]),
        "key": scr_key(m) if m else None,
        "scr": squash(m.group(0)) if m else "",
        "insc": f"{insc.group('year')} INSC {insc.group('num')}" if insc else "",
        "bench": parse_bench(lines),
        "statutes": parse_statutes(head[:6000]) + [{"act": a, "provisions": []} for a in lists["acts"]],
        "keywords": lists["keywords"],
        "author_marked": author,
        "headnote": squash(head)[:8000],
        "held": squash(held.group(1))[:4000] if held else "",
        "cites": cites,
        "named_cases": parse_headnote_cases(head) + [{"name": r["name"], "keys": r["alt_keys"]} for r in listed if r["name"]],
        "body": "\n".join(body_lines)[:200000],
    }


def _safe(path):
    try:
        return extract(path)
    except Exception as e:  # keep going on bad PDFs
        print("ERR", path, e, file=sys.stderr)
        return None


def main(txt_dir, out_path):
    from multiprocessing import Pool
    files = [os.path.join(txt_dir, f) for f in sorted(os.listdir(txt_dir)) if f.endswith(".txt")]
    ok = 0
    with Pool(int(os.getenv("WORKERS", os.cpu_count() or 2))) as pool, open(out_path, "w") as fo:
        for rec in pool.imap(_safe, files, chunksize=8):
            if rec:
                ok += 1
                fo.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"{txt_dir}: {ok}/{len(files)} extracted -> {out_path}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
