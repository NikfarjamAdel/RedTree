"""The RedTree dashboard: a small web server on this computer.

    python dashboard/server.py              local database (the MSSQL_* settings)
    python dashboard/server.py azure        Azure SQL Database (AZURE_MSSQL_*)
    python dashboard/server.py --port 9000  another port (default 8050)
    python dashboard/server.py --no-open    don't open the browser

Serves the page from dashboard/static and the figures from /api/dashboard,
read fresh from the database on every request. Read-only.

It listens on 127.0.0.1 only, never on the network: the data includes guest
names. There is no login, so don't put it behind a public address.
"""
import argparse
import contextlib
import io
import json
import sys
import threading
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "python"))
sys.path.insert(0, str(HERE))

from dashboard_data import dashboard_payload  # noqa: E402
from engine_setup import connect_db  # noqa: E402

STATIC = HERE / "static"
HOST = "127.0.0.1"

# Explicit, because Windows' registry can map .js to text/plain, and
# browsers refuse to run a module script served as that.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".ico": "image/x-icon",
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}


def make_handler(target):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RedTreeDashboard"

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/api/dashboard":
                self.send_api()
            else:
                self.send_static("index.html" if path == "/" else path.lstrip("/"))

        def send_api(self):
            try:
                with contextlib.redirect_stdout(io.StringIO()):   # connect_db prints
                    db = connect_db(target)
                try:
                    body = json.dumps(dashboard_payload(db)).encode()
                finally:
                    db.close()
                self.reply(HTTPStatus.OK, body, "application/json", cache="no-store")
            except Exception as e:     # the page shows what went wrong and how to fix it
                traceback.print_exc()
                body = json.dumps({"error": type(e).__name__,
                                   "detail": str(e)[:400], "target": target}).encode()
                self.reply(HTTPStatus.SERVICE_UNAVAILABLE, body, "application/json",
                           cache="no-store")

        def send_static(self, relative):
            file = (STATIC / relative).resolve()
            # Only files inside static/: no path tricks, no directory listings
            if STATIC not in file.parents or not file.is_file():
                self.reply(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")
                return
            content_type = CONTENT_TYPES.get(file.suffix.lower(), "application/octet-stream")
            self.reply(HTTPStatus.OK, file.read_bytes(), content_type, cache="no-cache")

        def reply(self, status, body, content_type, cache=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if cache:
                self.send_header("Cache-Control", cache)
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            if "/api/" in self.path:     # page loads, not every stylesheet
                sys.stderr.write(f"{self.log_date_time_string()}  {fmt % args}\n")

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Run the RedTree dashboard.")
    parser.add_argument("target", nargs="?", default="local", choices=("local", "azure"))
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--no-open", action="store_true", help="don't open the browser")
    args = parser.parse_args()

    server = ThreadingHTTPServer((HOST, args.port), make_handler(args.target))
    url = f"http://{HOST}:{args.port}/"
    print(f"RedTree dashboard ({args.target} database): {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_open:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
