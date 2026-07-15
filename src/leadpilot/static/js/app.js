/* LeadPilot workspace JS — deliberately small. Everything interactive
   is htmx partial swaps; this file only contains the four sanctioned
   vanilla-JS exceptions (design spec v001 §7) plus localStorage-backed
   preferences (§8 items 2–3, resolved as localStorage for Phase 1):

     1. navigator.clipboard.writeText() — initiate_lead_call approval
     2. Panel drag-resize (widths persisted per browser)
     3. Web Audio click sounds (synthesized, no audio files; default glass-tap per Marc 2026-07-15)
     4. Click-feedback glow-ring class toggle

   Anything beyond these is scope creep — flag it, don't add it. */

(function () {
  "use strict";

  var PREFS_KEY = "leadpilot.prefs";

  function loadPrefs() {
    try {
      return JSON.parse(localStorage.getItem(PREFS_KEY)) || {};
    } catch (e) {
      return {};
    }
  }

  function savePrefs(prefs) {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  }

  function setPref(key, value) {
    var prefs = loadPrefs();
    prefs[key] = value;
    savePrefs(prefs);
  }

  /* ---- Theme / pattern / sound boot ------------------------------- */
  /* data-theme is also set by an inline <head> script in base.html
     before first paint (no theme flash); this re-applies defensively
     and wires the settings popover. */

  var PATTERNS = ["orbs", "grid", "weave", "corner", "none"];

  function applyPrefs() {
    var prefs = loadPrefs();
    var root = document.documentElement;
    root.setAttribute("data-theme", prefs.theme || "cool-blue");
    /* Unknown stored values (e.g. the retired "aurora") fall back to
       the default orbs layer rather than an unstyled background. */
    var pattern = PATTERNS.indexOf(prefs.pattern) >= 0 ? prefs.pattern : "orbs";
    root.setAttribute("data-pattern", pattern);
    document.querySelectorAll("[data-set-theme]").forEach(function (el) {
      el.classList.toggle("selected", el.getAttribute("data-set-theme") === (prefs.theme || "cool-blue"));
    });
    document.querySelectorAll("[data-set-pattern]").forEach(function (el) {
      el.classList.toggle("selected", el.getAttribute("data-set-pattern") === pattern);
    });
    var soundSel = document.getElementById("sound-select");
    if (soundSel) soundSel.value = prefs.sound || "glass-tap";
  }

  document.addEventListener("click", function (ev) {
    var themeEl = ev.target.closest("[data-set-theme]");
    if (themeEl) {
      setPref("theme", themeEl.getAttribute("data-set-theme"));
      applyPrefs();
    }
    var patternEl = ev.target.closest("[data-set-pattern]");
    if (patternEl) {
      setPref("pattern", patternEl.getAttribute("data-set-pattern"));
      applyPrefs();
    }
    var toggle = ev.target.closest("[data-toggle-settings]");
    var pop = document.getElementById("settings-pop");
    if (toggle && pop) {
      pop.hidden = !pop.hidden;
    } else if (pop && !pop.hidden && !ev.target.closest("#settings-pop")) {
      pop.hidden = true;
    }
  });

  document.addEventListener("change", function (ev) {
    if (ev.target.id === "sound-select") setPref("sound", ev.target.value);
  });

  /* ---- Exception 4: click-feedback glow ring (§5) ------------------ */

  document.addEventListener("click", function (ev) {
    var el = ev.target.closest("button, .btn");
    if (!el || el.disabled) return;
    el.classList.add("fx-glow");
    setTimeout(function () { el.classList.remove("fx-glow"); }, 180);
    playClick();

    /* Quick-switch tab active state — pure class cosmetics, part of
       the same feedback exception. */
    if (el.classList.contains("tab")) {
      document.querySelectorAll(".tab").forEach(function (t) {
        t.classList.toggle("active", t === el);
      });
    }
  });

  /* ---- Exception 3: Web Audio click sounds (§5, default OFF) ------- */

  var audioCtx = null;

  function playClick() {
    var sound = loadPrefs().sound || "glass-tap";
    if (sound === "off") return;
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      var t = audioCtx.currentTime;
      var osc = audioCtx.createOscillator();
      var gain = audioCtx.createGain();
      osc.connect(gain);
      gain.connect(audioCtx.destination);

      var recipes = {
        "glass-tap":  { type: "sine",     freq: 1800, decay: 0.06, vol: 0.08 },
        "soft-chime": { type: "sine",     freq: 880,  decay: 0.25, vol: 0.06 },
        "mechanical": { type: "square",   freq: 220,  decay: 0.03, vol: 0.05 },
        "marimba":    { type: "triangle", freq: 440,  decay: 0.18, vol: 0.09 },
        "low-pulse":  { type: "sine",     freq: 140,  decay: 0.12, vol: 0.10 },
        "crystal":    { type: "sine",     freq: 2600, decay: 0.10, vol: 0.05 }
      };
      var r = recipes[sound] || recipes["glass-tap"];
      osc.type = r.type;
      osc.frequency.setValueAtTime(r.freq, t);
      gain.gain.setValueAtTime(r.vol, t);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + r.decay);
      osc.start(t);
      osc.stop(t + r.decay + 0.02);
    } catch (e) {
      /* Audio unavailable — silently fine, sound is a garnish. */
    }
  }

  /* ---- Exception 1: clipboard copy for approved calls -------------- */
  /* The server marks the confirmed call card with data-clipboard-copy.
     Auto-attempt on swap (works inside the click's transient-activation
     window when the response is fast); always leave a visible manual
     copy button as the fallback — never rely on the auto-attempt. */

  function attemptCopy(el) {
    var text = el.getAttribute("data-clipboard-copy");
    if (!text || !navigator.clipboard) return;
    navigator.clipboard.writeText(text).then(
      function () {
        var status = el.querySelector("[data-copy-status]");
        if (status) status.textContent = "Copied to clipboard ✓";
      },
      function () { /* manual button remains */ }
    );
  }

  document.body.addEventListener("htmx:afterSwap", function (ev) {
    ev.detail.elt.querySelectorAll("[data-clipboard-copy]").forEach(attemptCopy);
    applyPrefs();
  });

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-copy-btn]");
    if (!btn) return;
    var host = btn.closest("[data-clipboard-copy]");
    if (host) attemptCopy(host);
  });

  /* ---- Exception 2: panel drag-resize (§4) ------------------------- */
  /* 6px handles, accent tint on hover (CSS), min-width floor 130px,
     widths persisted per browser via localStorage. */

  var MIN_PANE = 130;

  function initResize() {
    var prefs = loadPrefs();
    var root = document.documentElement;
    if (prefs.paneLeft) root.style.setProperty("--pane-left", prefs.paneLeft + "px");
    if (prefs.paneRight) root.style.setProperty("--pane-right", prefs.paneRight + "px");

    document.querySelectorAll(".resize-handle").forEach(function (handle) {
      handle.addEventListener("pointerdown", function (down) {
        down.preventDefault();
        handle.classList.add("dragging");
        handle.setPointerCapture(down.pointerId);
        var side = handle.getAttribute("data-resize");
        var startX = down.clientX;
        var startW = parseInt(
          getComputedStyle(root).getPropertyValue(side === "left" ? "--pane-left" : "--pane-right"),
          10
        );

        function onMove(move) {
          var delta = move.clientX - startX;
          var w = side === "left" ? startW + delta : startW - delta;
          w = Math.max(MIN_PANE, Math.min(w, window.innerWidth / 2));
          root.style.setProperty(side === "left" ? "--pane-left" : "--pane-right", w + "px");
        }

        function onUp() {
          handle.classList.remove("dragging");
          handle.removeEventListener("pointermove", onMove);
          handle.removeEventListener("pointerup", onUp);
          var key = side === "left" ? "paneLeft" : "paneRight";
          setPref(key, parseInt(
            getComputedStyle(root).getPropertyValue(side === "left" ? "--pane-left" : "--pane-right"),
            10
          ));
        }

        handle.addEventListener("pointermove", onMove);
        handle.addEventListener("pointerup", onUp);
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyPrefs();
    initResize();
  });
})();
