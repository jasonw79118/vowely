function getPlayerId() {
  let pid = localStorage.getItem("vowely_player_id");
  if (!pid) {
    pid = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now());
    localStorage.setItem("vowely_player_id", pid);
  }
  return pid;
}

let ws;
let endsAt = 0;
let inMatch = false;
let matchId = null;
let youAre = null;
let playMode = "casual";
let leaderboardTimer = null;

const el = (id) => document.getElementById(id);


function setText(id, txt) {
  const node = el(id);
  if (!node) return;
  node.textContent = (txt === undefined || txt === null || txt === "") ? "—" : String(txt);
}

function setDeltaText(id, delta) {
  const node = el(id);
  if (!node) return;
  if (delta === undefined || delta === null) { node.textContent = ""; return; }
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
    row.style.padding = "6px 8px";
    row.style.border = "1px solid #eee";
    row.style.borderRadius = "10px";
    row.style.background = "#fafafa";

    const left = document.createElement("div");
    left.style.display = "flex";
    left.style.flexDirection = "column";
    left.style.gap = "2px";

    const top = document.createElement("div");
    top.innerHTML = `<strong>${res}</strong> vs <strong>${opp}</strong>`;

    const mid = document.createElement("div");
    mid.className = "muted";
    if (scoreFor !== "" && scoreAgainst !== "") {
      mid.textContent = `Score: ${scoreFor}-${scoreAgainst}`;
    } else {
      mid.textContent = "";
    }

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
    const res = await fetch(`/api/leaderboard?limit=25`, { cache: "no-store" });
    const data = await res.json();
    const items = (data && data.items) ? data.items : [];
    box.innerHTML = "";
    items.forEach((p) => {
      const row = document.createElement("div");
      row.style.display = "flex";
      row.style.justifyContent = "space-between";
      row.style.gap = "10px";
      row.style.padding = "6px 8px";
      row.style.borderBottom = "1px solid #eee";
      row.innerHTML = `<div><strong>#${p.rank}</strong> ${p.name} <span class="muted">(${p.tier})</span></div>` +
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
  const div = document.createElement("div");
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function logMyWord(text) {
  const box = el("mywords");
  const div = document.createElement("div");
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function setStatus(s) {
  el("status").textContent = s;
}

function setMatchPill(text) {
  const pill = el("matchPill");
  if (!text) {
    pill.style.display = "none";
    pill.textContent = "";
  } else {
    pill.style.display = "inline-block";
    pill.textContent = text;
  }
}

function tick() {
  if (!endsAt) {
    el("timer").textContent = "—";
    return;
  }
  const left = Math.max(0, Math.floor(endsAt - Date.now() / 1000));
  const mm = String(Math.floor(left / 60)).padStart(2, "0");
  const ss = String(left % 60).padStart(2, "0");
  el("timer").textContent = `${mm}:${ss}`;
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

  el("opponent").textContent = "—";
  el("cons").textContent = "—";
  el("feedback").textContent = "";
  el("mywords").innerHTML = "";
  setMatchPill(null);

  el("play").disabled = false;
  el("cancel").disabled = true;
}

function connect() {
  const pid = encodeURIComponent(getPlayerId());
  const proto = (location.protocol === "https:") ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws?pid=${pid}`);

  ws.onopen = () => {
    setStatus("CONNECTED");
    logFeed("Connected.");
    refreshLeaderboard();
  };

  ws.onclose = () => {
    setStatus("DISCONNECTED");
    logFeed("Disconnected.");
    resetUIForIdle();
  };

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);

    if (msg.type === "hello") {
      el("name").value = msg.name || "";
      setStatus("READY");
      // Phase 2 profile + recent
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
      return;
    }

    if (msg.type === "nameSet") {
      logFeed(`Name set to ${msg.name}.`);
      return;
    }

    if (msg.type === "searching") {
      const mode = msg.mode || playMode;
      setStatus(mode === "ranked" ? "SEARCHING (RANKED)…" : "SEARCHING (CASUAL)…");
      el("play").disabled = true;
      el("cancel").disabled = false;
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
      endsAt = msg.endsAt;
      setStatus("IN MATCH");
      setMatchPill("Reconnected " + matchId.slice(0, 8));
      el("opponent").textContent = msg.opponent || "Opponent";
      el("cons").textContent = (msg.consonants || []).join(" ").toUpperCase();
      logFeed("Reconnected to match.");
      el("cancel").disabled = true;
      el("play").disabled = true;
      return;
    }

    if (msg.type === "matchFound") {
      inMatch = true;
      matchId = msg.matchId;
      youAre = msg.youAre;
      endsAt = msg.endsAt;

      setStatus("IN MATCH");
      const mMode = msg.mode || playMode;
      const band = (msg.band !== undefined && msg.band !== null) ? `±${msg.band}` : "";
      setMatchPill((mMode === "ranked" ? "Ranked" : "Casual") + " " + matchId.slice(0, 8) + (band ? (" " + band) : ""));

      el("opponent").textContent = msg.opponent || "Opponent";
      el("cons").textContent = (msg.consonants || []).join(" ").toUpperCase();

      el("mywords").innerHTML = "";
      logFeed(`Match found vs ${msg.opponent}. Go!`);
      el("cancel").disabled = true;
      el("play").disabled = true;

      // Initialize score labels
      if (youAre === "a") {
        el("aName").textContent = el("name").value || "You";
        el("bName").textContent = msg.opponent || "Opponent";
      } else {
        el("aName").textContent = msg.opponent || "Opponent";
        el("bName").textContent = el("name").value || "You";
      }
      return;
    }

    if (msg.type === "score") {
      // msg.a and msg.b are fixed "a" and "b" sides
      el("aName").textContent = msg.a.name;
      el("bName").textContent = msg.b.name;
      el("aScore").textContent = msg.a.score;
      el("bScore").textContent = msg.b.score;
      return;
    }

    if (msg.type === "accept") {
      el("feedback").textContent = `Accepted (+${msg.points})`;
      logMyWord(`${msg.word} (+${msg.points})`);
      return;
    }

    if (msg.type === "reject") {
      el("feedback").textContent = `Nope: ${msg.reason}`;
      return;
    }

    if (msg.type === "cheer") {
      logFeed(`${msg.from}: ${msg.text}`);
      return;
    }

    if (msg.type === "result") {
      // Phase 2 authoritative profile update after match
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
      const a = msg.a, b = msg.b;
      let result = "Tie!";
      if (msg.winner) result = `Winner: ${msg.winner}`;
      const reason = msg.endedReason ? ` (${msg.endedReason})` : "";
      logFeed(`Match ended. ${result}${reason}`);

      // show status and enable play again
      setStatus("READY");
      el("play").disabled = false;
      setMatchPill(null);
      return;
    }
  };
}

el("setName").onclick = () => send("setName", { name: el("name").value });
el("play").onclick = () => send("play", { mode: playMode });
el("cancel").onclick = () => send("cancelSearch");

el("submit").onclick = () => {
  const w = el("word").value.trim();
  if (!w) return;
  el("word").value = "";
  send("submit", { word: w });
};

el("word").addEventListener("keydown", (e) => {
  if (e.key === "Enter") el("submit").click();
});

document.querySelectorAll(".cheer").forEach((btn) => {
  btn.addEventListener("click", () => {
    const token = btn.getAttribute("data-token");
    send("cheer", { token });
  });
});


window.addEventListener("DOMContentLoaded", () => {
  const modeSel = el("modeSelect");
  if (modeSel) {
    playMode = (modeSel.value || "casual");
    modeSel.addEventListener("change", () => { playMode = (modeSel.value || "casual"); });
  }
  const againBtn = el("playAgain");
  if (againBtn) againBtn.addEventListener("click", () => send("play", { mode: playMode }));
  const refreshBtn = el("refreshLeaderboard");
  if (refreshBtn) refreshBtn.addEventListener("click", refreshLeaderboard);
  if (!leaderboardTimer) leaderboardTimer = setInterval(refreshLeaderboard, 15000);

  const btn = el("toggleRecentBtn");
  const list = el("recentList");
  const empty = el("recentEmpty");
  if (!btn || !list || !empty) return;

  let shown = true;
  btn.addEventListener("click", () => {
    shown = !shown;
    list.style.display = shown ? "flex" : "none";
    empty.style.display = shown ? empty.style.display : "none";
    btn.textContent = shown ? "Hide" : "Show";
  });
});


connect();
