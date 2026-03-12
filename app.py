"""
Upwork Proposal Agent — BD team web UI.
Paste job post, get proposal. Proposals are logged and agent stays up to date from docx + log.
"""
import os
import json
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, render_template, request, jsonify, redirect, url_for, session

from proposal_engine import generate_proposal, get_proposals_for_rating, save_rating, load_ratings
from config import PROPOSALS_LOG_PATH
from auth import create_user, authenticate, get_user, get_all_users, init_auth_db

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-me")

# Ensure auth tables exist (no-op if already created)
try:
    init_auth_db()
except Exception as e:
    # In production you might want to log this; for now we let app start even if DB is unavailable
    pass


@app.route("/")
def index():
    user = None
    user_id = session.get("user_id")
    if user_id:
        user = get_user(user_id)
    return render_template("index.html", user=user)


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json() or {}
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"ok": False, "error": "You must be logged in to generate a proposal.", "proposal": ""}), 401
    job_post = (data.get("job_post") or "").strip()
    user_instructions = (data.get("instructions") or "").strip()
    relevant_example = (data.get("relevant_example") or "").strip()
    if not job_post:
        return jsonify({"ok": False, "error": "Job post is required.", "proposal": ""}), 400
    result = generate_proposal(
        job_post,
        user_instructions=user_instructions,
        relevant_example_override=relevant_example or None,
        user_id=user_id,
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
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))
    proposals = get_proposals_for_rating(user_id=user_id)
    return render_template("rate.html", proposals=proposals, user=get_user(user_id))


@app.route("/rate", methods=["POST"])
def rate_submit():
    data = request.get_json() or {}
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"ok": False, "error": "You must be logged in to rate proposals."}), 401
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


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = (request.form.get("password") or "").strip()
        user_id = create_user(email, password)
        if not user_id:
            return render_template("auth.html", mode="signup", error="Email already exists or invalid.", email=email)
        session["user_id"] = user_id
        return redirect(url_for("index"))
    return render_template("auth.html", mode="signup")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = (request.form.get("password") or "").strip()
        user = authenticate(email, password)
        if not user:
            return render_template("auth.html", mode="login", error="Invalid email or password.", email=email)
        session["user_id"] = user["id"]
        return redirect(url_for("index"))
    return render_template("auth.html", mode="login")


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("login"))


@app.route("/admin")
def admin_dashboard():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))
    user = get_user(user_id)
    if not user or not user.get("is_admin"):
        return "Forbidden", 403

    proposals = []
    ratings = load_ratings()
    users = get_all_users()
    user_email_by_id = {uid: u.get("email") for uid, u in users.items()}

    if PROPOSALS_LOG_PATH.exists():
        try:
            with open(PROPOSALS_LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue
            ts = data.get("ts") or ""
            job = (data.get("job_post") or "").strip()
            proposal_text = (data.get("proposal") or "").strip()
            uid = data.get("user_id") or ""
            proposals.append({
                "ts": ts,
                "job_snippet": job[:200] + ("..." if len(job) > 200 else ""),
                "proposal_snippet": proposal_text[:250] + ("..." if len(proposal_text) > 250 else ""),
                "proposal_full": proposal_text,
                "rating": ratings.get(ts),
                "user_id": uid,
                "user_email": user_email_by_id.get(uid, None),
            })

    proposals.sort(key=lambda x: x["ts"], reverse=True)

    user_counts: dict[str, int] = {}
    for p in proposals:
        email = p["user_email"] or "Unknown"
        user_counts[email] = user_counts.get(email, 0) + 1

    return render_template("admin.html", user=user, proposals=proposals, user_counts=user_counts)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "0") == "1")
