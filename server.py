from __future__ import annotations

import asyncio
import json
import random
import re
import sqlite3
import time
import uuid
import os
import secrets
import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Tuple, List
from urllib.parse import parse_qs

from fastapi import FastAPI, Response, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocket, WebSocketDisconnect

from wordfreq import zipf_frequency
# ---------------------------
# Deploy-safe defaults
# ---------------------------
# These constants are referenced in runtime paths. Define them unconditionally so
# production deploys don't crash if earlier refactors moved/removed definitions.
try:
    DISCONNECT_GRACE_SECONDS
except NameError:
    DISCONNECT_GRACE_SECONDS = 15

try:
    SUBMIT_RATE_LIMIT_SECONDS
except NameError:
    SUBMIT_RATE_LIMIT_SECONDS = 0.35

try:
    RANKED_SEARCH_BASE_BAND
except NameError:
    RANKED_SEARCH_BASE_BAND = 100

try:
    RANKED_SEARCH_MAX_BAND
except NameError:
    RANKED_SEARCH_MAX_BAND = 600

try:
    RANKED_SEARCH_STEP_SECONDS
except NameError:
    RANKED_SEARCH_STEP_SECONDS = 2.5

try:
    RANKED_SEARCH_STEP_BAND
except NameError:
    RANKED_SEARCH_STEP_BAND = 50

try:
    RANKED_NO_BOT_TIMEOUT
except NameError:
    RANKED_NO_BOT_TIMEOUT = 10

def tier_for_rating(rating: int) -> str:
    """Return a tier label for a given Elo rating."""
    try:
        r = int(rating)
    except Exception:
        r = 1200
    if r < 1000:
        return "Bronze"
    if r < 1200:
        return "Silver"
    if r < 1400:
        return "Gold"
    if r < 1600:
        return "Platinum"
    if r < 1800:
        return "Diamond"
    return "Master"


app = FastAPI()

SESSION_COOKIE_NAME = "vowely_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
PASSWORD_MIN_LENGTH = 8
USERNAME_RE = re.compile(r"^[a-z0-9_]{3,20}$")


# --- Hard CORS (GitHub Pages -> Render) ---
HARD_CORS_ALLOW_ORIGINS = {
    "https://jasonw79118.github.io",
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
}

@app.middleware("http")
async def hard_cors_middleware(request, call_next):
    # Handle preflight
    origin = request.headers.get("origin")
    if request.method == "OPTIONS":
        resp = Response(status_code=204)
    else:
        resp = await call_next(request)

    if origin and origin in HARD_CORS_ALLOW_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = request.headers.get(
            "access-control-request-headers", "*"
        )
    return resp
# --- End Hard CORS ---

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def root():
    return FileResponse("static/index.html")

@app.head("/")
def root_head():
    # Render health checks often use HEAD /. FastAPI doesn't always auto-wire HEAD when
    # returning FileResponse, so we provide an explicit 200 for stability.
    return Response(status_code=200)

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.head("/healthz")
def healthz_head():
    return Response(status_code=200)


@app.get("/api/config")
def api_config():
    return {"roundSeconds": int(ROUND_SECONDS)}

@app.get("/api/leaderboard")
def api_leaderboard(limit: int = 50):
    limit = max(1, min(int(limit), 100))
    cur = DB.cursor()
    cur.execute(
        "SELECT name, rating, wins, losses, tier, ranked_games, casual_games FROM users ORDER BY rating DESC, wins DESC LIMIT ?",
        (limit,),
    )
    rows = cur.fetchall() or []
    out = []
    for i, r in enumerate(rows, start=1):
        out.append({
            "rank": i,
            "name": str(r["name"]),
            "rating": int(r["rating"]),
            "wins": int(r["wins"]),
            "losses": int(r["losses"]),
            "tier": str(r["tier"]) if "tier" in r.keys() else tier_for_rating(int(r["rating"])),
            "ranked_games": int(r["ranked_games"]) if "ranked_games" in r.keys() else 0,
            "casual_games": int(r["casual_games"]) if "casual_games" in r.keys() else 0,
        })
    return {"items": out}

@app.get("/api/me")
def api_me(request: Request):
    user = get_current_user_from_request(request)

    # If authenticated user
    if user:
        return {
            "authenticated": True,
            "profile": profile_payload(user),
            "recent": get_recent_matches(user["user_id"], 20),
        }

    # Guest fallback
    guest_id = guest_id_from_request(request)
    if guest_id:
        user = get_or_create_user(guest_id, "Guest")
        return {
            "authenticated": False,
            "profile": profile_payload(user),
            "recent": get_recent_matches(user["user_id"], 20),
        }

    return {
        "authenticated": False,
        "profile": None,
        "recent": [],
    }

# ---------------------------
# Game rules (Vowely)
# ---------------------------
VOWELS = set("aeiou")
ALLOWED_EXTRA = set("y")
ALLOWED_NON_CONSONANTS = VOWELS | ALLOWED_EXTRA
ALL_CONSONANTS = [c for c in "abcdefghijklmnopqrstuvwxyz" if c not in VOWELS and c != "y"]
WORD_RE = re.compile(r"^[a-z]+$")

ROUND_SECONDS = 120

MIN_WORD = 3
MAX_WORD = 24

# Bot fallback
BOT_FALLBACK_SECONDS = 10
BOT_FIRST_NAMES = [
    "Ava","Mia","Zoey","Ella","Lily","Grace","Aria","Nora","Ruby","Ivy",
    "Liam","Noah","Ethan","Mason","Logan","Lucas","Owen","Jack","Leo","Wyatt",
    "Caleb","Henry","Miles","Ezra","Finn","Milo","Kai","Zane","Jade","Skye"
]

# Make bot easier
BOT_SLEEP_RANGE = (1.8, 3.6)      # slower = easier
BOT_POINTS_RANGE = (1, 3)         # smaller points = easier
BOT_MAX_SCORE_CAP = 55            # prevents runaway wins


def is_real_word(word: str) -> bool:
    return zipf_frequency(word, "en") >= 2.2


def score_word(word: str) -> int:
    return len(word)


def pick_consonants() -> Set[str]:
    k = random.choice([2, 3, 3])
    return set(random.sample(ALL_CONSONANTS, k))


def pick_bot_name() -> str:
    return random.choice(BOT_FIRST_NAMES)


# ---------------------------
# Persistence (SQLite)
# ---------------------------
DB_PATH = "vowely.db"


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


DB = db_connect()


def _now_ts() -> float:
    return time.time()


def _safe_row_get(row: sqlite3.Row, key: str, default=None):
    try:
        return row[key] if key in row.keys() else default
    except Exception:
        return default


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def normalize_username(value: str) -> str:
    return (value or "").strip().lower()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200000)
    return f"pbkdf2_sha256$200000${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds, salt, expected = (stored or "").split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(rounds))
        return hmac.compare_digest(dk.hex(), expected)
    except Exception:
        return False


def profile_payload(user: sqlite3.Row) -> dict:
    return {
        "userId": str(user["user_id"]),
        "username": str(_safe_row_get(user, "username", "") or ""),
        "displayName": str(_safe_row_get(user, "name", "") or ""),
        "email": str(_safe_row_get(user, "email", "") or ""),
        "isGuest": bool(int(_safe_row_get(user, "is_guest", 1) or 0)),
        "authProvider": str(_safe_row_get(user, "auth_provider", "guest") or "guest"),
        "avatarSeed": str(_safe_row_get(user, "avatar_seed", "") or ""),
        "rating": int(_safe_row_get(user, "rating", 1200) or 1200),
        "wins": int(_safe_row_get(user, "wins", 0) or 0),
        "losses": int(_safe_row_get(user, "losses", 0) or 0),
        "tier": str(_safe_row_get(user, "tier", tier_for_rating(int(_safe_row_get(user, "rating", 1200) or 1200))) or "Bronze"),
        "rankedGames": int(_safe_row_get(user, "ranked_games", 0) or 0),
        "casualGames": int(_safe_row_get(user, "casual_games", 0) or 0),
        "lastResult": str(_safe_row_get(user, "last_result", "") or ""),
        "lastDelta": int(_safe_row_get(user, "last_delta", 0) or 0),
    }


def get_user_by_email_or_username(email_or_username: str) -> Optional[sqlite3.Row]:
    value = (email_or_username or "").strip()
    if not value:
        return None
    cur = DB.cursor()
    cur.execute(
        "SELECT * FROM users WHERE lower(email) = ? OR lower(username) = ? LIMIT 1",
        (normalize_email(value), normalize_username(value)),
    )
    return cur.fetchone()


def username_exists(username: str, exclude_user_id: str = "") -> bool:
    cur = DB.cursor()
    if exclude_user_id:
        cur.execute("SELECT 1 FROM users WHERE lower(username) = ? AND user_id != ? LIMIT 1", (normalize_username(username), exclude_user_id))
    else:
        cur.execute("SELECT 1 FROM users WHERE lower(username) = ? LIMIT 1", (normalize_username(username),))
    return cur.fetchone() is not None


def email_exists(email: str, exclude_user_id: str = "") -> bool:
    cur = DB.cursor()
    if exclude_user_id:
        cur.execute("SELECT 1 FROM users WHERE lower(email) = ? AND user_id != ? LIMIT 1", (normalize_email(email), exclude_user_id))
    else:
        cur.execute("SELECT 1 FROM users WHERE lower(email) = ? LIMIT 1", (normalize_email(email),))
    return cur.fetchone() is not None


def create_session(user_id: str, request: Optional[Request] = None) -> str:
    now = _now_ts()
    sid = secrets.token_urlsafe(32)
    ua = request.headers.get("user-agent", "")[:300] if request else ""
    ip = (request.client.host if request and request.client else "")[:80]
    DB.execute(
        "INSERT INTO sessions (session_id, user_id, created_at, expires_at, last_seen_at, user_agent, ip_address) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, user_id, now, now + SESSION_TTL_SECONDS, now, ua, ip),
    )
    DB.commit()
    return sid


def get_session(session_id: str) -> Optional[sqlite3.Row]:
    if not session_id:
        return None
    cur = DB.cursor()
    cur.execute("SELECT * FROM sessions WHERE session_id = ? LIMIT 1", (session_id,))
    row = cur.fetchone()
    if not row:
        return None
    if float(row["expires_at"] or 0) < _now_ts():
        DB.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        DB.commit()
        return None
    DB.execute("UPDATE sessions SET last_seen_at = ? WHERE session_id = ?", (_now_ts(), session_id))
    DB.commit()
    return row


def destroy_session(session_id: str) -> None:
    if not session_id:
        return
    DB.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    DB.commit()


def get_current_user_from_request(request: Request) -> Optional[sqlite3.Row]:
    sid = request.cookies.get(SESSION_COOKIE_NAME, "")
    sess = get_session(sid)
    if not sess:
        return None
    return get_user(str(sess["user_id"]))


def attach_session_cookie(resp: Response, session_id: str) -> None:
    resp.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


def clear_session_cookie(resp: Response) -> None:
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")


def valid_username(value: str) -> bool:
    return bool(USERNAME_RE.fullmatch(normalize_username(value)))


def valid_password(value: str) -> bool:
    return len((value or "").strip()) >= PASSWORD_MIN_LENGTH


def guest_id_from_request(request: Request) -> str:
    return (request.headers.get("X-Guest-Player-Id", "") or "").strip()[:80]


def db_init() -> None:
    cur = DB.cursor()

    # Base tables (backwards compatible)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
      user_id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      rating INTEGER NOT NULL DEFAULT 1200,
      wins INTEGER NOT NULL DEFAULT 0,
      losses INTEGER NOT NULL DEFAULT 0,
      created_at REAL NOT NULL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS matches (
      match_id TEXT PRIMARY KEY,
      created_at REAL NOT NULL,
      a_user TEXT NOT NULL,
      b_user TEXT NOT NULL,
      a_name TEXT NOT NULL,
      b_name TEXT NOT NULL,
      a_score INTEGER NOT NULL,
      b_score INTEGER NOT NULL,
      winner TEXT,
      consonants TEXT NOT NULL,
      vs_bot INTEGER NOT NULL DEFAULT 0
    );
    """)

    # --- Migrations: add Phase 2 columns if missing (safe to run every startup) ---
    cur.execute("PRAGMA table_info(users);")
    user_cols = {row[1] for row in cur.fetchall()}

    if "last_delta" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN last_delta INTEGER NOT NULL DEFAULT 0;")
    if "last_result" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN last_result TEXT NOT NULL DEFAULT '';")
    if "updated_at" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN updated_at REAL NOT NULL DEFAULT 0;")
    if "tier" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN tier TEXT NOT NULL DEFAULT 'Bronze';")
    if "ranked_games" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN ranked_games INTEGER NOT NULL DEFAULT 0;")
    if "casual_games" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN casual_games INTEGER NOT NULL DEFAULT 0;")
    if "email" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN email TEXT;")
    if "password_hash" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN password_hash TEXT;")
    if "auth_provider" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN auth_provider TEXT NOT NULL DEFAULT 'guest';")
    if "is_guest" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN is_guest INTEGER NOT NULL DEFAULT 1;")
    if "is_verified" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN is_verified INTEGER NOT NULL DEFAULT 0;")
    if "avatar_seed" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN avatar_seed TEXT;")
    if "last_login_at" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN last_login_at REAL NOT NULL DEFAULT 0;")
    if "linked_guest_id" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN linked_guest_id TEXT;")
    if "username" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN username TEXT;")

    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users(email);")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_unique ON users(username);")
    cur.execute("CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, last_seen_at REAL NOT NULL, user_agent TEXT, ip_address TEXT);")
    cur.execute("CREATE TABLE IF NOT EXISTS password_reset_tokens (token TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL NOT NULL DEFAULT 0);")
    cur.execute("CREATE TABLE IF NOT EXISTS friend_requests (request_id TEXT PRIMARY KEY, from_user_id TEXT NOT NULL, to_user_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL, responded_at REAL NOT NULL DEFAULT 0);")
    cur.execute("CREATE TABLE IF NOT EXISTS friends (pair_key TEXT PRIMARY KEY, user_a TEXT NOT NULL, user_b TEXT NOT NULL, created_at REAL NOT NULL);")

    cur.execute("PRAGMA table_info(matches);")
    match_cols = {row[1] for row in cur.fetchall()}

    if "winner_user" not in match_cols:
        cur.execute("ALTER TABLE matches ADD COLUMN winner_user TEXT;")
    if "winner_name" not in match_cols:
        cur.execute("ALTER TABLE matches ADD COLUMN winner_name TEXT;")
    if "delta_a" not in match_cols:
        cur.execute("ALTER TABLE matches ADD COLUMN delta_a INTEGER NOT NULL DEFAULT 0;")
    if "delta_b" not in match_cols:
        cur.execute("ALTER TABLE matches ADD COLUMN delta_b INTEGER NOT NULL DEFAULT 0;")
    if "is_ranked" not in match_cols:
        cur.execute("ALTER TABLE matches ADD COLUMN is_ranked INTEGER NOT NULL DEFAULT 1;")
    if "ended_at" not in match_cols:
        cur.execute("ALTER TABLE matches ADD COLUMN ended_at REAL NOT NULL DEFAULT 0;")
    if "ended_reason" not in match_cols:
        cur.execute("ALTER TABLE matches ADD COLUMN ended_reason TEXT NOT NULL DEFAULT '';")

    DB.commit()



def get_or_create_user(user_id: str, default_name: str) -> sqlite3.Row:
    cur = DB.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row:
        return row
    now = time.time()
    cur.execute(
        "INSERT INTO users (user_id, name, rating, wins, losses, created_at, tier, ranked_games, casual_games) VALUES (?, ?, 1200, 0, 0, ?, ?, 0, 0)",
        (user_id, default_name, now, tier_for_rating(1200)),
    )
    DB.commit()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return cur.fetchone()


def update_user_name(user_id: str, new_name: str) -> None:
    DB.execute("UPDATE users SET name = ? WHERE user_id = ?", (new_name, user_id))
    DB.commit()

# Lightweight display-name validation (no heavy profanity list yet).
# Keep this conservative to avoid crashes; you can tighten later.
_NAME_ALLOWED_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-]{0,15}$")

def is_name_allowed(name: str) -> bool:
    if not name:
        return False
    n = name.strip()
    if len(n) < 2 or len(n) > 16:
        return False
    # Block obvious reserved names
    if n.lower() in {"admin", "moderator", "mod", "support", "system"}:
        return False
    if not _NAME_ALLOWED_RE.match(n):
        return False
    # Collapse multiple spaces to discourage weird formatting
    if "  " in n:
        return False
    return True


def get_user(user_id: str) -> Optional[sqlite3.Row]:
    cur = DB.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return cur.fetchone()


def elo_update(ra: int, rb: int, sa: float, k: int) -> Tuple[int, int]:
    # Expected scores
    ea = 1.0 / (1.0 + 10 ** ((rb - ra) / 400))
    eb = 1.0 / (1.0 + 10 ** ((ra - rb) / 400))
    ra2 = int(round(ra + k * (sa - ea)))
    rb2 = int(round(rb + k * ((1.0 - sa) - eb)))
    return ra2, rb2


def apply_match_result(a_id: str, b_id: str, a_score: int, b_score: int, *, is_ranked: bool) -> Tuple[int, int]:
    """
    Returns (a_rating_delta, b_rating_delta).

    Phase 2 rule:
      - Elo + W/L updates happen only for human-vs-human (caller should pass vs_bot=False).
      - Also updates last_result / last_delta / updated_at for both players.
    """
    a = get_user(a_id)
    b = get_user(b_id)

    if not a or not b:
        return (0, 0)

    ra, rb = int(a["rating"]), int(b["rating"])

    if not is_ranked:
        # Casual: update last_result/updated_at only (no Elo, no W/L)
        if a_score > b_score:
            a_result, b_result = "win", "loss"
        elif b_score > a_score:
            a_result, b_result = "loss", "win"
        else:
            a_result, b_result = "draw", "draw"
        now = time.time()
        DB.execute("UPDATE users SET last_delta = 0, last_result = ?, updated_at = ?, casual_games = casual_games + 1 WHERE user_id = ?",
                   (a_result, now, a_id))
        DB.execute("UPDATE users SET last_delta = 0, last_result = ?, updated_at = ?, casual_games = casual_games + 1 WHERE user_id = ?",
                   (b_result, now, b_id))
        DB.commit()
        return (0, 0)


    if a_score > b_score:
        sa = 1.0
        a_result, b_result = "win", "loss"
        DB.execute("UPDATE users SET wins = wins + 1 WHERE user_id = ?", (a_id,))
        DB.execute("UPDATE users SET losses = losses + 1 WHERE user_id = ?", (b_id,))
    elif b_score > a_score:
        sa = 0.0
        a_result, b_result = "loss", "win"
        DB.execute("UPDATE users SET losses = losses + 1 WHERE user_id = ?", (a_id,))
        DB.execute("UPDATE users SET wins = wins + 1 WHERE user_id = ?", (b_id,))
    else:
        sa = 0.5
        a_result, b_result = "draw", "draw"

    # K-factor: slightly higher if low games played
    a_games = int(a["wins"]) + int(a["losses"])
    b_games = int(b["wins"]) + int(b["losses"])
    k_a = 32 if a_games < 30 else 16
    k_b = 32 if b_games < 30 else 16
    k = int(round((k_a + k_b) / 2))

    ra2, rb2 = elo_update(ra, rb, sa, k)

    a_delta = ra2 - ra
    b_delta = rb2 - rb
    now = time.time()

    DB.execute(
        "UPDATE users SET rating = ?, tier = ?, ranked_games = ranked_games + 1, last_delta = ?, last_result = ?, updated_at = ? WHERE user_id = ?",
        (ra2, tier_for_rating(ra2), a_delta, a_result, now, a_id),
    )
    DB.execute(
        "UPDATE users SET rating = ?, tier = ?, ranked_games = ranked_games + 1, last_delta = ?, last_result = ?, updated_at = ? WHERE user_id = ?",
        (rb2, tier_for_rating(rb2), b_delta, b_result, now, b_id),
    )
    DB.commit()
    return (a_delta, b_delta)



def record_match(
    m: "Match",
    winner_text: Optional[str],
    winner_user: Optional[str],
    winner_name: Optional[str],
    vs_bot: bool,
    delta_a: int = 0,
    delta_b: int = 0,
    is_ranked: int = 1,
    ended_at: float = 0.0,
    ended_reason: str = "",
) -> None:
    DB.execute(
        """INSERT OR REPLACE INTO matches
           (match_id, created_at, a_user, b_user, a_name, b_name, a_score, b_score, winner, consonants, vs_bot,
            winner_user, winner_name, delta_a, delta_b, is_ranked, ended_at, ended_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            m.match_id,
            time.time(),
            m.a_user,
            m.b_user,
            m.a_name,
            m.b_name,
            m.a_score,
            m.b_score,
            winner_text,
            ",".join(sorted(m.consonants)),
            1 if vs_bot else 0,
            winner_user,
            winner_name,
            int(delta_a),
            int(delta_b),
            int(is_ranked),
            float(ended_at),
            str(ended_reason or ""),
        ),
    )
    DB.commit()


def get_recent_matches(user_id: str, limit: int = 20) -> list[dict]:
    cur = DB.cursor()
    cur.execute(
        """SELECT created_at, ended_at, ended_reason, is_ranked, a_user, b_user, a_name, b_name, a_score, b_score,
                  winner_user, delta_a, delta_b, vs_bot
           FROM matches
           WHERE a_user = ? OR b_user = ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (user_id, user_id, int(limit)),
    )
    rows = cur.fetchall() or []
    out = []
    for r in rows:
        is_a = (r["a_user"] == user_id)
        opp_name = r["b_name"] if is_a else r["a_name"]
        score_for = int(r["a_score"] if is_a else r["b_score"])
        score_against = int(r["b_score"] if is_a else r["a_score"])
        delta = int(r["delta_a"] if is_a else r["delta_b"])
        # derive result
        res = "draw"
        if r["winner_user"]:
            res = "win" if r["winner_user"] == user_id else "loss"
        else:
            if score_for > score_against:
                res = "win"
            elif score_for < score_against:
                res = "loss"
        ts = float(r["ended_at"] or r["created_at"])
        played_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        out.append({
            "createdAt": float(r["created_at"]),
            "played_at": played_at,
            "opponent": opp_name,
            "result": res,
            "delta": delta,
            "scoreFor": score_for,
            "scoreAgainst": score_against,
            "vsBot": bool(int(r["vs_bot"])),
            "isRanked": bool(int(r["is_ranked"])) if r["is_ranked"] is not None else False,
            "endedReason": str(r["ended_reason"] or ""),
        })
    return out


# ---------------------------
# Models
# ---------------------------
@dataclass
class PlayerConn:
    ws: WebSocket
    user_id: str
    name: str
    state: str = "idle"  # idle | searching | in_match
    disconnected_at: float = 0.0
    grace_task: Optional[asyncio.Task] = None
    last_submit_at: float = 0.0
    heartbeat_task: Optional[asyncio.Task] = None


@dataclass
class Match:
    match_id: str
    a_user: str
    b_user: str
    a_name: str
    b_name: str
    consonants: Set[str]
    started_at: float
    ends_at: float
    status: str = "live"  # live | complete

    a_score: int = 0
    b_score: int = 0
    a_words: Set[str] = field(default_factory=set)
    b_words: Set[str] = field(default_factory=set)

    vs_bot: bool = False
    is_ranked: bool = True
    ended_reason: str = ""


# ---------------------------
# Hub / State
# ---------------------------
class Hub:
    def __init__(self) -> None:
        self.clients: Dict[str, PlayerConn] = {}
        self.ws_to_user: Dict[int, str] = {}
        # Legacy FIFO queue removed in favor of ladder queues
        self.search_queue: asyncio.Queue[str] = asyncio.Queue()  # unused
        self.ranked_wait: List[Tuple[str, float]] = []   # (user_id, enqueued_at)
        self.casual_wait: List[Tuple[str, float]] = []
        self.queue_lock = asyncio.Lock()
        self.matches: Dict[str, Match] = {}
        self.user_match: Dict[str, str] = {}
        self._match_lock = asyncio.Lock()


    async def _heartbeat(self, user_id: str) -> None:
        """Periodic keepalive messages so hosted proxies don't drop idle websockets."""
        while True:
            await asyncio.sleep(20)
            pc = self.clients.get(user_id)
            if not pc:
                return
            try:
                await pc.ws.send_text(json.dumps({"type": "hb", "t": time.time()}))
            except Exception:
                return

    async def send(self, user_id: str, msg: dict) -> None:
        pc = self.clients.get(user_id)
        if not pc:
            return
        try:
            await pc.ws.send_text(json.dumps(msg))
        except Exception:
            pass

    async def send_both(self, m: Match, msg: dict) -> None:
        await self.send(m.a_user, msg)
        if not m.vs_bot:
            await self.send(m.b_user, msg)

    async def broadcast_scores(self, m: Match) -> None:
        # send to human(s)
        await self.send(m.a_user, {
            "type": "score",
            "matchId": m.match_id,
            "a": {"name": m.a_name, "score": m.a_score},
            "b": {"name": m.b_name, "score": m.b_score},
        })
        if not m.vs_bot:
            await self.send(m.b_user, {
                "type": "score",
                "matchId": m.match_id,
                "a": {"name": m.a_name, "score": m.a_score},
                "b": {"name": m.b_name, "score": m.b_score},
            })

    def get_player_set(self, m: Match, user_id: str) -> Tuple[Set[str], str]:
        if user_id == m.a_user:
            return m.a_words, "a"
        return m.b_words, "b"


    async def enqueue(self, user_id: str, *, is_ranked: bool) -> None:
        async with self.queue_lock:
            now = time.time()
            if is_ranked:
                self.ranked_wait = [(u,t) for (u,t) in self.ranked_wait if u != user_id]
                self.ranked_wait.append((user_id, now))
            else:
                self.casual_wait = [(u,t) for (u,t) in self.casual_wait if u != user_id]
                self.casual_wait.append((user_id, now))

    async def cancel_search(self, user_id: str) -> None:
        async with self.queue_lock:
            self.ranked_wait = [(u,t) for (u,t) in self.ranked_wait if u != user_id]
            self.casual_wait = [(u,t) for (u,t) in self.casual_wait if u != user_id]

    async def pop_ranked_match(self) -> Optional[Tuple[str, str, int]]:
        async with self.queue_lock:
            self.ranked_wait = [(u,t) for (u,t) in self.ranked_wait if (u in self.clients and self.clients[u].state == "searching")]
            if len(self.ranked_wait) < 2:
                return None
            def rating_of(u: str) -> int:
                row = get_user(u)
                return int(row["rating"]) if row else 1200
            self.ranked_wait.sort(key=lambda ut: rating_of(ut[0]))
            for i in range(len(self.ranked_wait) - 1):
                u1, t1 = self.ranked_wait[i]
                r1 = rating_of(u1)
                waited = max(0.0, time.time() - t1)
                band = min(RANKED_SEARCH_MAX_BAND, RANKED_SEARCH_BASE_BAND + int(waited // RANKED_SEARCH_STEP_SECONDS) * RANKED_SEARCH_STEP_BAND)
                for j in range(i + 1, len(self.ranked_wait)):
                    u2, _t2 = self.ranked_wait[j]
                    r2 = rating_of(u2)
                    if r2 - r1 > band:
                        break
                    if u1 != u2:
                        self.ranked_wait = [(u,t) for (u,t) in self.ranked_wait if u not in {u1,u2}]
                        return (u1, u2, band)
            return None

    async def pop_casual_opponent(self, user1: str) -> Optional[str]:
        async with self.queue_lock:
            self.casual_wait = [(u,t) for (u,t) in self.casual_wait if (u in self.clients and self.clients[u].state == "searching")]
            for (u,_t) in list(self.casual_wait):
                if u != user1:
                    self.casual_wait = [(x,tx) for (x,tx) in self.casual_wait if x != u]
                    return u
            return None

hub = Hub()


async def bot_play(match_id: str):
    while True:
        m = hub.matches.get(match_id)
        if not m or m.status != "live" or not m.vs_bot:
            return
        if time.time() >= m.ends_at:
            return

        await asyncio.sleep(random.uniform(*BOT_SLEEP_RANGE))

        m = hub.matches.get(match_id)
        if not m or m.status != "live" or not m.vs_bot:
            return

        if m.b_score >= BOT_MAX_SCORE_CAP:
            continue

        pts = random.randint(*BOT_POINTS_RANGE)
        m.b_score += pts
        await hub.broadcast_scores(m)


async def forfeit_if_not_reconnected(user_id: str, match_id: str, disconnected_at: float):
    await asyncio.sleep(DISCONNECT_GRACE_SECONDS)
    pc = hub.clients.get(user_id)
    if pc and pc.disconnected_at != disconnected_at:
        return
    m = hub.matches.get(match_id)
    if not m or m.status != "live":
        return
    m.ended_reason = "forfeit"
    m.status = "complete"
    winner_user = m.b_user if user_id == m.a_user else m.a_user
    winner_name = m.b_name if user_id == m.a_user else m.a_name
    winner_text = winner_name
    vs_bot = m.vs_bot
    is_ranked = bool(getattr(m, "is_ranked", True))
    a_delta = 0
    b_delta = 0
    if (not vs_bot) and is_ranked:
        a_delta, b_delta = apply_match_result(m.a_user, m.b_user, m.a_score, m.b_score, is_ranked=True)
    elif (not vs_bot) and (not is_ranked):
        a_delta, b_delta = apply_match_result(m.a_user, m.b_user, m.a_score, m.b_score, is_ranked=False)
    record_match(m, winner_text=winner_text, winner_user=winner_user, winner_name=winner_name, vs_bot=vs_bot,
                 delta_a=a_delta, delta_b=b_delta, is_ranked=(1 if is_ranked else 0),
                 ended_at=time.time(), ended_reason="forfeit")
    payload = {"type":"matchEnd","matchId":m.match_id,"a":{"name":m.a_name,"score":m.a_score},"b":{"name":m.b_name,"score":m.b_score},
               "winner":winner_text,"ratingDelta":{"a":a_delta,"b":b_delta},"mode":("ranked" if is_ranked else "casual"),
               "endedReason":"forfeit"}
    await hub.send(m.a_user, payload)
    if not vs_bot:
        await hub.send(m.b_user, payload)
    a_user = get_user(m.a_user)
    if a_user:
        await hub.send(m.a_user, {"type":"result","result":("win" if winner_user==m.a_user else "loss"),"delta":int(a_delta),
                                  "rating":int(a_user["rating"]),"wins":int(a_user["wins"]),"losses":int(a_user["losses"]),
                                  "tier":str(a_user["tier"]) if "tier" in a_user.keys() else tier_for_rating(int(a_user["rating"])),
                                  "ranked":bool(is_ranked),"opponent":m.b_name,"score_for":m.a_score,"score_against":m.b_score,
                                  "recent":get_recent_matches(m.a_user, limit=20)})
    if not vs_bot:
        b_user = get_user(m.b_user)
        if b_user:
            await hub.send(m.b_user, {"type":"result","result":("win" if winner_user==m.b_user else "loss"),"delta":int(b_delta),
                                      "rating":int(b_user["rating"]),"wins":int(b_user["wins"]),"losses":int(b_user["losses"]),
                                      "tier":str(b_user["tier"]) if "tier" in b_user.keys() else tier_for_rating(int(b_user["rating"])),
                                      "ranked":bool(is_ranked),"opponent":m.a_name,"score_for":m.b_score,"score_against":m.a_score,
                                      "recent":get_recent_matches(m.b_user, limit=20)})
    for uid in [m.a_user, m.b_user]:
        pc2 = hub.clients.get(uid)
        if pc2 and pc2.state == "in_match":
            pc2.state = "idle"
        hub.user_match.pop(uid, None)
    await asyncio.sleep(60)
    hub.matches.pop(match_id, None)


# ---------------------------
# Matchmaking loop (in-memory)
# ---------------------------
async def start_match(user1: str, user2: str, *, is_ranked: bool, band: Optional[int], use_bot: bool = False, bot_name: Optional[str] = None):
    pc1 = hub.clients.get(user1)
    pc2 = hub.clients.get(user2) if not use_bot else None
    if not pc1 or pc1.state != "searching":
        return
    if not use_bot:
        if not pc2 or pc2.state != "searching":
            return
    async with hub._match_lock:
        pc1 = hub.clients.get(user1)
        pc2 = hub.clients.get(user2) if not use_bot else None
        if not pc1 or pc1.state != "searching":
            return
        if not use_bot and (not pc2 or pc2.state != "searching"):
            return
        match_id = str(uuid.uuid4())
        cons = pick_consonants()
        now = time.time()
        m = Match(match_id=match_id, a_user=user1, b_user=user2, a_name=pc1.name,
                  b_name=(bot_name if use_bot else pc2.name), consonants=cons,
                  started_at=now, ends_at=now + ROUND_SECONDS, vs_bot=use_bot, is_ranked=bool(is_ranked))
        hub.matches[match_id] = m
        hub.user_match[user1] = match_id
        pc1.state = "in_match"
        if not use_bot:
            hub.user_match[user2] = match_id
            pc2.state = "in_match"
    await hub.send(user1, {"type":"matchFound","matchId":m.match_id,"youAre":"a","opponent":m.b_name,"consonants":sorted(list(m.consonants)),
                           "endsAt":m.ends_at,"roundSeconds":ROUND_SECONDS,"mode":("ranked" if is_ranked else "casual"),"band":band})
    if not use_bot:
        await hub.send(user2, {"type":"matchFound","matchId":m.match_id,"youAre":"b","opponent":m.a_name,"consonants":sorted(list(m.consonants)),
                               "endsAt":m.ends_at,"roundSeconds":ROUND_SECONDS,"mode":("ranked" if is_ranked else "casual"),"band":band})
    await hub.broadcast_scores(m)
    asyncio.create_task(end_match_at(m.match_id, m.ends_at))
    if use_bot:
        asyncio.create_task(bot_play(m.match_id))

async def matchmaking_loop():
    while True:
        ranked = await hub.pop_ranked_match()
        if ranked:
            u1, u2, band = ranked
            await start_match(u1, u2, is_ranked=True, band=band)
            await asyncio.sleep(0)
            continue
        async with hub.queue_lock:
            hub.casual_wait = [(u,t) for (u,t) in hub.casual_wait if (u in hub.clients and hub.clients[u].state == "searching")]
            user1 = hub.casual_wait[0][0] if hub.casual_wait else None
        if user1:
            opp = await hub.pop_casual_opponent(user1)
            if opp:
                await start_match(user1, opp, is_ranked=False, band=None)
                await asyncio.sleep(0)
                continue
            pc1 = hub.clients.get(user1)
            if pc1 and pc1.state == "searching":
                async with hub.queue_lock:
                    t0 = next((t for (u,t) in hub.casual_wait if u == user1), None)
                if t0 and (time.time() - t0) >= BOT_FALLBACK_SECONDS:
                    await hub.cancel_search(user1)
                    bot_user_id = f"bot-{uuid.uuid4()}"
                    bot_name = pick_bot_name()
                    await start_match(user1, bot_user_id, is_ranked=False, band=None, use_bot=True, bot_name=bot_name)
                    await asyncio.sleep(0)
                    continue
        async with hub.queue_lock:
            now = time.time()
            for (u,t0) in list(hub.ranked_wait)[:20]:
                if now - t0 >= RANKED_NO_BOT_TIMEOUT:
                    await hub.send(u, {"type": "rankedSearchTimeout", "seconds": int(now - t0)})
        await asyncio.sleep(0.25)


async def end_match_at(match_id: str, ends_at: float):
    await asyncio.sleep(max(0.0, ends_at - time.time()))
    m = hub.matches.get(match_id)
    if not m or m.status != "live":
        return

    m.status = "complete"

    winner_text: Optional[str] = None
    winner_user: Optional[str] = None
    winner_name: Optional[str] = None

    if m.a_score > m.b_score:
        winner_text = m.a_name
        winner_user = m.a_user
        winner_name = m.a_name
    elif m.b_score > m.a_score:
        winner_text = m.b_name
        winner_user = m.b_user
        winner_name = m.b_name

    vs_bot = m.vs_bot
    is_ranked = bool(getattr(m, 'is_ranked', True))

    a_delta = 0
    b_delta = 0

    # Elo + W/L only for human-vs-human
    if (not vs_bot) and is_ranked:
        a_delta, b_delta = apply_match_result(m.a_user, m.b_user, m.a_score, m.b_score, is_ranked=True)
    elif (not vs_bot) and (not is_ranked):
        a_delta, b_delta = apply_match_result(m.a_user, m.b_user, m.a_score, m.b_score, is_ranked=False)

    # Persist match (store deltas)
    record_match(
        m,
        winner_text=winner_text,
        winner_user=winner_user,
        winner_name=winner_name,
        vs_bot=vs_bot,
        delta_a=a_delta,
        delta_b=b_delta,
        is_ranked=(1 if is_ranked else 0),
        ended_at=time.time(),
        ended_reason=str(getattr(m, 'ended_reason', '') or ''),
    )

    # send result (legacy shape)
    payload = {
        "type": "matchEnd",
        "matchId": m.match_id,
        "a": {"name": m.a_name, "score": m.a_score},
        "b": {"name": m.b_name, "score": m.b_score},
        "winner": winner_text,
        "ratingDelta": {"a": a_delta, "b": b_delta},
        "mode": "ranked" if is_ranked else "casual",
    }
    await hub.send(m.a_user, payload)
    if not vs_bot:
        await hub.send(m.b_user, payload)

    # phase 2: send per-player profile update
    a_user = get_user(m.a_user)
    if a_user:
        await hub.send(m.a_user, {
            "type": "result",
            "result": ("win" if winner_user == m.a_user else ("loss" if winner_user else "draw")),
            "delta": int(a_delta),
            "rating": int(a_user["rating"]),
            "wins": int(a_user["wins"]),
            "losses": int(a_user["losses"]),
            "opponent": m.b_name,
            "score_for": m.a_score,
            "score_against": m.b_score,
        })

    if not vs_bot:
        b_user = get_user(m.b_user)
        if b_user:
            await hub.send(m.b_user, {
                "type": "result",
                "result": ("win" if winner_user == m.b_user else ("loss" if winner_user else "draw")),
                "delta": int(b_delta),
                "rating": int(b_user["rating"]),
                "wins": int(b_user["wins"]),
                "losses": int(b_user["losses"]),
                "opponent": m.a_name,
                "score_for": m.b_score,
                "score_against": m.a_score,
            })

    # reset player(s)
    for uid in [m.a_user, m.b_user]:
        pc = hub.clients.get(uid)
        if pc and pc.state == "in_match":
            pc.state = "idle"
        hub.user_match.pop(uid, None)

    await asyncio.sleep(60)
    hub.matches.pop(match_id, None)


@app.on_event("startup")
async def startup():
    db_init()
    asyncio.create_task(matchmaking_loop())


# ---------------------------
# WebSocket endpoint
# ---------------------------
@app.get("/api/me")
def api_me(request: Request):
    user = get_current_user_from_request(request)
    if user:
        return {"authenticated": True, "profile": profile_payload(user), "recent": get_recent_matches(str(user["user_id"]), limit=20)}
    pid = guest_id_from_request(request)
    if pid:
        guest = get_user(pid)
        if guest:
            return {"authenticated": False, "profile": profile_payload(guest), "recent": get_recent_matches(str(guest["user_id"]), limit=20)}
    return {"authenticated": False, "profile": None, "recent": []}


@app.post("/api/auth/signup")
async def api_auth_signup(request: Request):
    data = await request.json()
    email = normalize_email(data.get("email", ""))
    username = normalize_username(data.get("username", ""))
    password = (data.get("password", "") or "")
    display_name = (data.get("displayName") or data.get("display_name") or username).strip()

    if not email or "@" not in email:
        return JSONResponse({"ok": False, "error": "Enter a valid email address."}, status_code=400)
    if not valid_username(username):
        return JSONResponse({"ok": False, "error": "Username must be 3-20 chars using letters, numbers, or underscore."}, status_code=400)
    if not valid_password(password):
        return JSONResponse({"ok": False, "error": "Password must be at least 8 characters."}, status_code=400)
    if not is_name_allowed(display_name):
        return JSONResponse({"ok": False, "error": "Display name is not allowed."}, status_code=400)
    if email_exists(email):
        return JSONResponse({"ok": False, "error": "That email is already in use."}, status_code=400)
    if username_exists(username):
        return JSONResponse({"ok": False, "error": "That username is already taken."}, status_code=400)

    user_id = str(uuid.uuid4())
    now = _now_ts()
    DB.execute(
        "INSERT INTO users (user_id, name, rating, wins, losses, created_at, tier, ranked_games, casual_games, email, password_hash, auth_provider, is_guest, is_verified, avatar_seed, last_login_at, linked_guest_id, username) VALUES (?, ?, 1200, 0, 0, ?, ?, 0, 0, ?, ?, 'password', 0, 0, ?, ?, '', ?)",
        (user_id, display_name, now, tier_for_rating(1200), email, hash_password(password), username, now, username),
    )
    DB.commit()
    user = get_user(user_id)
    sid = create_session(user_id, request)
    resp = JSONResponse({"ok": True, "profile": profile_payload(user), "recent": []})
    attach_session_cookie(resp, sid)
    return resp


@app.post("/api/auth/login")
async def api_auth_login(request: Request):
    data = await request.json()
    value = data.get("emailOrUsername", "")
    password = (data.get("password", "") or "")
    user = get_user_by_email_or_username(value)
    if not user or not verify_password(password, str(_safe_row_get(user, "password_hash", "") or "")):
        return JSONResponse({"ok": False, "error": "Invalid login."}, status_code=400)
    now = _now_ts()
    DB.execute("UPDATE users SET last_login_at = ?, is_guest = 0, auth_provider = CASE WHEN auth_provider = '' THEN 'password' ELSE auth_provider END WHERE user_id = ?", (now, str(user["user_id"])))
    DB.commit()
    user = get_user(str(user["user_id"]))
    sid = create_session(str(user["user_id"]), request)
    resp = JSONResponse({"ok": True, "profile": profile_payload(user), "recent": get_recent_matches(str(user["user_id"]), limit=20)})
    attach_session_cookie(resp, sid)
    return resp


@app.post("/api/auth/logout")
async def api_auth_logout(request: Request):
    sid = request.cookies.get(SESSION_COOKIE_NAME, "")
    destroy_session(sid)
    resp = JSONResponse({"ok": True})
    clear_session_cookie(resp)
    return resp


@app.post("/api/auth/upgrade-guest")
async def api_auth_upgrade_guest(request: Request):
    data = await request.json()
    guest_id = guest_id_from_request(request) or str(data.get("guestId", "")).strip()
    if not guest_id:
        return JSONResponse({"ok": False, "error": "Guest player id not found."}, status_code=400)
    user = get_user(guest_id)
    if not user:
        return JSONResponse({"ok": False, "error": "Guest profile not found."}, status_code=404)

    email = normalize_email(data.get("email", ""))
    username = normalize_username(data.get("username", ""))
    password = (data.get("password", "") or "")
    display_name = (data.get("displayName") or data.get("display_name") or str(user["name"])).strip()

    if not email or "@" not in email:
        return JSONResponse({"ok": False, "error": "Enter a valid email address."}, status_code=400)
    if not valid_username(username):
        return JSONResponse({"ok": False, "error": "Username must be 3-20 chars using letters, numbers, or underscore."}, status_code=400)
    if not valid_password(password):
        return JSONResponse({"ok": False, "error": "Password must be at least 8 characters."}, status_code=400)
    if not is_name_allowed(display_name):
        return JSONResponse({"ok": False, "error": "Display name is not allowed."}, status_code=400)
    if email_exists(email, exclude_user_id=guest_id):
        return JSONResponse({"ok": False, "error": "That email is already in use."}, status_code=400)
    if username_exists(username, exclude_user_id=guest_id):
        return JSONResponse({"ok": False, "error": "That username is already taken."}, status_code=400)

    now = _now_ts()
    DB.execute(
        "UPDATE users SET name = ?, email = ?, username = ?, password_hash = ?, auth_provider = 'password', is_guest = 0, is_verified = 0, avatar_seed = COALESCE(avatar_seed, ?), last_login_at = ?, linked_guest_id = ? WHERE user_id = ?",
        (display_name, email, username, hash_password(password), username, now, guest_id, guest_id),
    )
    DB.commit()
    user = get_user(guest_id)
    sid = create_session(guest_id, request)
    resp = JSONResponse({"ok": True, "profile": profile_payload(user), "recent": get_recent_matches(guest_id, limit=20)})
    attach_session_cookie(resp, sid)
    return resp


@app.patch("/api/me")
async def api_me_patch(request: Request):
    user = get_current_user_from_request(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Not logged in."}, status_code=401)
    data = await request.json()
    display_name = (data.get("displayName") or data.get("display_name") or str(user["name"])).strip()
    avatar_seed = (data.get("avatarSeed") or data.get("avatar_seed") or _safe_row_get(user, "avatar_seed", "")).strip()[:64]
    if not is_name_allowed(display_name):
        return JSONResponse({"ok": False, "error": "Display name is not allowed."}, status_code=400)
    DB.execute("UPDATE users SET name = ?, avatar_seed = ? WHERE user_id = ?", (display_name, avatar_seed, str(user["user_id"])))
    DB.commit()
    user = get_user(str(user["user_id"]))
    return {"ok": True, "profile": profile_payload(user)}


@app.get("/api/friends")
def api_friends(request: Request):
    user = get_current_user_from_request(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Not logged in."}, status_code=401)
    uid = str(user["user_id"])
    cur = DB.cursor()
    cur.execute("SELECT request_id, from_user_id, to_user_id, status, created_at FROM friend_requests WHERE status = 'pending' AND (from_user_id = ? OR to_user_id = ?) ORDER BY created_at DESC", (uid, uid))
    pending = [dict(r) for r in cur.fetchall() or []]
    cur.execute("SELECT user_a, user_b, created_at FROM friends WHERE user_a = ? OR user_b = ? ORDER BY created_at DESC", (uid, uid))
    friends = []
    for r in cur.fetchall() or []:
        other = r["user_b"] if r["user_a"] == uid else r["user_a"]
        u = get_user(str(other))
        if u:
            friends.append(profile_payload(u))
    return {"ok": True, "friends": friends, "pending": pending}


def get_pid_from_ws(ws: WebSocket) -> str:
    # ws.scope["query_string"] is bytes like b"pid=..."
    qs = ws.scope.get("query_string", b"").decode("utf-8", errors="ignore")
    params = parse_qs(qs)
    pid = (params.get("pid", [""])[0] or "").strip()
    # basic sanity
    if not pid or len(pid) > 80:
        pid = str(uuid.uuid4())
    return pid


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()

    user_id = get_pid_from_ws(ws)
    default_name = f"Player-{user_id[-4:]}"
    user = get_or_create_user(user_id, default_name)

    existing = hub.clients.get(user_id)
    if existing:
        existing.ws = ws
        existing.disconnected_at = 0.0
        if existing.grace_task and not existing.grace_task.done():
            existing.grace_task.cancel()
        pc = existing
    else:
        pc = PlayerConn(ws=ws, user_id=user_id, name=user["name"])
        hub.clients[user_id] = pc
    # keepalive heartbeat for hosted websockets
    if pc.heartbeat_task and not pc.heartbeat_task.done():
        pc.heartbeat_task.cancel()
    pc.heartbeat_task = asyncio.create_task(hub._heartbeat(user_id))
    hub.ws_to_user[id(ws)] = user_id

    profile = {
        "rating": int(user["rating"]),
        "wins": int(user["wins"]),
        "losses": int(user["losses"]),
        "last_delta": int(user["last_delta"]) if "last_delta" in user.keys() else 0,
        "last_result": str(user["last_result"]) if "last_result" in user.keys() else "",
        "tier": str(user["tier"]) if "tier" in user.keys() else tier_for_rating(int(user["rating"])),
        "ranked_games": int(user["ranked_games"]) if "ranked_games" in user.keys() else 0,
        "casual_games": int(user["casual_games"]) if "casual_games" in user.keys() else 0,
    }
    recent = get_recent_matches(user_id, limit=20)

    await ws.send_text(json.dumps({
        "type": "hello",
        "userId": user_id,
        "pid": user_id,
        "name": pc.name,
        # legacy flat fields (keep existing client working)
        "rating": profile["rating"],
        "wins": profile["wins"],
        "losses": profile["losses"],
        # phase 2
        "profile": profile,
        "recent": recent,
    }))

    mid = hub.user_match.get(user_id)
    m_active = hub.matches.get(mid) if mid else None
    if m_active and m_active.status == "live":
        side = "a" if user_id == m_active.a_user else "b"
        opp = m_active.b_name if side == "a" else m_active.a_name
        await hub.send(user_id, {
            "type": "reconnected",
            "matchId": m_active.match_id,
            "youAre": side,
            "opponent": opp,
            "consonants": sorted(list(m_active.consonants)),
            "endsAt": m_active.ends_at,
            "mode": "ranked" if bool(getattr(m_active, "is_ranked", True)) else "casual",
        })
        await hub.send(user_id, {
            "type": "score",
            "matchId": m_active.match_id,
            "a": {"name": m_active.a_name, "score": m_active.a_score},
            "b": {"name": m_active.b_name, "score": m_active.b_score},
        })

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "setName":
                new_name = (msg.get("name") or "").strip()[:24]
                if new_name and is_name_allowed(new_name):
                    pc.name = new_name
                    update_user_name(user_id, new_name)
                    await hub.send(user_id, {"type": "nameSet", "name": pc.name})
                else:
                    await hub.send(user_id, {"type": "reject", "reason": "Name not allowed."})

            elif mtype == "play":
                if pc.state == "in_match":
                    await hub.send(user_id, {"type": "reject", "reason": "Already in a match."})
                    continue
                pc.state = "searching"
                mode = (msg.get("mode") or "casual").strip().lower()
                is_ranked = (mode == "ranked")
                await hub.send(user_id, {"type": "searching", "mode": ("ranked" if is_ranked else "casual")})
                await hub.enqueue(user_id, is_ranked=is_ranked)

            elif mtype == "cancelSearch":
                if pc.state == "searching":
                    pc.state = "idle"
                await hub.cancel_search(user_id)
                await hub.send(user_id, {"type": "idle"})

            elif mtype == "submit":
                now_ts = time.time()
                if pc.last_submit_at and (now_ts - pc.last_submit_at) < SUBMIT_RATE_LIMIT_SECONDS:
                    await hub.send(user_id, {"type": "reject", "reason": "Too fast. Slow down."})
                    continue
                pc.last_submit_at = now_ts
                word = (msg.get("word") or "").strip().lower()

                match_id = hub.user_match.get(user_id)
                m = hub.matches.get(match_id) if match_id else None
                if not m or m.status != "live":
                    await hub.send(user_id, {"type": "reject", "reason": "Not in a live match."})
                    continue

                if time.time() > m.ends_at:
                    await hub.send(user_id, {"type": "reject", "reason": "Round is over."})
                    continue

                if not word or len(word) < MIN_WORD or len(word) > MAX_WORD:
                    await hub.send(user_id, {"type": "reject", "reason": f"Word length must be {MIN_WORD}–{MAX_WORD}."})
                    continue

                if not WORD_RE.match(word):
                    await hub.send(user_id, {"type": "reject", "reason": "Letters only (a–z)."})
                    continue

                bad = []
                for ch in word:
                    if ch in ALLOWED_NON_CONSONANTS:
                        continue
                    if ch in m.consonants:
                        continue
                    bad.append(ch)
                if bad:
                    await hub.send(user_id, {"type": "reject", "reason": "Invalid letters for this round."})
                    continue

                if not is_real_word(word):
                    await hub.send(user_id, {"type": "reject", "reason": "Not recognized as an English word."})
                    continue

                used_set, side = hub.get_player_set(m, user_id)
                if word in used_set:
                    await hub.send(user_id, {"type": "reject", "reason": "Already used this match."})
                    continue

                pts = score_word(word)
                used_set.add(word)

                if side == "a":
                    m.a_score += pts
                else:
                    m.b_score += pts

                await hub.send(user_id, {"type": "accept", "word": word, "points": pts})
                await hub.broadcast_scores(m)

            elif mtype == "cheer":
                token = (msg.get("token") or "").strip()
                allowed = {"gg": "GG!", "nice": "Nice!", "clap": "👏", "fire": "🔥", "party": "🎉"}
                if token not in allowed:
                    await hub.send(user_id, {"type": "reject", "reason": "Cheer not allowed."})
                    continue

                match_id = hub.user_match.get(user_id)
                m = hub.matches.get(match_id) if match_id else None
                if not m or m.status != "live":
                    continue

                # send to opponent only if human exists
                if m.vs_bot:
                    await hub.send(m.a_user, {"type": "cheer", "from": pc.name, "text": allowed[token]})
                else:
                    await hub.send_both(m, {"type": "cheer", "from": pc.name, "text": allowed[token]})

            else:
                await hub.send(user_id, {"type": "reject", "reason": "Unknown message type."})

    except WebSocketDisconnect:
        pass
    finally:
        uid = hub.ws_to_user.pop(id(ws), None)
        if uid:
            pcx = hub.clients.get(uid)
            if pcx and pcx.heartbeat_task and not pcx.heartbeat_task.done():
                pcx.heartbeat_task.cancel()
            mid = hub.user_match.get(uid)
            if pcx and mid and pcx.state == "in_match":
                pcx.disconnected_at = time.time()
                if pcx.grace_task and not pcx.grace_task.done():
                    pcx.grace_task.cancel()
                pcx.grace_task = asyncio.create_task(forfeit_if_not_reconnected(uid, mid, pcx.disconnected_at))
            else:
                hub.clients.pop(uid, None)
                hub.user_match.pop(uid, None)
            await hub.cancel_search(uid)

@app.get("/api/cors-check")
async def cors_check():
    return {"ok": True, "allowedOrigins": sorted(HARD_CORS_ALLOW_ORIGINS)}
