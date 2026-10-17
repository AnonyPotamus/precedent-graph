"""Precedent Graph Agent: Claude + FalkorDB tools, streamed as events.

Events yielded by `run()`:
  {"type":"tool_call", "id", "name", "input"}
  {"type":"tool_result", "id", "name", "summary", "graph"?}
  {"type":"text", "delta"}
  {"type":"done", "graph", "usage"}
  {"type":"error", "message"}
"""
import json
import os

import anthropic

from .graph_tools import SCHEMA, TOOLS, stats

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", 8))

CASE_ARG = {"type": "string", "description": "Case name (e.g. 'SMS Tea Estates v. Chandmari Tea'), SCR citation ('[2011] 9 S.C.R. 382'), neutral citation ('2023 INSC 1066') or graph key."}

TOOL_SPECS = [
    {"name": "search_cases", "description": "Full-text search over Supreme Court case names, citations and editorial headnotes. Use to find candidate precedents for a topic or to identify a case the user named loosely.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 8},
                                                         "year_from": {"type": "integer"}, "year_to": {"type": "integer"}}, "required": ["query"]}},
    {"name": "get_case", "description": "Full details of one case: bench, author, disposal, statutory provisions interpreted, editorial holding, how it treated the cases it cites, and counts of how later cases treated it.",
     "input_schema": {"type": "object", "properties": {"case": CASE_ARG}, "required": ["case"]}},
    {"name": "check_good_law", "description": "THE core tool. Decides whether a precedent is still good law by multi-hop graph reasoning: (1) later judgments marking it overruled / per incuriam / doubted, with bench strength, (2) the likely overruling judgment, (3) authorities it relied on that were themselves later overruled (1-2 hops upstream), (4) later cases still relying on it after negative treatment. Returns the evidence paths.",
     "input_schema": {"type": "object", "properties": {"case": CASE_ARG}, "required": ["case"]}},
    {"name": "citing_cases", "description": "Later judgments citing a case, optionally filtered by polarity: POSITIVE (relied on/followed/approved), NEGATIVE (overruled/per incuriam), PARTIAL, CAUTION (doubted/not followed), DISTINGUISHED, NEUTRAL (referred to). Includes the paragraph where it was cited.",
     "input_schema": {"type": "object", "properties": {"case": CASE_ARG, "polarity": {"type": "string", "enum": ["POSITIVE", "NEGATIVE", "PARTIAL", "CAUTION", "DISTINGUISHED", "NEUTRAL"]},
                                                         "limit": {"type": "integer", "default": 15}}, "required": ["case"]}},
    {"name": "connection_path", "description": "Shortest citation chain(s) between two cases with the treatment on each hop. Use to explain how two lines of authority connect.",
     "input_schema": {"type": "object", "properties": {"case_a": CASE_ARG, "case_b": CASE_ARG, "max_len": {"type": "integer", "default": 6}}, "required": ["case_a", "case_b"]}},
    {"name": "cases_on_provision", "description": "Most authoritative judgments interpreting a statutory provision, ranked by citation PageRank, with negative-treatment counts. Query like 'Code of Criminal Procedure s. 482', 'Constitution Art. 21', 'Arbitration Act s. 11'.",
     "input_schema": {"type": "object", "properties": {"provision": {"type": "string"}, "limit": {"type": "integer", "default": 12}}, "required": ["provision"]}},
    {"name": "landmark_cases", "description": "Most influential precedents by citation PageRank, overall or under an Act.",
     "input_schema": {"type": "object", "properties": {"act": {"type": "string"}, "limit": {"type": "integer", "default": 10}}}},
    {"name": "judge_profile", "description": "A judge's footprint: number of benches, authored judgments, precedents their benches overruled, top Acts, frequent co-judges.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "treatment_search", "description": "Find judgments that received a particular treatment across the corpus: 'per incuriam', 'overruled', 'partly overruled', 'not good law', 'doubted', 'distinguished', etc. Returns the treated case, the judgment that applied the treatment, both bench sizes, dates and the paragraph. Use for questions like 'which judgments were held per incuriam' or 'what did 5-judge benches overrule since 2020'.",
     "input_schema": {"type": "object", "properties": {"treatment": {"type": "string"}, "min_bench": {"type": "integer", "description": "only treatments applied by benches at least this large"},
                                                         "year_from": {"type": "integer"}, "limit": {"type": "integer", "default": 25}}, "required": ["treatment"]}},
    {"name": "run_cypher", "description": "Run a READ-ONLY openCypher query on FalkorDB for questions the other tools cannot answer (aggregations, custom traversals). Always RETURN case keys (e.g. c.key, s.key) and the edge treatment so the UI can draw the result. Schema:\n" + SCHEMA,
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]


def system_prompt():
    s = stats()
    return f"""You are Precedent Graph, a legal research agent for Indian Supreme Court case law.
Your knowledge comes from a FalkorDB citation graph built from Supreme Court Reports (SCR): {s.get('reported', '?')} fully-parsed judgments
from {s.get('y0', '?')}-{s.get('y1', '?')}, {s.get('cases', '?')} cases in total and {s.get('cites', '?')} citation edges, each labelled
with the editorial treatment (relied on, followed, distinguished, overruled, per incuriam, ...).

How to work:
- Ground every legal claim in a tool result. Never rely on memory for whether a case is good law; always call check_good_law.
- When the user names a case, resolve it (search_cases or get_case) before reasoning about it.
- Prefer multi-hop answers: if a precedent the user relies on itself rests on overruled authority, say so and show the chain.
- Treatment labels record the status noted by the citing judgment. A 2-judge bench marking a 3-judge decision "overruled" is reporting
  an earlier overruling, not doing it. Use bench strength and dates ('likely_overruled_by', 'larger_or_equal_bench') to say who
  most likely overruled it, and be explicit when that is inferred.
- If the judgment that overruled a precedent was itself overruled later ('itself_overruled_by'), the verdict is
  UNCERTAIN, never "good law" (not even "appears to be good law"). Say: "Uncertain: it was overruled by X, and X was
  later overruled by Y; it may be wholly or partly restored, so read Y before relying on it."
- Lead with the tool's verdict wording. Do not upgrade a CAUTION verdict to good law or downgrade NOT GOOD LAW.
- Absence of negative treatment in this corpus is not proof of good law: say "no negative treatment found in the corpus (years ...)".

Answer format (markdown):
1. A one-line **verdict** first when the question is about validity.
2. The reasoning chain, citing every case as *Short Name* [SCR citation], with the treatment and bench size, e.g.
   *SMS Tea Estates* [2011] 9 S.C.R. 382 → marked **overruled** by *N.N. Global* [2023] 9 S.C.R. 285 (5-judge bench).
3. Quote at most one or two short lines from the paragraph context when it helps.
4. End with a short "Coverage" note if relevant. Do not give legal advice; you are a research aid.
Be concise. The UI already draws the evidence graph from your tool results."""


def _summary(name, result):
    if not isinstance(result, dict):
        return str(result)[:200]
    if "error" in result:
        return "error: " + result["error"]
    if name == "check_good_law":
        return f"{result['case']['name']}: {result['verdict']}"
    for k in ("results", "citing", "paths", "rows"):
        if k in result:
            return f"{len(result[k])} {k}"
    if name == "get_case":
        return f"{result.get('name')} {result.get('scr')}"
    if name == "judge_profile":
        return f"{result.get('judge')}: {result.get('benches')} benches"
    return "ok"


def _merge(acc, g):
    if not g:
        return
    for n in g.get("nodes", []):
        cur = acc["nodes"].get(n["id"])
        if not cur or (n.get("role") and n.get("role") != "positive"):
            acc["nodes"][n["id"]] = {**(cur or {}), **n}
    for e in g.get("edges", []):
        acc["edges"][(e["from"], e["to"], e["label"])] = e


def run(messages):
    """messages: list of {role, content(str)} from the UI conversation."""
    client = anthropic.Anthropic()
    convo = [{"role": m["role"], "content": m["content"]} for m in messages if m.get("content")]
    evidence = {"nodes": {}, "edges": {}}
    usage = {"input_tokens": 0, "output_tokens": 0}
    try:
        sysmsg = system_prompt()
        for _ in range(MAX_STEPS):
            with client.messages.stream(model=MODEL, max_tokens=2500, system=sysmsg, tools=TOOL_SPECS, messages=convo) as stream:
                for ev in stream:
                    if ev.type == "content_block_delta" and getattr(ev.delta, "type", "") == "text_delta":
                        yield {"type": "text", "delta": ev.delta.text}
                msg = stream.get_final_message()
            usage["input_tokens"] += msg.usage.input_tokens
            usage["output_tokens"] += msg.usage.output_tokens
            convo.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in msg.content]})
            calls = [b for b in msg.content if b.type == "tool_use"]
            if not calls:
                break
            results = []
            for b in calls:
                yield {"type": "tool_call", "id": b.id, "name": b.name, "input": b.input}
                fn = TOOLS.get(b.name)
                try:
                    out = fn(**b.input) if fn else {"error": f"unknown tool {b.name}"}
                except Exception as e:  # surface tool errors to the model
                    out = {"error": f"{type(e).__name__}: {e}"}
                g = out.pop("graph", None) if isinstance(out, dict) else None
                _merge(evidence, g)
                res_ev = {"type": "tool_result", "id": b.id, "name": b.name, "summary": _summary(b.name, out), "graph": g}
                if b.name == "check_good_law" and isinstance(out, dict) and "verdict" in out:
                    res_ev["verdict"] = {k: out.get(k) for k in ("verdict", "case", "likely_overruled_by", "inferred_overruling_authority",
                                                                  "negative_treatment", "weak_foundations", "positive_treatment_count")}
                yield res_ev
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out, default=str)[:30000]})
            convo.append({"role": "user", "content": results})
            if any(b.type == "text" and b.text.strip() for b in msg.content):
                yield {"type": "text", "delta": "\n\n"}
        yield {"type": "done", "graph": {"nodes": list(evidence["nodes"].values()), "edges": list(evidence["edges"].values())}, "usage": usage}
    except anthropic.APIError as e:
        yield {"type": "error", "message": f"Claude API error: {e}"}
    except Exception as e:
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
