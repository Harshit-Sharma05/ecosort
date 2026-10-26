import hmac
import os
import random
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from config import Config
from db import close_db, get_db, init_db
from quiz_data import BANK
from waste_finder import clf, find_waste_item, guided_questions, resolve_guided


app = Flask(__name__)
app.config.from_object(Config)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Login cookies are only sent over HTTPS. Running `python app.py` on your
    # own computer switches this off (see the bottom of this file).
    SESSION_COOKIE_SECURE=True,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


# --------------------------------------------------
# Secret key (used to sign login cookies)
# --------------------------------------------------

def load_secret_key():
    """Use the key from config.py if set, else create and keep one in data/.secret_key."""
    key_file = Path(__file__).parent / "data" / ".secret_key"
    try:
        return key_file.read_text().strip()
    except FileNotFoundError:
        key = secrets.token_hex(32)
        key_file.parent.mkdir(exist_ok=True)
        key_file.write_text(key)
        return key


if not app.config.get("SECRET_KEY"):
    app.config["SECRET_KEY"] = load_secret_key()

init_db()
app.teardown_appcontext(close_db)


# --------------------------------------------------
# Category colours (shared by badges and dashboard chart)
# --------------------------------------------------

PALETTE = {
    "Wet waste": "#2f8f5b",
    "Dry waste": "#2f6fb5",
    "Sanitary waste": "#c98a1b",
    "Special care waste": "#c0463a",
    "E-waste": "#7a5bb5",
}
CATEGORY_COLORS = {
    m["category"]: PALETTE.get(m["category"], "#8a8f8c")
    for m in clf.materials.values()
}


# --------------------------------------------------
# Users, sessions and CSRF protection
# --------------------------------------------------

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def load_user_and_check_csrf():
    if request.endpoint == "static":
        return

    g.user = None
    user_id = session.get("user_id")
    if user_id:
        row = get_db().execute(
            "SELECT id, name, email FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row:
            g.user = row
        else:
            session.pop("user_id", None)

    if request.method == "POST":
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
        expected = session.get("_csrf", "")
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400, "Your form expired. Please go back, reload the page and try again.")


@app.context_processor
def inject_globals():
    return {"current_user": g.get("user"), "category_colors": CATEGORY_COLORS}


@app.template_filter("pretty_date")
def pretty_date(value):
    try:
        return datetime.fromisoformat(value).strftime("%d %b, %I:%M %p").lstrip("0")
    except (TypeError, ValueError):
        return value or ""


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Please log in to see your dashboard.", "info")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def safe_next(target):
    """Only allow redirects to a path on this site (never to another website)."""
    if (target
            and target.startswith("/")
            and not target.startswith("//")
            and "\\" not in target
            and not any(ord(ch) < 32 for ch in target)):
        return target
    return url_for("dashboard")


def now():
    return datetime.now().isoformat(timespec="seconds")


def record_activity(result, action):
    """Save a found result to the logged-in user's history."""
    if g.user is None or not result or result.get("status") != "found":
        return
    db = get_db()
    db.execute(
        "INSERT INTO activity (user_id, item_name, category, material, bin, action, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (g.user["id"], result["item_name"][:120], result["category"],
         result.get("material"), result.get("bin"), action, now()),
    )
    db.commit()


# --------------------------------------------------
# Home / Waste Finder
# --------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def home():
    result = None

    if request.method == "POST":
        item_name = request.form.get("item", "")
        result = find_waste_item(item_name)
        record_activity(result, "searched")

    return render_template("index.html", result=result)


@app.route("/guided", methods=["GET", "POST"])
def guided():
    """User answered the 'what is it made of?' questions."""
    if request.method == "GET":
        return redirect(url_for("home"))

    result = resolve_guided(
        request.form.get("item", ""),
        request.form.get("material"),
        request.form.get("special"),
    )
    record_activity(result, "searched")
    return render_template("index.html", result=result)


@app.route("/refine", methods=["GET", "POST"])
def refine():
    """User clicked 'Not right?' on a guessed result."""
    if request.method == "GET":
        return redirect(url_for("home"))

    result = guided_questions(request.form.get("item", ""))
    return render_template("index.html", result=result)


@app.route("/log", methods=["POST"])
def log_activity():
    """'Log as disposed' button on a result card (called with fetch)."""
    if g.user is None:
        return jsonify(ok=False, error="login_required"), 401

    data = request.get_json(silent=True) or {}
    item = str(data.get("item_name", "")).strip()[:120]
    category = str(data.get("category", ""))

    if not item or category not in CATEGORY_COLORS:
        return jsonify(ok=False, error="invalid"), 400

    db = get_db()
    db.execute(
        "INSERT INTO activity (user_id, item_name, category, material, bin, action, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'disposed', ?)",
        (g.user["id"], item, category,
         str(data.get("material", ""))[:60], str(data.get("bin", ""))[:60], now()),
    )
    db.commit()
    return jsonify(ok=True)


# --------------------------------------------------
# Register / Login / Logout
# --------------------------------------------------

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("dashboard"))

    errors, form = [], {"name": "", "email": ""}

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        form = {"name": name, "email": email}

        if not 1 <= len(name) <= 60:
            errors.append("Enter your name (up to 60 characters).")
        if not EMAIL_PATTERN.match(email) or len(email) > 120:
            errors.append("Enter a valid email address.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("The two passwords do not match.")

        if not errors:
            db = get_db()
            try:
                cursor = db.execute(
                    "INSERT INTO users (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (name, email, generate_password_hash(password), now()),
                )
                db.commit()
            except sqlite3.IntegrityError:
                errors.append("An account with this email already exists. Try logging in.")
            else:
                session.clear()
                session["user_id"] = cursor.lastrowid
                session.permanent = True
                flash(f"Welcome to EcoSort, {name}!", "success")
                return redirect(url_for("dashboard"))

    return render_template("register.html", errors=errors, form=form)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))

    error, email = None, ""

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        row = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

        if row and check_password_hash(row["password_hash"], password):
            session.clear()
            session["user_id"] = row["id"]
            session.permanent = True
            flash(f"Welcome back, {row['name']}!", "success")
            return redirect(safe_next(request.args.get("next")))

        error = "Incorrect email or password."

    return render_template("login.html", error=error, email=email)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("home"))


# --------------------------------------------------
# Dashboard
# --------------------------------------------------

def day_streak(db, user_id):
    rows = db.execute(
        "SELECT substr(created_at, 1, 10) AS d FROM activity WHERE user_id = ? "
        "UNION SELECT substr(created_at, 1, 10) FROM quiz_results WHERE user_id = ?",
        (user_id, user_id),
    ).fetchall()
    days = {row["d"] for row in rows}

    today = date.today()
    cursor = today if today.isoformat() in days else today - timedelta(days=1)
    streak = 0
    while cursor.isoformat() in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    uid = g.user["id"]

    def count(action):
        return db.execute(
            "SELECT COUNT(*) FROM activity WHERE user_id = ? AND action = ?", (uid, action)
        ).fetchone()[0]

    rows = db.execute(
        "SELECT category, COUNT(*) AS n FROM activity "
        "WHERE user_id = ? AND action = 'disposed' GROUP BY category ORDER BY n DESC",
        (uid,),
    ).fetchall()
    top = max([r["n"] for r in rows], default=1)
    bars = [{"category": r["category"], "count": r["n"], "pct": round(100 * r["n"] / top)} for r in rows]

    recent = db.execute(
        "SELECT item_name, category, bin, action, created_at FROM activity "
        "WHERE user_id = ? ORDER BY id DESC LIMIT 12",
        (uid,),
    ).fetchall()

    quizzes = db.execute(
        "SELECT score, total, created_at FROM quiz_results WHERE user_id = ? ORDER BY id DESC LIMIT 5",
        (uid,),
    ).fetchall()
    best = db.execute(
        "SELECT MAX(score * 100.0 / total) FROM quiz_results WHERE user_id = ?", (uid,)
    ).fetchone()[0]

    return render_template(
        "dashboard.html",
        searches=count("searched"),
        logged=count("disposed"),
        streak=day_streak(db, uid),
        best_quiz=None if best is None else round(best),
        bars=bars,
        recent=recent,
        quizzes=quizzes,
    )


@app.route("/dashboard/clear", methods=["POST"])
@login_required
def clear_history():
    db = get_db()
    db.execute("DELETE FROM activity WHERE user_id = ?", (g.user["id"],))
    db.commit()
    flash("Your activity history has been cleared.", "info")
    return redirect(url_for("dashboard"))


# --------------------------------------------------
# Quiz (anyone can play, scores are saved for logged-in users)
# --------------------------------------------------

QUESTIONS_PER_QUIZ = 10


@app.route("/quiz", methods=["GET", "POST"])
def quiz():
    by_id = {q["id"]: q for q in BANK}

    if request.method == "POST":
        ids = []
        for part in request.form.get("qids", "").split(","):
            if part.isdigit() and int(part) in by_id and int(part) not in ids:
                ids.append(int(part))
        ids = ids[:QUESTIONS_PER_QUIZ]
        if not ids:
            return redirect(url_for("quiz"))

        review, score = [], 0
        for qid in ids:
            q = by_id[qid]
            picked = request.form.get(f"q{qid}", "")
            picked = int(picked) if picked.isdigit() and int(picked) < len(q["options"]) else None
            correct = picked == q["answer"]
            score += correct
            review.append({"q": q, "picked": picked, "correct": correct})

        saved = False
        if g.user:
            db = get_db()
            db.execute(
                "INSERT INTO quiz_results (user_id, score, total, created_at) VALUES (?, ?, ?, ?)",
                (g.user["id"], score, len(ids), now()),
            )
            db.commit()
            saved = True

        return render_template("quiz.html", mode="result", review=review,
                               score=score, total=len(ids), saved=saved)

    questions = random.sample(BANK, QUESTIONS_PER_QUIZ)
    return render_template("quiz.html", mode="play", questions=questions,
                           qids=",".join(str(q["id"]) for q in questions))


if __name__ == "__main__":
    # For running on your own computer only. Hosting services start the app
    # through their own server and never run this block.
    # Debug mode is off unless you ask for it:  set FLASK_DEBUG=1
    app.config["SESSION_COOKIE_SECURE"] = False  # plain http://127.0.0.1 has no HTTPS
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")