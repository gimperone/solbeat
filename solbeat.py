#!/usr/bin/env python3
"""SolBeat — autonomous Solana ecosystem reporter.

Pure Python stdlib. Commands:
  python solbeat.py run            # collectors scheduler + API/dashboard server (default)
  python solbeat.py collect        # one collection round, print summary, exit
  python solbeat.py report         # regenerate reports/report.md + report.json, exit
  python solbeat.py serve --port N # serve dashboard + API without background collection
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from core.anomaly import evaluate  # noqa: E402
from core.report import build_payload, generate_json, generate_markdown  # noqa: E402
from core.store import Store  # noqa: E402
from core.util import utcnow  # noqa: E402

CFG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "data", "solbeat.db")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
DASHBOARD = os.path.join(BASE_DIR, "dashboard", "index.html")


def load_config() -> dict:
    with open(CFG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# Collector orchestration
# --------------------------------------------------------------------------
class Orchestrator:
    def __init__(self, cfg: dict, store: Store):
        self.cfg = cfg
        self.store = store
        self.state: dict[str, dict] = {}   # source -> partial snapshot
        self.last_run: dict[str, float] = {}
        self.lock = threading.Lock()
        self._stop = threading.Event()
        from collectors import news as news_mod
        from collectors import market, rpc_collector
        self.sources = {
            "rpc": (rpc_collector.collect, "rpc"),
            "daa": (rpc_collector.collect_daa, "daa"),
            "defillama": (market.collect_defillama, "defillama"),
            "coingecko": (market.collect_coingecko, "coingecko"),
            "news": (news_mod.attach_store(store), "news"),
        }

    def _merge_and_persist(self):
        metrics: dict = {}
        validators: list = []
        derived: dict = {}
        for partial in self.state.values():
            metrics.update(partial.get("metrics", {}))
            validators = partial.get("validators", validators)
            derived.update(partial.get("derived", {}))
        snapshot = {"ts": utcnow(), "metrics": metrics, "validators": validators, "derived": derived}
        if metrics:
            self.store.record_metrics(metrics)
            self.store.save_snapshot(snapshot)
            alerts = evaluate(snapshot, self.store, self.cfg)
            self.store.save_alerts(alerts)

    def run_source(self, name: str):
        fn, _ = self.sources[name]
        try:
            res = fn(self.cfg)
            items = len(res.get("items", []))
            with self.lock:
                if name == "news":
                    pass  # news already persisted inside wrapper
                else:
                    self.state[name] = res
                self.last_run[name] = time.time()
            self.store.source_ok(name, max(items, len(res.get("metrics", {}))))
        except Exception as exc:
            self.store.source_fail(name, str(exc))

    def round_once(self):
        threads = []
        for name in self.sources:
            t = threading.Thread(target=self.run_source, args=(name,), daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=120)
        self._merge_and_persist()

    def loop(self):
        intervals = self.cfg["intervals_sec"]
        while not self._stop.is_set():
            now = time.time()
            due = [n for n, (_, key) in self.sources.items()
                   if now - self.last_run.get(n, 0) >= intervals.get(key, 300)]
            if due:
                self.round_once()
                write_reports(self.cfg, self.store)
            self._stop.wait(timeout=10)

    def stop(self):
        self._stop.set()


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------
def write_reports(cfg: dict, store: Store) -> dict:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    payload = build_payload(store.latest_snapshot(), store, cfg)
    md_path = os.path.join(REPORTS_DIR, "report.md")
    json_path = os.path.join(REPORTS_DIR, "report.json")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(generate_markdown(payload))
    with open(json_path, "w", encoding="utf-8") as fh:
        fh.write(generate_json(payload))
    return payload


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    orchestrator: Orchestrator = None  # injected
    cfg: dict = None

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            if route == "/" or route == "/index.html":
                with open(DASHBOARD, "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")

            if route.endswith(".html"):
                # serve any dashboard page; basename prevents path traversal
                safe = os.path.basename(route)
                page = os.path.join(os.path.dirname(DASHBOARD), safe)
                if os.path.isfile(page):
                    with open(page, "rb") as fh:
                        return self._send(200, fh.read(), "text/html; charset=utf-8")
                return self._send(404, b'{"error":"page not found"}', "application/json")

            if route == "/healthz":
                return self._send(200, b'{"ok":true}', "application/json")

            orch = self.orchestrator
            if route == "/api/report":
                payload = build_payload(orch.store.latest_snapshot(), orch.store, self.cfg)
                return self._send(200, json.dumps(payload, default=str).encode(), "application/json")

            if route == "/api/history":
                qs = parse_qs(parsed.query)
                key = (qs.get("key") or ["tps_total"])[0]
                points = int((qs.get("points") or [300])[0])
                hist = orch.store.history(key, limit=2000)
                if len(hist) > points:
                    step = len(hist) / points
                    hist = [hist[int(i * step)] for i in range(points)]
                return self._send(200, json.dumps(
                    {"key": key, "points": [[t * 1000, v] for t, v in hist]}).encode(),
                    "application/json")

            return self._send(404, b'{"error":"not found"}', "application/json")
        except BrokenPipeError:
            return
        except Exception as exc:
            try:
                return self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
            except Exception:
                return


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(prog="solbeat")
    ap.add_argument("command", nargs="?", default="run",
                    choices=["run", "collect", "report", "serve"])
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    cfg = load_config()
    store = Store(DB_PATH)
    orch = Orchestrator(cfg, store)

    if args.command == "collect":
        orch.round_once()
        snap = store.latest_snapshot() or {}
        m = snap.get("metrics", {})
        print(json.dumps({k: m[k] for k in sorted(m)[:12]}, indent=2))
        store.close()
        return

    if args.command == "report":
        payload = write_reports(cfg, store)
        print(f"reports written · alerts={len(payload['alerts'])} news={len(payload['news'])}")
        store.close()
        return

    port = args.port or int(cfg.get("port", 7801))
    Handler.orchestrator = orch
    Handler.cfg = cfg

    if args.command in ("run",):
        threading.Thread(target=orch.loop, daemon=True).start()
        orch.round_once()  # first round synchronously so UI has data immediately

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[solbeat] serving on http://localhost:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        orch.stop()
    finally:
        store.close()


if __name__ == "__main__":
    main()
