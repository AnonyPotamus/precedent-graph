"""Make sure a FalkorDB with the precedent graph is available before serving.

* FALKORDB_HOST set  -> use that server (e.g. FalkorDB Cloud). If the graph is missing (free-tier instances are
  wiped after inactivity), load it from the bundled dump.
* FALKORDB_HOST unset -> start an embedded FalkorDB server (redis-server + falkordb.so from the falkordblite wheel)
  on localhost and load the dump into it.
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DUMP = Path(os.getenv("GRAPH_DUMP") or next((p for p in (ROOT / "data" / "precedents_full.json.xz",
                                                            ROOT / "data" / "precedents_compact.json.gz") if p.exists()),
                                            ROOT / "data" / "precedents_full.json.xz"))


def _start_embedded():
    import redislite
    bindir = Path(redislite.__file__).parent / "bin"
    server = bindir / "redis-server" if (bindir / "redis-server").exists() else shutil.which("redis-server")
    datadir = Path(os.getenv("EMBEDDED_DATA_DIR", "/tmp/falkordb"))
    datadir.mkdir(parents=True, exist_ok=True)
    port = os.getenv("FALKORDB_PORT", "6379")
    subprocess.Popen([str(server), "--port", port, "--bind", "127.0.0.1", "--dir", str(datadir),
                      "--loadmodule", str(bindir / "falkordb.so"), "--save", "", "--appendonly", "no"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.environ["FALKORDB_HOST"] = "127.0.0.1"
    os.environ["FALKORDB_PORT"] = port
    os.environ.setdefault("FALKORDB_MODE", "embedded")
    print(f"[bootstrap] embedded FalkorDB starting on 127.0.0.1:{port}", flush=True)


def ensure_graph():
    os.environ.setdefault("FALKORDB_MODE", "server" if os.getenv("FALKORDB_HOST") else "embedded")
    if not os.getenv("FALKORDB_HOST"):
        _start_embedded()
    from falkordb import FalkorDB
    name = os.getenv("FALKORDB_GRAPH", "precedents")
    remote = bool(os.getenv("FALKORDB_HOST")) and os.environ["FALKORDB_HOST"] != "127.0.0.1"
    allow_fallback = os.getenv("ALLOW_EMBEDDED_FALLBACK", "1") == "1"
    tries = int(os.getenv("FALKORDB_CONNECT_TRIES", "30"))
    for i in range(tries):
        try:
            db = FalkorDB(host=os.environ["FALKORDB_HOST"], port=int(os.getenv("FALKORDB_PORT", 6379)),
                          username=os.getenv("FALKORDB_USERNAME") or None, password=os.getenv("FALKORDB_PASSWORD") or None)
            graphs = db.list_graphs()
            break
        except Exception as e:
            if remote and allow_fallback and i >= 5:
                # FalkorDB Cloud unreachable (free instances stop when idle): fall back to embedded FalkorDB
                print(f"[bootstrap] remote FalkorDB unreachable ({e}); falling back to embedded", flush=True)
                for k in ("FALKORDB_HOST", "FALKORDB_USERNAME", "FALKORDB_PASSWORD"):
                    os.environ.pop(k, None)
                os.environ["FALKORDB_PORT"] = "6379"
                os.environ["FALKORDB_MODE"] = "embedded-fallback"
                _start_embedded()
                remote = False
                continue
            if i == tries - 1:
                raise
            time.sleep(1)
    version = dump_version()
    if name in graphs:
        g0 = db.select_graph(name)
        n = g0.ro_query("MATCH (c:Case) RETURN count(c)").result_set[0][0]
        loaded = g0.ro_query("OPTIONAL MATCH (m:Meta) RETURN m.dump_version").result_set[0][0]
        if n > 1000 and loaded == version:
            print(f"[bootstrap] graph '{name}' ready with {n} cases (dump {version})", flush=True)
            wait_for_indexes(g0)
            return
        print(f"[bootstrap] graph has dump {loaded}, bundled dump is {version}: reloading", flush=True)
    print(f"[bootstrap] loading graph '{name}' from {DUMP}", flush=True)
    sys.path.insert(0, str(ROOT))
    from pipeline.build_graph import open_dump, write_graph
    import gzip
    import json
    g = db.select_graph(name)
    try:
        g.delete()
    except Exception:
        pass
    g = db.select_graph(name)
    with open_dump(DUMP) as f:
        write_graph(g, json.load(f))
    g.query("MERGE (m:Meta) SET m.dump_version = $v, m.loaded_at = timestamp()", {"v": version})
    print("[bootstrap] graph loaded; waiting for full-text indexes", flush=True)
    wait_for_indexes(g)


def dump_version():
    import hashlib
    h = hashlib.sha1()
    with open(DUMP, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def wait_for_indexes(g, timeout=300):
    """Full-text indexes are populated asynchronously after a bulk load; don't report healthy before they answer."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            a = g.ro_query("CALL db.idx.fulltext.queryNodes('Case', 'Supreme') YIELD node RETURN count(node)").result_set[0][0]
            b = g.ro_query("CALL db.idx.fulltext.queryNodes('Provision', 'Code') YIELD node RETURN count(node)").result_set[0][0]
            if a > 0 and b > 0:
                print(f"[bootstrap] indexes ready ({int(time.time() - t0)}s)", flush=True)
                return
        except Exception:
            pass
        time.sleep(2)
    print("[bootstrap] indexes still building; continuing", flush=True)


if __name__ == "__main__":
    ensure_graph()
