const CLIENT_BUILD = window.__VOWELY_BUILD__ || "2026-03-06-5";
const FORCED_ROUND_SECONDS = 120;

let roundSeconds = FORCED_ROUND_SECONDS;
let ws;
let endsAt = 0;
let inMatch = false;
let matchId = null;
let youAre = null;
let playMode = "casual";
let leaderboardTimer = null;
let authState = { authenticated: false, profile: null, recent: [] };
let reconnectScheduled = false;

const el = (id) => document.getElementById(id);

function getPlayerId() {
  let pid = localStorage.getItem("vowely_player_id");
  if (!pid) {
    pid = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now());
    localStorage.setItem("vowely_player_id", pid);
  }
  return pid;
}

function setPlayerId(pid) {
  if (!pid) return;
  localStorage.setItem("vowely_player_id", String(pid));
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

async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-Guest-Player-Id", getPlayerId());
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const res = await fetch(`${getApiBase()}${path}`, {
    ...options,
    headers,
    credentials: "include",
    cache: "no-store",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.error || data.detail || "Request failed.");
  }
  return data;
}

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

function appendLetter(ch) {
  const w = el("word");
  if (!w) return;
  w.value = (w.value || "") + String(ch || "").toUpperCase();
}

function backspaceWord() {
  const w = el("word");
  if (!w) return;
  w.value = (w.value || "").slice(0, -1);
}

function clearWord() {
  const w = el("word");
  if (!w) return;
  w.value = "";
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
    vowels.forEach(v => vowelWrap.appendChild(makeTileButton(v, "vowel")));
  }
  const consonantWrap = el("consonantTiles");
  if (consonantWrap) {
    consonantWrap.innerHTML = "";
    const letters = String(consonantsStr || "").toUpperCase().replace(/[^A-Z]/g, "").split("").filter(Boolean);
    letters.forEach(c => consonantWrap.appendChild(makeTileButton(c, "consonant")));
  }
}

function applyProfileUI(profile, pidForDisplay) {
  if (!profile) return;
  setText("ratingValue", profile.rating);
  setText("winsValue", profile.wins);
  setText("lossesValue", profile.losses);
  if (profile.tier !== undefined) setText("tierValue", profile.tier);
  setText("rankedGamesValue", profile.rankedGames ?? profile.ranked_games ?? 0);
  setText("casualGamesValue", profile.casualGames ?? profile.casual_games ?? 0);
  setText("lastResultValue", profile.lastResult ?? profile.last_result ?? "");
  setDeltaText("lastDeltaValue", profile.lastDelta ?? profile.last_delta ?? 0);
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
    const row = document.createElement("div");
    row.style.display = "flex";
    row.style.justifyContent = "space-between";
    row.style.gap = "10px";
    row.style.padding = "8px 10px";
    row.style.border = "1px solid #e5ecf4";
    row.style.borderRadius = "12px";
    row.style.background = "#ffffff";
    row.innerHTML = `<div><div><strong>${res}</strong> vs <strong>${opp}</strong></div><div class="muted">${scoreFor !== "" ? `Score: ${scoreFor}-${scoreAgainst}` : ""}</div><div class="muted" style="font-size:12px;">${when}</div></div><div style="text-align:right; min-width:110px;"><div>Δ <strong>${sign}${delta}</strong></div></div>`;
    wrap.appendChild(row);
  });
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

function setAuthMessage(error = "", success = "") {
  setText("authError", error || "");
  setText("authSuccess", success || "");
}

function showAuthModal(tab = "signup") {
  const backdrop = el("authBackdrop");
  if (backdrop) backdrop.classList.add("show");
  switchAuthTab(tab);
}

function hideAuthModal() {
  const backdrop = el("authBackdrop");
  if (backdrop) backdrop.classList.remove("show");
  setAuthMessage("", "");
}

function switchAuthTab(tab) {
  const signupTab = el("tabSignup");
  const loginTab = el("tabLogin");
  const signupPane = el("signupPane");
  const loginPane = el("loginPane");
  if (signupTab) signupTab.classList.toggle("active", tab === "signup");
  if (loginTab) loginTab.classList.toggle("active", tab === "login");
  if (signupPane) signupPane.classList.toggle("active", tab === "signup");
  if (loginPane) loginPane.classList.toggle("active", tab === "login");
  setAuthMessage("", "");
}

function updateAccountUI() {
  const profile = authState.profile;
  const isAuth = !!authState.authenticated && profile;
  const accountName = el("accountName");
  const accountSub = el("accountSub");
  const accountAvatar = el("accountAvatar");
  const accountSummary = el("accountSummary");
  const guestBanner = el("guestBanner");
  const loginBtn = el("openLoginBtn");
  const signupBtn = el("openSignupBtn");
  const logoutBtn = el("logoutBtn");
  const openUpgradeBtn = el("openUpgradeBtn");

  if (isAuth) {
    accountName.textContent = profile.displayName || profile.username || "Player";
    accountSub.textContent = profile.username ? `@${profile.username}` : "Signed in";
    accountAvatar.textContent = String((profile.displayName || profile.username || "P").slice(0, 1)).toUpperCase();
    accountSummary.textContent = `Signed in as ${profile.displayName || profile.username}. Your stats now follow you across devices.`;
    guestBanner.classList.remove("show");
    if (loginBtn) loginBtn.style.display = "none";
    if (signupBtn) signupBtn.style.display = "none";
    if (logoutBtn) logoutBtn.style.display = "inline-flex";
    if (openUpgradeBtn) openUpgradeBtn.textContent = "Edit Profile";
  } else {
    accountName.textContent = profile?.displayName || "Guest";
    accountSub.textContent = "Playing on this device";
    accountAvatar.textContent = "G";
    accountSummary.textContent = "Guest mode active. Create an account to keep your stats across devices.";
    guestBanner.classList.add("show");
    if (loginBtn) loginBtn.style.display = "inline-flex";
    if (signupBtn) signupBtn.style.display = "inline-flex";
    if (logoutBtn) logoutBtn.style.display = "none";
    if (openUpgradeBtn) openUpgradeBtn.textContent = "Save Progress";
  }
}

async function refreshLeaderboard() {
  const box = el("leaderboardList");
  if (!box) return;
  try {
    const res = await fetch(`${getApiBase()}/api/leaderboard?limit=25&_=${encodeURIComponent(CLIENT_BUILD)}`, { cache: "no-store" });
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
      row.innerHTML = `<div><strong>#${p.rank}</strong> ${p.name} <span class="muted">(${p.tier})</span></div><div><strong>${p.rating}</strong> <span class="muted">${p.wins}-${p.losses}</span></div>`;
      box.appendChild(row);
    });
    if (items.length === 0) {
      const row = document.createElement("div");
      row.className = "muted";
      row.textContent = "No ranked players yet.";
      box.appendChild(row);
    }
  } catch (_) {}
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

async function loadAuthState() {
  try {
    const data = await apiFetch("/api/me");
    authState = {
      authenticated: !!data.authenticated,
      profile: data.profile || null,
      recent: data.recent || [],
    };
    if (authState.profile?.userId && authState.authenticated && authState.profile.userId !== getPlayerId()) {
      setPlayerId(authState.profile.userId);
      reconnectSocket();
    }
    if (authState.profile) {
      applyProfileUI(authState.profile, authState.profile.userId || getPlayerId());
      renderRecent(authState.recent || []);
      const nameInput = el("name");
      if (nameInput && authState.profile.displayName) nameInput.value = authState.profile.displayName;
    }
    updateAccountUI();
  } catch (err) {
    console.error(err);
  }
}

async function handleSignupOrUpgrade() {
  const payload = {
    displayName: (el("signupDisplayName")?.value || "").trim(),
    username: (el("signupUsername")?.value || "").trim(),
    email: (el("signupEmail")?.value || "").trim(),
    password: (el("signupPassword")?.value || "").trim(),
  };
  try {
    const path = authState.authenticated ? "/api/me" : "/api/auth/upgrade-guest";
    let data;
    if (!authState.authenticated && authState.profile?.isGuest !== false) {
      data = await apiFetch("/api/auth/upgrade-guest", { method: "POST", body: JSON.stringify(payload) });
    } else {
      data = await apiFetch("/api/auth/signup", { method: "POST", body: JSON.stringify(payload) });
    }
    authState = { authenticated: true, profile: data.profile, recent: data.recent || [] };
    if (data.profile?.userId) setPlayerId(data.profile.userId);
    setAuthMessage("", "Account ready. Your progress is now saved.");
    updateAccountUI();
    applyProfileUI(data.profile, data.profile?.userId || getPlayerId());
    renderRecent(data.recent || []);
    reconnectSocket();
    setTimeout(hideAuthModal, 500);
  } catch (err) {
    setAuthMessage(err.message || "Could not create account.", "");
  }
}

async function handleLogin() {
  const payload = {
    emailOrUsername: (el("loginIdentity")?.value || "").trim(),
    password: (el("loginPassword")?.value || "").trim(),
  };
  try {
    const data = await apiFetch("/api/auth/login", { method: "POST", body: JSON.stringify(payload) });
    authState = { authenticated: true, profile: data.profile, recent: data.recent || [] };
    if (data.profile?.userId) setPlayerId(data.profile.userId);
    updateAccountUI();
    applyProfileUI(data.profile, data.profile?.userId || getPlayerId());
    renderRecent(data.recent || []);
    setAuthMessage("", "Logged in.");
    reconnectSocket();
    setTimeout(hideAuthModal, 500);
  } catch (err) {
    setAuthMessage(err.message || "Could not log in.", "");
  }
}

async function handleLogout() {
  try {
    await apiFetch("/api/auth/logout", { method: "POST" });
  } catch (_) {}
  authState = { authenticated: false, profile: authState.profile, recent: authState.recent || [] };
  updateAccountUI();
  hideAuthModal();
}

function forceLocalTimer() {
  roundSeconds = FORCED_ROUND_SECONDS;
  endsAt = (Date.now() / 1000) + FORCED_ROUND_SECONDS;
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

function reconnectSocket() {
  if (reconnectScheduled) return;
  reconnectScheduled = true;
  setTimeout(() => {
    reconnectScheduled = false;
    try {
      if (ws) ws.close();
    } catch (_) {}
  }, 50);
}

async function syncConfig() {
  roundSeconds = FORCED_ROUND_SECONDS;
  try { await fetch(`${getApiBase()}/api/config?_=${encodeURIComponent(CLIENT_BUILD)}`, { cache: "no-store" }); } catch (_) {}
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
      const currentProfile = authState.profile || {};
      const profile = {
        ...currentProfile,
        rating: msg.profile?.rating ?? msg.rating,
        wins: msg.profile?.wins ?? msg.wins,
        losses: msg.profile?.losses ?? msg.losses,
        tier: msg.profile?.tier ?? currentProfile.tier,
        rankedGames: msg.profile?.ranked_games ?? msg.profile?.rankedGames ?? currentProfile.rankedGames ?? 0,
        casualGames: msg.profile?.casual_games ?? msg.profile?.casualGames ?? currentProfile.casualGames ?? 0,
        lastResult: msg.profile?.last_result ?? msg.profile?.lastResult ?? currentProfile.lastResult ?? "",
        lastDelta: msg.profile?.last_delta ?? msg.profile?.lastDelta ?? currentProfile.lastDelta ?? 0,
        displayName: currentProfile.displayName || msg.name,
        userId: currentProfile.userId || msg.userId || msg.pid,
        isGuest: currentProfile.isGuest ?? true,
      };
      authState.profile = profile;
      applyProfileUI(profile, profile.userId || getPlayerId());
      renderRecent(authState.recent?.length ? authState.recent : (msg.recent || []));
      setStatus("READY");
      const nameInput = el("name");
      if (nameInput && profile.displayName) nameInput.value = profile.displayName;
      renderTileTrays("");
      updateAccountUI();
      return;
    }

    if (msg.type === "nameSet") {
      logFeed(`Name set to ${msg.name}.`);
      if (authState.profile) authState.profile.displayName = msg.name;
      updateAccountUI();
      return;
    }

    if (msg.type === "searching") {
      const mode = msg.mode || playMode;
      setStatus(mode === "ranked" ? "STARTING RANKED…" : "SEARCHING (CASUAL)…");
      el("play").disabled = true;
      el("cancel").disabled = false;
      setMatchPill("Searching");
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
      el("cancel").disabled = true;
      el("play").disabled = true;
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
      el("cancel").disabled = true;
      el("play").disabled = true;
      if (youAre === "a") {
        setText("aName", el("name").value || "You");
        setText("bName", msg.opponent || "Opponent");
      } else {
        setText("aName", msg.opponent || "Opponent");
        setText("bName", el("name").value || "You");
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
      if (authState.profile) {
        authState.profile.rating = msg.rating;
        authState.profile.wins = msg.wins;
        authState.profile.losses = msg.losses;
        if (msg.tier !== undefined) authState.profile.tier = msg.tier;
        authState.profile.lastResult = msg.result || "";
        authState.profile.lastDelta = msg.delta ?? 0;
      }
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
      const missed = Array.isArray(msg.missedWords)
        ? msg.missedWords
        : (youAre === "a" ? (msg.missedWordsA || []) : (msg.missedWordsB || []));
      if (Array.isArray(missed) && missed.length) {
        logFeed(`Words you missed: ${missed.join(", ")}`);
      }
      if (msg.pendingMessage) {
        logFeed(msg.pendingMessage);
      }
      setStatus("READY");
      resetUIForIdle();
      el("play").disabled = false;
    }
  };
}

window.addEventListener("DOMContentLoaded", async () => {
  const modeSel = el("modeSelect");
  if (modeSel) {
    playMode = (modeSel.value || "casual");
    modeSel.addEventListener("change", () => { playMode = (modeSel.value || "casual"); });
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
  el("word").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); el("submit").click(); } });
  el("playAgain").addEventListener("click", () => send("play", { mode: playMode }));
  el("refreshLeaderboard").addEventListener("click", refreshLeaderboard);
  if (!leaderboardTimer) leaderboardTimer = setInterval(refreshLeaderboard, 15000);
  el("backspaceWord").addEventListener("click", backspaceWord);
  el("clearWord").addEventListener("click", clearWord);

  document.querySelectorAll(".cheer").forEach((btn) => {
    btn.addEventListener("click", () => {
      const token = btn.getAttribute("data-token");
      send("cheer", { token });
    });
  });

  const toggleRecent = el("toggleRecentBtn");
  if (toggleRecent) {
    let shown = true;
    toggleRecent.addEventListener("click", () => {
      shown = !shown;
      el("recentList").style.display = shown ? "flex" : "none";
      el("recentEmpty").style.display = shown ? el("recentEmpty").style.display : "none";
      toggleRecent.textContent = shown ? "Hide" : "Show";
    });
  }

  el("openLoginBtn").addEventListener("click", () => showAuthModal("login"));
  el("openSignupBtn").addEventListener("click", () => showAuthModal("signup"));
  el("bannerLoginBtn").addEventListener("click", () => showAuthModal("login"));
  el("bannerSignupBtn").addEventListener("click", () => showAuthModal("signup"));
  el("openUpgradeBtn").addEventListener("click", () => showAuthModal("signup"));
  el("openFriendsBtn").addEventListener("click", async () => {
    try {
      const data = await apiFetch("/api/friends");
      const count = Array.isArray(data.friends) ? data.friends.length : 0;
      alert(`Friends foundation is connected. Current friends: ${count}.`);
    } catch (err) {
      alert(err.message || "Log in first to use friends.");
    }
  });
  el("logoutBtn").addEventListener("click", handleLogout);
  el("closeAuthBtn").addEventListener("click", hideAuthModal);
  el("tabSignup").addEventListener("click", () => switchAuthTab("signup"));
  el("tabLogin").addEventListener("click", () => switchAuthTab("login"));
  el("signupBtn").addEventListener("click", handleSignupOrUpgrade);
  el("loginBtn").addEventListener("click", handleLogin);
  el("authBackdrop").addEventListener("click", (e) => { if (e.target.id === "authBackdrop") hideAuthModal(); });

  renderTileTrays("");
  await loadAuthState();
  await refreshLeaderboard();
  connect();
});
