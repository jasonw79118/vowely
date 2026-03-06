const CLIENT_BUILD = window.__VOWELY_BUILD__ || "2026-03-06-4";
const FORCED_ROUND_SECONDS = 120;

function getPlayerId() {
  let pid = localStorage.getItem("vowely_player_id");
  if (!pid) {
    pid = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now());
    localStorage.setItem("vowely_player_id", pid);
  }
  return pid;
}

function getApiBase() {
  if (location.hostname.endsWith("github.io")) return "https://vowely.onrender.com";
  return "";
}

function getWsBase() {
  const proto = (location.protocol === "https:") ? "wss" : "ws";
  if (location.hostname.endsWith("github.io")) return `${proto}://vowely.onrender.com`;
  return `${proto}://${location.host}`;
}

let roundSeconds = FORCED_ROUND_SECONDS;
let ws;
let endsAt = 0;
let inMatch = false;
let matchId = null;
let youAre = null;
let playMode = "casual";
let leaderboardTimer = null;

const el = (id) => document.getElementById(id);

function appendLetter(ch) {
  const w = el("word");
  if (!w) return;
  w.value = (w.value || "") + String(ch || "").toUpperCase();
  w.focus();
}

function backspaceWord() {
  const w = el("word");
  if (!w) return;
  w.value = (w.value || "").slice(0, -1);
  w.focus();
}

function clearWord() {
  const w = el("word");
  if (!w) return;
  w.value = "";
  w.focus();
}

function makeTileButton(letter, cls) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `letter-tile ${cls || ""}`.trim();
  btn.textContent = letter;
  btn.addEventListener("click", () => appendLetter(letter));
  return btn;
}

function renderTileTrays(consonantsStr) {
  const vowels = ["A", "E", "I", "O", "U", "Y"];
  const vowelWrap = el("vowelTiles");
  if (vowelWrap) {
    vowelWrap.innerHTML = "";
    vowels.forEach((v) => vowelWrap.appendChild(makeTileButton(v, "vowel")));
  }

  const consonantWrap = el("consonantTiles");
  if (consonantWrap) {
    consonantWrap.innerHTML = "";
    const letters = String(consonantsStr || "")
      .toUpperCase()
      .replace(/[^A-Z]/g, "")
      .split("")
      .filter(Boolean);

    letters.forEach((c) => consonantWrap.appendChild(makeTileButton(c, "consonant")));
  }
}

function setText(id, txt) {
  const node = el(id);
  if (!node) return;
  node.textContent = (txt === undefined || txt === null || txt === "") ? "—" : String(txt);
}

function setDeltaText(id, delta) {
  const node = el(id);
  if (!node) return;
  if (delta === undefined || delta === null) {
    node.textContent = "";
    return;
  }
  const n = Number(delta) || 0;
  const sign = n > 0 ? "+" : "";
  node.textContent = `(${sign}${n})`;
}

function applyProfileUI(profile, pidForDisplay) {
  if (!profile) return;
  setText("ratingValue", profile.rating);
  setText("winsValue", profile.wins);
  setText("lossesValue", profile.losses);
  if (profile.tier !== undefined) setText("tierValue", profile.tier);

  const lr = profile.last_result ?? profile.lastResult ?? "";
  const ld = profile.last_delta ?? profile.lastDelta ?? 0;

  setText("lastResultValue", lr);
  setDeltaText("lastDeltaValue", ld);

  if (pidForDisplay) {
    const shortPid = pidForDisplay.length > 10 ? (pidForDisplay.slice(0, 8) + "…") : pidForDisplay;
    setText("playerIdValue", shortPid);
  }
}

function renderRecent(list) {
  const wrap = el("recentList");
  const empty = el("recentEmpty");
  if (!wrap || !empty) return;

  wrap.innerHTML = "";
  if (!Array.isArray(list) || list.length === 0) {
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";

  list.forEach((m) => {
    const when = m.played_at || m.playedAt || "";
    const opp = m.opponent || "—";
    const res = (m.result || "").toUpperCase() || "—";
    const delta = Number(m.delta ?? 0) || 0;
    const sign = delta > 0 ? "+" : "";
    const scoreFor = m.score_for ?? m.scoreFor ?? "";
    const scoreAgainst = m.score_against ?? m.scoreAgainst ?? "";
    const ratingAfter = m.rating_after ?? m.ratingAfter ?? null;

    const row = document.createElement("div");
    row.style.display = "flex";
    row.style.justifyContent = "space-between";
    row.style.gap = "10px";
    row.style.padding = "8px 10px";
    row.style.border = "1px solid #e5ecf4";
    row.style.borderRadius = "12px";
    row.style.background = "#ffffff";

    const left = document.createElement("div");
    left.style.display = "flex";
    left.style.flexDirection = "column";
    left.style.gap = "2px";

    const top = document.createElement("div");
    top.innerHTML = `<strong>${res}</strong> vs <strong>${opp}</strong>`;

    const mid = document.createElement("div");
    mid.className = "muted";
    mid.textContent = (scoreFor !== "" && scoreAgainst !== "") ? `Score: ${scoreFor}-${scoreAgainst}` : "";

    const bot = document.createElement("div");
    bot.className = "muted";
    bot.style.fontSize = "12px";
    bot.textContent = when;

    left.appendChild(top);
    if (mid.textContent) left.appendChild(mid);
    if (bot.textContent) left.appendChild(bot);

    const right = document.createElement("div");
    right.style.textAlign = "right";
    right.style.minWidth = "110px";
    right.innerHTML =
      `<div>Δ <strong>${sign}${delta}</strong></div>` +
      (ratingAfter !== null ? `<div class="muted">→ ${ratingAfter}</div>` : "");

    row.appendChild(left);
    row.appendChild(right);
    wrap.appendChild(row);
  });
}

async function refreshLeaderboard() {
  const box = el("leaderboardList");
  if (!box) return;
  try {
    const res = await fetch(`${getApiBase()}/api/leaderboard?limit=25&_=${encodeURIComponent(CLIENT_BUILD)}`, {
      cache: "no-store"
    });
    const data = await res.json();
    const items = (data && data.items) ? data.items : [];
    box.innerHTML = "";

    items.forEach((p) => {
      const row = document.createElement("div");
      row.style.display = "flex";
      row.style.justifyContent = "space-between";
      row.style.gap = "10px";
      row.style.padding = "8px 10px";
      row.style.borderBottom = "1px solid #e5ecf4";
      row.innerHTML =
        `<div><strong>#${p.rank}</strong> ${p.name} <span class="muted">(${p.tier})</span></div>` +
        `<div><strong>${p.rating}</strong> <span class="muted">${p.wins}-${p.losses}</span></div>`;
      box.appendChild(row);
    });

    if (items.length === 0) {
      const row = document.createElement("div");
      row.className = "muted";
      row.textContent = "No ranked players yet.";
      box.appendChild(row);
    }
  } catch (e) {}
}

function logFeed(text) {
  const box = el("feed");
  if (!box) return;
  const div = document.createElement("div");
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function logMyWord(text) {
  const box = el("mywords");
  if (!box) return;
  const div = document.createElement("div");
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function setStatus(s) {
  const node = el("status");
  if (node) node.textContent = s;
}

function setMatchPill(text) {
  const pill = el("matchPill");
  if (!pill) return;
  if (!text) {
    pill.style.display = "none";
    pill.textContent = "";
  } else {
    pill.style.display = "inline-flex";
    pill.textContent = text;
  }
}

async function syncConfig() {
  roundSeconds = FORCED_ROUND_SECONDS;
  try {
    await fetch(`${getApiBase()}/api/config?_=${encodeURIComponent(CLIENT_BUILD)}`, {
      cache: "no-store"
    });
  } catch (e) {}
}

function tick() {
  const timerNode = el("timer");
  if (!timerNode) return;
  if (!endsAt) {
    timerNode.textContent = "—";
    return;
  }

  const left = Math.max(0, Math.floor(endsAt - Date.now() / 1000));
  const mm = String(Math.floor(left / 60)).padStart(2, "0");
  const ss = String(left % 60).padStart(2, "0");
  timerNode.textContent = `${mm}:${ss}`;
}

setInterval(tick, 200);

function send(type, payload = {}) {
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type, ...payload }));
}

function resetUIForIdle() {
  inMatch = false;
  matchId = null;
  youAre = null;
  endsAt = 0;

  setText("opponent", "—");
  setText("cons", "—");
  setText("feedback", "");
  const mywords = el("mywords");
  if (mywords) mywords.innerHTML = "";
  setMatchPill(null);
  renderTileTrays("");

  const playBtn = el("play");
  const cancelBtn = el("cancel");
  if (playBtn) playBtn.disabled = false;
  if (cancelBtn) cancelBtn.disabled = true;
}

function forceLocalTimer() {
  roundSeconds = FORCED_ROUND_SECONDS;
  endsAt = (Date.now() / 1000) + FORCED_ROUND_SECONDS;
}

async function connect() {
  await syncConfig();

  const pid = encodeURIComponent(getPlayerId());
  ws = new WebSocket(`${getWsBase()}/ws?pid=${pid}&build=${encodeURIComponent(CLIENT_BUILD)}`);

  ws.onopen = () => {
    setStatus("CONNECTED");
    logFeed(`Connected (${CLIENT_BUILD}).`);
    refreshLeaderboard();
  };

  ws.onclose = () => {
    setStatus("DISCONNECTED");
    logFeed("Disconnected.");
    resetUIForIdle();
    setTimeout(connect, 1500);
  };

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);

    if (msg.type === "hello") {
      const nameInput = el("name");
      if (nameInput) nameInput.value = msg.name || "";
      setStatus("READY");

      const profile = msg.profile || {
        rating: msg.rating,
        wins: msg.wins,
        losses: msg.losses,
        last_delta: msg.last_delta,
        last_result: msg.last_result
      };

      applyProfileUI(profile, msg.pid || getPlayerId());
      if (profile.ranked_games !== undefined) setText("rankedGamesValue", profile.ranked_games);
      if (profile.casual_games !== undefined) setText("casualGamesValue", profile.casual_games);

      renderRecent(msg.recent || []);
      renderTileTrays("");
      return;
    }

    if (msg.type === "nameSet") {
      logFeed(`Name set to ${msg.name}.`);
      return;
    }

    if (msg.type === "searching") {
      const mode = msg.mode || playMode;
      setStatus(mode === "ranked" ? "SEARCHING (RANKED)…" : "SEARCHING (CASUAL)…");
      const playBtn = el("play");
      const cancelBtn = el("cancel");
      if (playBtn) playBtn.disabled = true;
      if (cancelBtn) cancelBtn.disabled = false;
      setMatchPill("Searching");
      return;
    }

    if (msg.type === "rankedSearchTimeout") {
      logFeed(`Ranked search taking longer than usual (${msg.seconds}s).`);
      return;
    }

    if (msg.type === "idle") {
      setStatus("READY");
      resetUIForIdle();
      return;
    }

    if (msg.type === "reconnected") {
      inMatch = true;
      matchId = msg.matchId;
      youAre = msg.youAre;
      forceLocalTimer();

      setStatus("IN MATCH");
      setMatchPill("Reconnected " + matchId.slice(0, 8));
      setText("opponent", msg.opponent || "Opponent");

      const consonants = (msg.consonants || []).join(" ").toUpperCase();
      setText("cons", consonants || "—");
      renderTileTrays(consonants);

      logFeed("Reconnected to match.");

      const cancelBtn = el("cancel");
      const playBtn = el("play");
      if (cancelBtn) cancelBtn.disabled = true;
      if (playBtn) playBtn.disabled = true;
      return;
    }

    if (msg.type === "matchFound") {
      inMatch = true;
      matchId = msg.matchId;
      youAre = msg.youAre;
      forceLocalTimer();

      setStatus("IN MATCH");
      const mMode = msg.mode || playMode;
      const band = (msg.band !== undefined && msg.band !== null) ? `±${msg.band}` : "";
      setMatchPill((mMode === "ranked" ? "Ranked" : "Casual") + " " + matchId.slice(0, 8) + (band ? (" " + band) : ""));

      setText("opponent", msg.opponent || "Opponent");

      const consonants = (msg.consonants || []).join(" ").toUpperCase();
      setText("cons", consonants || "—");
      renderTileTrays(consonants);

      const mywords = el("mywords");
      if (mywords) mywords.innerHTML = "";
      logFeed(`Match found vs ${msg.opponent}. Go!`);

      const cancelBtn = el("cancel");
      const playBtn = el("play");
      if (cancelBtn) cancelBtn.disabled = true;
      if (playBtn) playBtn.disabled = true;

      const nameInput = el("name");
      if (youAre === "a") {
        setText("aName", (nameInput && nameInput.value) || "You");
        setText("bName", msg.opponent || "Opponent");
      } else {
        setText("aName", msg.opponent || "Opponent");
        setText("bName", (nameInput && nameInput.value) || "You");
      }
      return;
    }

    if (msg.type === "score") {
      setText("aName", msg.a.name);
      setText("bName", msg.b.name);
      setText("aScore", msg.a.score);
      setText("bScore", msg.b.score);
      return;
    }

    if (msg.type === "accept") {
      setText("feedback", `Accepted (+${msg.points})`);
      logMyWord(`${msg.word} (+${msg.points})`);
      return;
    }

    if (msg.type === "reject") {
      setText("feedback", `Nope: ${msg.reason}`);
      return;
    }

    if (msg.type === "cheer") {
      logFeed(`${msg.from}: ${msg.text}`);
      return;
    }

    if (msg.type === "result") {
      setText("ratingValue", msg.rating);
      setText("winsValue", msg.wins);
      setText("lossesValue", msg.losses);
      if (msg.tier !== undefined) setText("tierValue", msg.tier);
      setText("lastResultValue", msg.result || "—");
      setDeltaText("lastDeltaValue", msg.delta ?? 0);
      if (Array.isArray(msg.recent)) renderRecent(msg.recent);
      return;
    }

    if (msg.type === "matchEnd") {
      const result = msg.winner ? `Winner: ${msg.winner}` : "Tie!";
      const reason = msg.endedReason ? ` (${msg.endedReason})` : "";
      logFeed(`Match ended. ${result}${reason}`);
      setStatus("READY");
      resetUIForIdle();
      const playBtn = el("play");
      if (playBtn) playBtn.disabled = false;
    }
  };
}

const setNameBtn = el("setName");
if (setNameBtn) {
  setNameBtn.onclick = () => send("setName", { name: el("name").value });
}

const playBtn = el("play");
if (playBtn) {
  playBtn.onclick = () => send("play", { mode: playMode });
}

const cancelBtn = el("cancel");
if (cancelBtn) {
  cancelBtn.onclick = () => send("cancelSearch");
}

const submitBtn = el("submit");
if (submitBtn) {
  submitBtn.onclick = () => {
    const wordInput = el("word");
    const w = wordInput ? wordInput.value.trim() : "";
    if (!w) return;
    if (wordInput) wordInput.value = "";
    send("submit", { word: w });
  };
}

const wordInput = el("word");
if (wordInput) {
  wordInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && submitBtn) {
      submitBtn.click();
    }
  });
}

document.querySelectorAll(".cheer").forEach((btn) => {
  btn.addEventListener("click", () => {
    send("cheer", { token: btn.getAttribute("data-token") });
  });
});

window.addEventListener("DOMContentLoaded", () => {
  const modeSel = el("modeSelect");
  if (modeSel) {
    playMode = (modeSel.value || "casual");
    modeSel.addEventListener("change", () => {
      playMode = (modeSel.value || "casual");
    });
  }

  const againBtn = el("playAgain");
  if (againBtn) {
    againBtn.addEventListener("click", () => send("play", { mode: playMode }));
  }

  const refreshBtn = el("refreshLeaderboard");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", refreshLeaderboard);
  }

  if (!leaderboardTimer) {
    leaderboardTimer = setInterval(refreshLeaderboard, 15000);
  }

  const backBtn = el("backspaceWord");
  if (backBtn) {
    backBtn.addEventListener("click", backspaceWord);
  }

  const clearBtn = el("clearWord");
  if (clearBtn) {
    clearBtn.addEventListener("click", clearWord);
  }

  const btn = el("toggleRecentBtn");
  const list = el("recentList");
  const empty = el("recentEmpty");
  if (btn && list && empty) {
    let shown = true;
    btn.addEventListener("click", () => {
      shown = !shown;
      list.style.display = shown ? "flex" : "none";
      empty.style.display = shown ? empty.style.display : "none";
      btn.textContent = shown ? "Hide" : "Show";
    });
  }

  renderTileTrays("");
});

connect();
