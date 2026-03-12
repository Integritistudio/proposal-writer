"""
Upwork Proposal Agent — BD team web UI.
Paste job post, get proposal. Proposals are logged and agent stays up to date from docx + log.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, render_template, request, jsonify

from proposal_engine import generate_proposal, get_proposals_for_rating, save_rating

app = Flask(__name__, template_folder="templates", static_folder="static")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json() or {}
    job_post = (data.get("job_post") or "").strip()
    user_instructions = (data.get("instructions") or "").strip()
    relevant_example = (data.get("relevant_example") or "").strip()
    if not job_post:
        return jsonify({"ok": False, "error": "Job post is required.", "proposal": ""}), 400
    result = generate_proposal(
        job_post,
        user_instructions=user_instructions,
        relevant_example_override=relevant_example or None,
    )
    if result.get("error"):
        return jsonify({
            "ok": False,
            "error": result["error"],
            "proposal": result.get("proposal") or "",
            "relevant_example": "",
        }), 200
    return jsonify({
        "ok": True,
        "proposal": result["proposal"],
        "relevant_example": result.get("relevant_example") or "",
        "tech_stacks": result.get("tech_stacks", {}),
        "error": None,
    })


@app.route("/rate")
def rate_page():
    proposals = get_proposals_for_rating()
    return render_template("rate.html", proposals=proposals)


@app.route("/rate", methods=["POST"])
def rate_submit():
    data = request.get_json() or {}
    ts = (data.get("ts") or "").strip()
    try:
        rating = int(data.get("rating"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid rating"}), 400
    if not ts:
        return jsonify({"ok": False, "error": "Missing ts"}), 400
    if rating not in (3, 4, 6, 7, 10):
        return jsonify({"ok": False, "error": "Rating must be 3, 4, 6, 7, or 10"}), 400
    save_rating(ts, rating)
    return jsonify({"ok": True})


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "0") == "1")
