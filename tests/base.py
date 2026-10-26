"""Shared test setup: every test run uses a throw-away database and log file,
so your real data/ecosort.db and data/unknown_queries.txt are never touched."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Must be set before app.py is imported.
os.environ["SECRET_KEY"] = "test-secret-key"
_TMP = tempfile.mkdtemp(prefix="ecosort-tests-")

import db  # noqa: E402

db.DB_PATH = Path(_TMP) / "test.db"

import app as app_module  # noqa: E402

app_module.clf.unknown_log = str(Path(_TMP) / "unknown.txt")
app_module.app.config["TESTING"] = True
app_module.app.config["SESSION_COOKIE_SECURE"] = False   # tests use plain http


class AppTestCase(unittest.TestCase):
    """Gives each test a fresh client, an empty database and a CSRF helper."""

    def setUp(self):
        for table in ("activity", "quiz_results", "users"):
            conn = db.sqlite3.connect(db.DB_PATH)
            conn.execute(f"DELETE FROM {table}")
            conn.commit()
            conn.close()
        self.client = app_module.app.test_client()

    def token(self):
        """Load a page so the session has a CSRF token, then return it."""
        self.client.get("/")
        with self.client.session_transaction() as s:
            return s["_csrf"]

    def register(self, email="test@example.com", password="password123", name="Test"):
        return self.client.post("/register", data={
            "csrf_token": self.token(), "name": name, "email": email,
            "password": password, "confirm": password})

    def login(self, email="test@example.com", password="password123", next_url=None):
        url = "/login" + (f"?next={next_url}" if next_url else "")
        return self.client.post(url, data={
            "csrf_token": self.token(), "email": email, "password": password})
