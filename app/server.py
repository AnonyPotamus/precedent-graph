"""FastAPI server: streaming agent endpoint + direct graph endpoints + static UI."""
import contextlib
import json
import os
from pathlib import Path

# load .env (no extra dependency)
_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if "=" in _line and not _line.lstrip().startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from . import graph_tools as gt  # noqa: E402
from .agent import run  # noqa: E402
from .mcp_server import mcp  # noqa: E402


@contextlib.asynccontextmanager
async def lifespan(_app):
    if os.getenv("SKIP_BOOTSTRAP") != "1":
        from .bootstrap import ensure_graph
        ensure_graph()
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Precedent Graph", lifespan=lifespan)
app.mount("/mcp", mcp.streamable_http_app())
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    # always revalidate the page so browsers never run a stale UI after a deploy
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/health")
def health():
    try:
        return {"ok": True, "stats": gt.stats(), "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
                "agent": bool(os.getenv("ANTHROPIC_API_KEY")), "falkordb": os.getenv("FALKORDB_MODE", "external")}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.post("/api/ask")
async def ask(req: Request):
    body = await req.json()
    messages = body.get("messages", [])[-12:]

    def stream():
        for ev in run(messages):
            if ev.get("type") != "noop":
                yield f"data: {json.dumps(ev, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _wrap(fn, **kw):
    try:
        return JSONResponse(json.loads(json.dumps(fn(**kw), default=str)))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/search")
def search(q: str, limit: int = 8):
    return _wrap(gt.search_cases, query=q, limit=limit)


@app.get("/api/goodlaw")
def goodlaw(case: str):
    return _wrap(gt.check_good_law, case=case)


@app.get("/api/case")
def case(case: str):
    return _wrap(gt.get_case, case=case)


@app.get("/api/citing")
def citing(case: str, polarity: str | None = None):
    return _wrap(gt.citing_cases, case=case, polarity=polarity)


@app.get("/api/path")
def path(a: str, b: str):
    return _wrap(gt.connection_path, case_a=a, case_b=b)


@app.get("/api/judge")
def judge(name: str):
    return _wrap(gt.judge_profile, name=name)
