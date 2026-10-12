// EzyGamers stream overlay controller. Python calls window.ezOverlay.*.
(() => {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const GAME_W = Number(params.get("w")) || 540;
  const GAME_H = Number(params.get("h")) || 900;
  const fmt = (n) => Number(n || 0).toLocaleString("en-US");
  const clock = (s) => {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const mm = h ? String(m).padStart(2, "0") : m;
    return (h ? h + ":" : "") + mm + ":" + String(sec).padStart(2, "0");
  };

  // ---- fit the game's phone-sized viewport into the stage area ----
  const iframe = $("game");
  const wrap = $("frame-wrap");
  iframe.style.width = GAME_W + "px";
  iframe.style.height = GAME_H + "px";
  const FRAME_BORDER = 4;   // #frame-wrap border width (css)
  const FRAME_MARGIN = 10;  // room for the glow ring
  function fit() {
    const stage = $("stage");
    const room = 2 * (FRAME_BORDER + FRAME_MARGIN);
    const scale = Math.min((stage.clientWidth - room) / GAME_W, (stage.clientHeight - room) / GAME_H);
    if (!(scale > 0)) return;
    iframe.style.transform = `scale(${scale})`;
    wrap.style.width = Math.floor(GAME_W * scale) + 2 * FRAME_BORDER + "px";
    wrap.style.height = Math.floor(GAME_H * scale) + 2 * FRAME_BORDER + "px";
  }
  fit();
  // Re-fit whenever the space changes: web fonts loading, the challenge
  // panel appearing/disappearing, window resize.
  new ResizeObserver(fit).observe($("stage"));
  if (document.fonts) document.fonts.ready.then(fit);

  // ---- animated numbers ----
  const shown = { score: 0 };
  let scoreAnim = null;
  function animateScore(target) {
    if (target === shown.score) return;
    const from = shown.score, start = performance.now(), dur = target > from ? 450 : 0;
    cancelAnimationFrame(scoreAnim);
    const tick = (t) => {
      const k = dur ? Math.min(1, (t - start) / dur) : 1;
      shown.score = Math.round(from + (target - from) * (1 - Math.pow(1 - k, 3)));
      $("score").textContent = fmt(shown.score);
      if (k < 1) scoreAnim = requestAnimationFrame(tick);
    };
    scoreAnim = requestAnimationFrame(tick);
    // Set the final value immediately too, so the DOM is always correct even
    // if animation frames are throttled (e.g. headless capture).
    $("score").dataset.value = target;
  }
  function setText(id, text, bump) {
    const el = $(id);
    if (el.textContent === String(text)) return;
    el.textContent = text;
    if (bump) { el.classList.remove("bump"); void el.offsetWidth; el.classList.add("bump"); }
  }

  // ---- temporary messages (one at a time, queued) ----
  const queue = [];
  let showing = false;
  function nextToast() {
    if (showing || !queue.length) return;
    showing = true;
    const { text, kind, ms } = queue.shift();
    const el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.textContent = text;
    $("toasts").appendChild(el);
    setTimeout(() => {
      el.classList.add("out");
      setTimeout(() => { el.remove(); showing = false; nextToast(); }, 300);
    }, ms || 2200);
  }

  window.ezOverlay = {
    configured: false,
    lastStatus: null,

    configure(cfg) {
      if (cfg.title) $("title").textContent = cfg.title;
      if (cfg.tagline) $("tagline").textContent = cfg.tagline;
      if (cfg.cta !== undefined) $("cta").textContent = cfg.cta;
      this.configured = true;
    },

    update(s) {
      this.lastStatus = s;
      animateScore(s.score || 0);
      const lvl = s.goal_level ? Math.min(s.level || 1, s.goal_level) : (s.level || 1);
      setText("level", lvl, true);
      setText("level-goal", s.goal_level ? "/" + s.goal_level : "");
      setText("combo", "x" + (s.combo || 0), (s.combo || 0) > 0);
      $("combo-card").classList.toggle("hot", (s.combo || 0) >= 10);
      setText("record", fmt(s.high_score));
      setText("timer", clock(s.run_seconds));
      setText("game-name", (s.game || "").toUpperCase());
      setText("mode", (s.mode || "").toUpperCase() + " MODE");
      $("progress-fill").style.width = Math.round((s.progress || 0) * 100) + "%";

      const ch = s.challenge;
      const box = $("challenge");
      box.hidden = !ch;
      if (ch) {
        setText("challenge-name", "CHALLENGE: " + ch.name);
        box.classList.toggle("done", ch.state === "completed");
        box.classList.toggle("failed", ch.state === "failed");
        let status = "";
        if (ch.state === "completed") status = "✔ DONE";
        else if (ch.state === "failed") status = "✖ FAILED";
        else if (ch.time_limit_s) status = clock(ch.time_limit_s - (s.run_seconds || 0)) + " left";
        setText("challenge-status", status);
      }
    },

    message(text, kind, ms, urgent) {
      if (urgent) queue.length = 0;  // celebrations skip the queue
      queue.push({ text, kind, ms });
      // Never let a burst of events build a long backlog of stale messages.
      while (queue.length > 3) queue.shift();
      nextToast();
    },
  };
})();
