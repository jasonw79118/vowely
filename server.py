from __future__ import annotations

import asyncio
import json
import random
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Tuple, List
from urllib.parse import parse_qs

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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

# CORS: allow GitHub Pages frontend + local dev to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://jasonw79118.github.io",
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def root():
    return FileResponse("static/index.html")


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
                           "endsAt":m.ends_at,"mode":("ranked" if is_ranked else "casual"),"band":band})
    if not use_bot:
        await hub.send(user2, {"type":"matchFound","matchId":m.match_id,"youAre":"b","opponent":m.a_name,"consonants":sorted(list(m.consonants)),
                               "endsAt":m.ends_at,"mode":("ranked" if is_ranked else "casual"),"band":band})
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