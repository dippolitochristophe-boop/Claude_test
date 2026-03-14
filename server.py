"""
Serveur local — Job Scraper

Lance : python server.py
Ouvre : http://127.0.0.1:5000

Endpoints :
  GET  /                          → sert static/index.html
  GET  /api/profile               → charge profile.json
  POST /api/profile               → sauvegarde profile.json
  POST /api/run/scraper           → lance job_scrapper.py en subprocess
  POST /api/run/healthcheck       → lance agent_validator.py en subprocess
  POST /api/run/pipeline          → lance agents/pipeline.py en subprocess
  GET  /api/run/<run_id>/stream   → SSE stdout en temps réel
  GET  /api/run/<run_id>/status   → {"status": "running|done|error", "exit_code": ...}
  GET  /api/reports               → liste des rapport_*.html
"""

import json
import os
import subprocess
import sys
import time

# Fix Unicode output on Windows (cp1252 → utf-8)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
import webbrowser
from datetime import datetime
from glob import glob
from threading import Thread

from flask import Flask, Response, jsonify, request, send_from_directory

from profiles import load_profile, save_profile

app = Flask(__name__, static_folder="static")

# ── Store en mémoire pour les runs actifs ─────────────────────────────────────
# { run_id: {"proc": Popen, "lines": [str], "status": "running|done|error", "exit_code": int} }
RUNS: dict = {}


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/reports/<path:filename>")
def serve_report(filename):
    return send_from_directory(".", filename)


# ── Profile API ───────────────────────────────────────────────────────────────

@app.route("/api/profile", methods=["GET"])
def get_profile():
    return jsonify(load_profile())


@app.route("/api/profile", methods=["POST"])
def post_profile():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "invalid JSON"}), 400
    save_profile(data)
    return jsonify({"ok": True, "saved_at": data.get("updated_at", "")})


# ── Run API ───────────────────────────────────────────────────────────────────

def _start_run(cmd: list, env_extra: dict = None) -> str:
    """Lance un subprocess, retourne le run_id."""
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if env_extra:
        env.update(env_extra)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        env=env,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    RUNS[run_id] = {"proc": proc, "lines": [], "status": "running", "exit_code": None}

    # Thread qui lit stdout et accumule les lignes
    def _reader():
        for line in proc.stdout:
            RUNS[run_id]["lines"].append(line)
        proc.wait()
        RUNS[run_id]["status"] = "done" if proc.returncode == 0 else "error"
        RUNS[run_id]["exit_code"] = proc.returncode

    Thread(target=_reader, daemon=True).start()
    return run_id


@app.route("/api/run/scraper", methods=["POST"])
def run_scraper():
    profile = load_profile()
    cmd = [sys.executable, "job_scrapper.py"]
    opts = request.get_json(force=True) or {}
    if opts.get("new_only"):
        cmd.append("--new-only")
    if opts.get("no_html"):
        cmd.append("--no-html")
    run_id = _start_run(cmd)
    return jsonify({"run_id": run_id})


@app.route("/api/run/healthcheck", methods=["POST"])
def run_healthcheck():
    cmd = [sys.executable, "agent_validator.py"]
    run_id = _start_run(cmd)
    return jsonify({"run_id": run_id})


@app.route("/api/run/pipeline", methods=["POST"])
def run_pipeline():
    cmd = [sys.executable, "-m", "agents.pipeline"]
    run_id = _start_run(cmd)
    return jsonify({"run_id": run_id})


# ── SSE stream ────────────────────────────────────────────────────────────────

@app.route("/api/run/<run_id>/stream")
def stream(run_id):
    if run_id not in RUNS:
        return jsonify({"error": "run not found"}), 404

    def generate():
        sent = 0
        while True:
            run = RUNS[run_id]
            lines = run["lines"]
            while sent < len(lines):
                line = lines[sent].rstrip("\n")
                yield f"data: {json.dumps(line)}\n\n"
                sent += 1
            if run["status"] != "running":
                yield f"data: {json.dumps('__DONE__')}\n\n"
                break
            time.sleep(0.15)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/run/<run_id>/status")
def run_status(run_id):
    if run_id not in RUNS:
        return jsonify({"status": "not_found"}), 404
    r = RUNS[run_id]
    return jsonify({"status": r["status"], "exit_code": r["exit_code"],
                    "lines": len(r["lines"])})


# ── Reports list ──────────────────────────────────────────────────────────────

@app.route("/api/reports")
def list_reports():
    base = os.path.dirname(os.path.abspath(__file__))
    files = sorted(glob(os.path.join(base, "rapport_*.html")), reverse=True)
    reports = []
    for f in files:
        name = os.path.basename(f)
        # Extraire date depuis rapport_YYYYMMDD_HHMM.html
        try:
            ts = name.replace("rapport_", "").replace(".html", "")
            dt = datetime.strptime(ts, "%Y%m%d_%H%M")
            label = dt.strftime("%d %b %Y — %H:%M")
        except ValueError:
            label = name
        reports.append({"file": name, "label": label, "url": f"/reports/{name}"})
    return jsonify(reports)


# ── Démarrage ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    url = "http://127.0.0.1:5000"
    print(f"🚀 Job Scraper — {url}")
    # Ouvrir le navigateur après un court délai (laisser Flask démarrer)
    Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False)
