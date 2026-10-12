#!/usr/bin/env python3
"""
server.py
Zero-dependency HTTP server:
- Serves index.html and catalogue.json
- Reads and updates kanban_state.json via /api/state
"""

from http.server import HTTPServer, SimpleHTTPRequestHandler
import json
import os

PORT = 8000
STATE_FILE = "kanban_state.json"
CATALOGUE_FILE = "catalogue.json"

DEFAULT_STATE = {
    "selectedMajor": "CG51087486",  # Artificial Intelligence
    "selectedMinor": "CG50753244",  # Mathematics
    "semesterCount": 4,
    "placements": {},
    "customModules": []
}


class PlannerHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/state":
            if not os.path.exists(STATE_FILE):
                with open(STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump(DEFAULT_STATE, f, indent=2)

            with open(STATE_FILE, "r", encoding="utf-8") as f:
                content = f.read()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))
            return

        super().do_GET()

    def do_POST(self):
        if self.path == "/api/save_state":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            try:
                state_data = json.loads(post_data.decode("utf-8"))
                with open(STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump(state_data, f, indent=2, ensure_ascii=False)

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def end_headers(self):
        if self.path.endswith(".json"):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()


if __name__ == "__main__":
    if not os.path.exists(STATE_FILE):
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_STATE, f, indent=2)

    server = HTTPServer(("localhost", PORT), PlannerHandler)
    print(f"UZH Master Planner running at: http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")