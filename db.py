import sqlite3
from pathlib import Path

from flask import g

DB_PATH = Path(__file__).parent / "data" / "ecosort.db"


def get_db():
    """One SQLite connection per request."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create the tables if they do not exist yet."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            email         TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS activity (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            item_name  TEXT NOT NULL,
            category   TEXT NOT NULL,
            material   TEXT,
            bin        TEXT,
            action     TEXT NOT NULL CHECK (action IN ('searched', 'disposed')),
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_activity_user ON activity(user_id, id);

        CREATE TABLE IF NOT EXISTS quiz_results (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            score      INTEGER NOT NULL,
            total      INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_quiz_user ON quiz_results(user_id, id);
        """
    )
    db.commit()
    db.close()
