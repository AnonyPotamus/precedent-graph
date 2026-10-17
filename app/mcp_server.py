"""MCP server exposing the precedent-graph tools to any MCP client (Claude Desktop, Claude Code, IDE agents).

stdio:  python -m app.mcp_server
HTTP:   mounted by app.server at /mcp (streamable HTTP), e.g. claude mcp add --transport http precedents https://<host>/mcp
"""
import json
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import graph_tools as gt

# DNS-rebinding protection stays on, but the public domain (DOMAIN / MCP_ALLOWED_HOSTS) must be allowed,
# otherwise the SDK only accepts localhost and answers "Invalid Host header".
_hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
for _h in filter(None, [os.getenv("DOMAIN")] + os.getenv("MCP_ALLOWED_HOSTS", "").split(",")):
    _h = _h.strip()
    _hosts += [_h, f"{_h}:*"]
_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=_hosts,
    allowed_origins=[f"https://{h}" for h in _hosts if "*" not in h] + [f"http://{h}" for h in _hosts if "*" not in h],
)

mcp = FastMCP(
    "precedent-graph",
    instructions=("Indian Supreme Court citation graph on FalkorDB. Use check_good_law before relying on any precedent; "
                  "it returns verdict, the likely or inferred overruling judgment, and multi-hop evidence paths."),
    stateless_http=True,
    streamable_http_path="/",
    transport_security=_security,
)


def _out(x):
    if isinstance(x, dict):
        x = {k: v for k, v in x.items() if k != "graph"}
    return json.loads(json.dumps(x, default=str))


@mcp.tool()
def search_cases(query: str, limit: int = 8, year_from: int | None = None, year_to: int | None = None) -> dict:
    """Full-text search over Supreme Court case names, citations, keywords and holdings."""
    return _out(gt.search_cases(query, limit, year_from, year_to))


@mcp.tool()
def get_case(case: str) -> dict:
    """Details of one case (name, SCR or INSC citation): bench, provisions, holding, and how it treats / is treated by others."""
    return _out(gt.get_case(case))


@mcp.tool()
def check_good_law(case: str) -> dict:
    """Is this precedent still good law? Multi-hop: negative treatment with bench strength, likely or graph-inferred
    overruling judgment, reliance on overruled authority up to 2 hops, and later reliance after overruling."""
    return _out(gt.check_good_law(case))


@mcp.tool()
def citing_cases(case: str, polarity: str | None = None, limit: int = 15) -> dict:
    """Later judgments citing a case. polarity: POSITIVE, NEGATIVE, PARTIAL, CAUTION, DISTINGUISHED or NEUTRAL."""
    return _out(gt.citing_cases(case, polarity, limit))


@mcp.tool()
def connection_path(case_a: str, case_b: str, max_len: int = 6) -> dict:
    """Shortest citation chains between two cases, with the treatment on each hop."""
    return _out(gt.connection_path(case_a, case_b, max_len))


@mcp.tool()
def cases_on_provision(provision: str, limit: int = 12) -> dict:
    """Most authoritative judgments on a provision, e.g. 'Code of Criminal Procedure s. 482'."""
    return _out(gt.cases_on_provision(provision, limit))


@mcp.tool()
def judge_profile(name: str) -> dict:
    """A judge's benches, authored judgments, precedents their benches overruled, top Acts and co-judges."""
    return _out(gt.judge_profile(name))


@mcp.tool()
def treatment_search(treatment: str, min_bench: int | None = None, year_from: int | None = None, limit: int = 25) -> dict:
    """Judgments that received a treatment across the corpus, e.g. 'per incuriam', 'overruled', 'doubted',
    'not good law', 'partly overruled', with the judgment that applied it and both bench sizes."""
    return _out(gt.treatment_search(treatment, min_bench, year_from, limit))


@mcp.tool()
def landmark_cases(act: str | None = None, limit: int = 10) -> dict:
    """Most influential precedents by citation PageRank, overall or under an Act (e.g. 'Arbitration and Conciliation Act')."""
    return _out(gt.landmark_cases(act, limit))


@mcp.tool()
def run_cypher(query: str) -> dict:
    """Read-only openCypher against the graph. Schema:
    (:Case {key, scr, insc, short, date, year, bench_size, reported, stub, held, keywords, pagerank})
    (:Judge)-[:ON_BENCH|AUTHORED]->(:Case); (:Case)-[:UNDER]->(:Act); (:Case)-[:INTERPRETS]->(:Provision)-[:OF]->(:Act)
    (:Case)-[:CITES {treatment, polarity, paras, context}]->(:Case)"""
    return _out(gt.run_cypher(query))


if __name__ == "__main__":
    mcp.run()
