"""Local web UI for keyfinder. Run: python3 keyfinder.py serve"""
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from keyfinder import Researcher, make_store

UI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, content_type):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            with open(UI_PATH, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if url.path == "/api/research":
            return self._research(urllib.parse.parse_qs(url.query))
        self._send(404, "not found", "text/plain")

    def _research(self, q):
        def arg(name, default):
            return q.get(name, [default])[0]

        try:
            store = make_store(arg("store", "appstore"), arg("country", "us").lower(), arg("lang", "en"), 0.3)
            researcher = Researcher(
                store,
                probe=arg("probe", "1") == "1",
                difficulty=arg("difficulty", "1") == "1",
                limit=max(5, min(int(arg("limit", "40")), 150)),
                log=lambda m: print(m),
            )
            rows = [k.as_dict() for k in researcher.research(arg("seed", ""))]
            body = {"keywords": rows, "requests": store.fetcher.requests,
                    "difficulty": researcher.difficulty}
            self._send(200, json.dumps(body), "application/json")
        except Exception as exc:
            self._send(400, json.dumps({"error": str(exc)}), "application/json")

    def log_message(self, fmt, *args):
        pass


def serve(host="127.0.0.1", port=8000):
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Keyword finder running at http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
