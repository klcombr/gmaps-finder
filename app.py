"""Flask web app — Google Maps Business Finder."""

import csv
import io
import json
import logging
import os

from flask import Flask, jsonify, render_template, request, Response
from tasks import TaskManager

# ── App setup ──────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
manager: TaskManager | None = None


def _get_manager() -> TaskManager:
    global manager
    if manager is None:
        key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
        if not key:
            raise RuntimeError("GOOGLE_MAPS_API_KEY not set")
        os.makedirs("data", exist_ok=True)
        manager = TaskManager(key, "data/dedup.db")
    return manager


# ── Routes ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json(silent=True) or {}
    queries_raw = data.get("queries", "").strip()
    limit = min(int(data.get("limit", 20)), 60)
    enrich = bool(data.get("enrich", False))
    recheck = int(data.get("recheck_days", 0))

    if not queries_raw:
        return jsonify({"error": "queries required"}), 400

    queries = [q.strip() for q in queries_raw.splitlines() if q.strip()]
    if not queries:
        return jsonify({"error": "no valid queries"}), 400

    try:
        mgr = _get_manager()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    task = mgr.create(queries, limit=limit, enrich=enrich, recheck_days=recheck)
    return jsonify({"task_id": task.id, "status": "started"})


@app.route("/api/task/<task_id>")
def api_task(task_id: str):
    mgr = _get_manager()
    task = mgr.get(task_id)
    if not task:
        return jsonify({"error": "not found"}), 404
    return jsonify(task.to_dict())


@app.route("/api/task/<task_id>/leads")
def api_leads(task_id: str):
    mgr = _get_manager()
    task = mgr.get(task_id)
    if not task:
        return jsonify({"error": "not found"}), 404
    site_filter = request.args.get("site_filter", "")
    leads = task.leads
    if site_filter == "no_site":
        leads = [l for l in leads if l.get("has_website") == "false"]
    elif site_filter == "with_site":
        leads = [l for l in leads if l.get("has_website") == "true"]
    return jsonify({"leads": leads, "total": len(leads)})


@app.route("/api/task/<task_id>/cancel", methods=["POST"])
def api_cancel(task_id: str):
    mgr = _get_manager()
    ok = mgr.cancel(task_id)
    return jsonify({"ok": ok})


@app.route("/api/task/<task_id>/export/<fmt>")
def api_export(task_id: str, fmt: str):
    mgr = _get_manager()
    task = mgr.get(task_id)
    if not task:
        return jsonify({"error": "not found"}), 404

    leads = task.leads
    if fmt == "jsonl":
        lines = [json.dumps(l, ensure_ascii=False) for l in leads]
        return Response(
            "\n".join(lines),
            mimetype="application/jsonl",
            headers={"Content-Disposition": f"attachment; filename=leads_{task_id}.jsonl"},
        )
    # CSV
    if not leads:
        return Response("", mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=leads_{task_id}.csv"})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=leads[0].keys())
    w.writeheader()
    w.writerows(leads)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=leads_{task_id}.csv"},
    )


@app.route("/api/stats")
def api_stats():
    mgr = _get_manager()
    return jsonify(mgr.db.stats())


# ── Main ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
