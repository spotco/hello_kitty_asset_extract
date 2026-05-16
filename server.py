#!/usr/bin/env python3
from __future__ import annotations

import http.server
import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

PORT = 5173
ROOT = Path(__file__).parent.resolve()
INDEX_PATH = ROOT / "extracted" / "characters" / "index.json"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.send_response(302)
            self.send_header("Location", "/web_character_viewer/")
            self.end_headers()
            return
        if parsed.path == "/api/characters":
            self.send_json(200, self.read_index())
            return
        super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/extract":
            self.send_error(404)
            return

        params = urllib.parse.parse_qs(parsed.query)
        character_name = (params.get("name") or [None])[0]
        if not character_name:
            self.send_error(400, "Missing name parameter")
            return

        steps = [
            [sys.executable, "scripts/extract_character_glb.py", "--name", character_name],
        ]

        logs: list[str] = []
        for cmd in steps:
            logs.append("$ " + " ".join(cmd[1:]))
            proc = subprocess.run(
                cmd,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if proc.stdout:
                logs.extend(line for line in proc.stdout.splitlines() if line.strip())
            if proc.stderr:
                logs.extend(line for line in proc.stderr.splitlines() if line.strip())
            if proc.returncode != 0:
                self.send_json(
                    500,
                    {
                        "ok": False,
                        "returncode": proc.returncode,
                        "name": character_name,
                        "log": logs,
                    },
                )
                return

        self.send_json(
            200,
            {
                "ok": True,
                "name": character_name,
                "log": logs,
                "characters": self.read_index().get("characters", []),
            },
        )

    def read_index(self) -> dict:
        try:
            return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {"characters": []}

    def send_json(self, status: int, payload: dict):
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        try:
            path = str(args[0]).split()[1] if args else ""
            if path.startswith("/api/") or not path.startswith("/extracted/"):
                super().log_message(fmt, *args)
        except Exception:
            super().log_message(fmt, *args)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    server = Server(("", PORT), Handler)
    print(f"Serving on http://localhost:{PORT}/  -  Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
