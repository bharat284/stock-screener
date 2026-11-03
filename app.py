"""
Local app server for the screener dashboard.

    .venv\\Scripts\\python app.py        -> http://127.0.0.1:8765

Serves output/ and adds live endpoints the static (shared) copy doesn't have:
  GET  /api/ping                 -> {"ok": true}
  GET  /api/search?q=tata        -> screener.in company search
  GET  /api/analyze?symbol=TMPV  -> full analysis of any listed company, on demand
  POST /api/refresh              -> re-run the whole screen in the background
  GET  /api/status               -> refresh progress
  GET/POST /api/watchlist        -> watchlist.json (included in every daily run)
"""
import datetime as dt
import json
import re
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import screener

HOST, PORT = "127.0.0.1", 8765
WATCHLIST = screener.ROOT / "watchlist.json"
state = {"running": False, "done": 0, "total": 0, "current": "", "error": None, "finished": None}
lock = threading.Lock()
analysis_cache = {}


def new_day():
    screener.TODAY = dt.date.today()


def run_refresh():
    def progress(i, n, sym):
        state.update(done=i - 1, total=n, current=sym)
    try:
        new_day()
        screener.build(refresh=True, progress=progress)
        analysis_cache.clear()
        state.update(error=None, finished=dt.datetime.now().strftime("%H:%M"))
    except Exception as ex:
        state["error"] = str(ex)
    finally:
        state["running"] = False


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, obj, code=200):
        body = screener.to_json(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/ping":
            return self.send_json({"ok": True})
        if u.path == "/api/status":
            return self.send_json(state)
        if u.path == "/api/watchlist":
            return self.send_json(json.loads(WATCHLIST.read_text()) if WATCHLIST.exists() else [])
        if u.path == "/api/search":
            term = q.get("q", "").strip()
            if len(term) < 2:
                return self.send_json([])
            r = screener.requests.get(f"{screener.BASE}/api/company/search/?q={quote(term)}&v=3",
                                      headers=screener.HEADERS, timeout=15)
            out = []
            for item in r.json():
                m = re.match(r"/company/([^/]+)/", item.get("url") or "")
                if m and item.get("id"):
                    out.append({"symbol": m.group(1), "name": item["name"]})
            return self.send_json(out)
        if u.path == "/api/analyze":
            sym = q.get("symbol", "").strip().upper()
            if not sym:
                return self.send_json({"error": "symbol required"}, 400)
            if sym not in analysis_cache:
                new_day()
                analysis_cache[sym] = screener.analyse_one(sym)
            r = analysis_cache[sym]
            return self.send_json(r if r else {"error": f"No data for {sym}"}, 200 if r else 404)
        if u.path in ("/", ""):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/refresh":
            with lock:
                if not state["running"]:
                    state.update(running=True, done=0, total=0, current="", error=None)
                    threading.Thread(target=run_refresh, daemon=True).start()
            return self.send_json(state)
        if u.path == "/api/watchlist":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                syms = [str(s).upper() for s in json.loads(self.rfile.read(n) or b"[]")][:200]
            except ValueError:
                return self.send_json({"error": "bad json"}, 400)
            WATCHLIST.write_text(json.dumps(syms))
            return self.send_json(syms)
        self.send_error(404)


def main():
    if not (screener.OUT_DIR / "index.html").exists():
        print("No dashboard yet - running the screener first...")
        screener.build()
    server = ThreadingHTTPServer((HOST, PORT), partial(Handler, directory=str(screener.OUT_DIR)))
    url = f"http://{HOST}:{PORT}/"
    print(f"Screener app running at {url}  (Ctrl+C to stop)")
    webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
