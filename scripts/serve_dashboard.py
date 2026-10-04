"""Serve only dashboard assets on loopback; never expose the project directory."""
import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

PROJECT = Path(__file__).resolve().parents[1]
ALLOWED = {"/", "/index.html", "/style.css", "/app.js", "/data/dashboard.json"}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site",type=Path,default=PROJECT / "outputs/dashboard/local")
    parser.add_argument("--port",type=int,default=8765)
    options=parser.parse_args()
    directory=options.site.resolve()
    if not (directory / "index.html").is_file():
        parser.error("Generate the dashboard before starting the preview")
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,directory=str(directory),**kwargs)
        def do_GET(self):
            if unquote(urlsplit(self.path).path) not in ALLOWED:
                self.send_error(404)
                return
            super().do_GET()
        def do_HEAD(self):
            if unquote(urlsplit(self.path).path) not in ALLOWED:
                self.send_error(404)
                return
            super().do_HEAD()
        def end_headers(self):
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            super().end_headers()
    print(f"Dashboard preview: http://127.0.0.1:{options.port}/",flush=True)
    ThreadingHTTPServer(("127.0.0.1",options.port),Handler).serve_forever()


if __name__ == "__main__":
    main()
