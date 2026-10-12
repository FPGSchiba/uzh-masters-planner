#!/usr/bin/env python3
"""
server.py
Zero-dependency Multi-Threaded HTTP server:
- Serves index.html and catalogue.json concurrently
- Reads and updates kanban_state.json via /api/state
- Triggers fetch_full_catalogue.py with real-time SSE progress streaming via /api/refresh_catalogue
- Multi-threaded: background crawler never blocks page requests or reloads
- Automatically cleans up lingering socket occupants on startup
"""

from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import json
import os
import subprocess
import sys
import threading
import time

PORT = 8000
STATE_FILE = "kanban_state.json"
CATALOGUE_FILE = "catalogue.json"

DEFAULT_STATE = {
    "selectedMajor": "CG51087486",  # Artificial Intelligence
    "selectedMinor": "CG50753244",  # Mathematics
    "semesterCount": 4,
    "placements": {},
    "customModules": [],
    "starredModules": [],
    "categoryOverrides": {}
}

# Global crawler execution lock to prevent overlapping runs
CRAWLER_LOCK = threading.Lock()
IS_CRAWLING = False


class ReusableThreadingServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class PlannerHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        # Clean logging
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {args[0]} - {args[1]}\n")

    def do_GET(self):
        global IS_CRAWLING

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

        elif self.path == "/api/refresh_catalogue":
            if not CRAWLER_LOCK.acquire(blocking=False):
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Crawler is already running"}).encode("utf-8"))
                return

            IS_CRAWLING = True
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

                def send_event(event_type: str, data: dict):
                    payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
                    try:
                        self.wfile.write(payload.encode("utf-8"))
                        self.wfile.flush()
                    except Exception:
                        pass

                send_event("log", {"message": "Starting VVZ catalogue update...", "percent": 5, "stage": "init"})

                script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fetch_full_catalogue.py")
                proc = subprocess.Popen(
                    [sys.executable, "-u", script_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )

                percent = 10
                current_stage = "Discovering modules"

                for line in proc.stdout:
                    line_str = line.strip()
                    if not line_str:
                        continue

                    if "Discovering academic semesters" in line_str:
                        percent = 12
                        current_stage = "Resolving semesters"
                    elif "Loaded" in line_str and "Majors" in line_str:
                        percent = 20
                        current_stage = "Programs loaded"
                    elif "--- Crawling HS" in line_str:
                        percent = 30
                        current_stage = "Crawling Autumn (HS)"
                    elif "--- Crawling FS" in line_str:
                        percent = 50
                        current_stage = "Crawling Spring (FS)"
                    elif "Concurrently fetching schedule details" in line_str:
                        percent = 65
                        current_stage = "Fetching rooms & timetable"
                    elif "Progress:" in line_str and "/" in line_str:
                        try:
                            parts = line_str.split("Progress:")[1].split("schedules")[0].strip().split("/")
                            cur, tot = int(parts[0]), int(parts[1])
                            ratio = cur / tot
                            percent = int(65 + (ratio * 25))
                            current_stage = f"Fetching schedules ({cur}/{tot})"
                        except Exception:
                            pass
                    elif "Crawl Verification Summary" in line_str:
                        percent = 95
                        current_stage = "Verifying catalogue"
                    elif "Successfully saved catalogue.json" in line_str:
                        percent = 100
                        current_stage = "Complete"

                    send_event("log", {
                        "message": line_str,
                        "percent": percent,
                        "stage": current_stage
                    })

                proc.wait()

                if proc.returncode == 0:
                    send_event("done", {
                        "success": True,
                        "message": "Catalogue successfully updated!",
                        "percent": 100,
                        "stage": "Finished"
                    })
                else:
                    send_event("error", {
                        "success": False,
                        "message": f"Crawler failed with exit code {proc.returncode}",
                        "percent": percent,
                        "stage": "Error"
                    })

            except Exception as e:
                try:
                    send_event("error", {"success": False, "message": str(e), "percent": 0, "stage": "Error"})
                except Exception:
                    pass
            finally:
                IS_CRAWLING = False
                CRAWLER_LOCK.release()
            return

        super().do_GET()

    def do_POST(self):
        if self.path == "/api/save_state":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)

            try:
                new_state = json.loads(post_data.decode("utf-8"))
                with open(STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump(new_state, f, indent=2)

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        super().do_POST()


def kill_port_occupant(port: int):
    """Automatically frees the port if an old zombie/background server instance is lingering."""
    try:
        current_pid = os.getpid()
        out = subprocess.check_output(["lsof", "-ti", f":{port}"], text=True, stderr=subprocess.DEVNULL)
        pids = [int(p.strip()) for p in out.strip().split() if p.strip()]
        for p in pids:
            if p != current_pid:
                try:
                    os.kill(p, 9)
                    time.sleep(0.1)
                except OSError:
                    pass
    except Exception:
        pass


def run():
    server_address = ("", PORT)
    try:
        httpd = ReusableThreadingServer(server_address, PlannerHandler)
    except OSError as e:
        if e.errno == 48:
            kill_port_occupant(PORT)
            try:
                httpd = ReusableThreadingServer(server_address, PlannerHandler)
            except OSError:
                print(f"Error: Port {PORT} is already in use by another process.")
                print(f"You can find and stop it with: lsof -ti :{PORT} | xargs kill -9")
                return
        else:
            raise e

    print(f"UZH Study Planner running at http://localhost:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run()
