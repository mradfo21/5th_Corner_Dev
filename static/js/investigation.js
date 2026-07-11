/* ============================================================
   investigation.js — investigation / photograph capture infrastructure
   ------------------------------------------------------------
   Two small, dependency-free modules that turn a spot in the LIVE scene into a
   stored "texture" the game can reason about later:

     window.SceneCapture
       Reads pixels out of whatever is currently on screen (the Reactor live
       video, or — as a fallback — the still-image scene layers) and crops a
       bounding box AROUND (and under) a viewport point into a small thumbnail
       data-URL. Handles the `object-fit: cover` letterbox/overflow math so the
       crop lands on the pixels the player actually sees under the gizmo.

     window.CaptureStore
       A structured, capped, persisted (localStorage) store of those captures
       with rich metadata (where on screen, normalized coords, a human region
       label, the scene turn, the source, and any prompt the player attached).
       It emits change events so UIs can react.

   WHY THIS EXISTS (design intent):
     The TOUCH tool lets the player reach into the world and investigate a spot.
     Every investigation now also grabs a little texture of that patch — like
     lifting a fingerprint. These thumbnails are the raw material for future
     mechanics: building prompts out of things you've investigated, a
     journalist-style "photograph" that captures a framed subset of the world
     with ceremony, evidence boards, etc. Nothing consumes the store yet — this
     file is the plumbing so those mechanics have somewhere to pull from.

   Both modules are intentionally UI-agnostic and standalone.js-agnostic so they
   can be reused by any future feature (photograph mode, evidence board, an
   AI-facing "what have I looked at" endpoint, …).
   ============================================================ */
(function () {
  "use strict";

  function log() {
    try { console.log.apply(console, ["[capture]", ...arguments]); } catch (_) {}
  }

  // ==========================================================
  // SceneCapture — crop a region of the on-screen world to a data URL
  // ==========================================================
  const SceneCapture = (function () {
    // Default edge length (viewport px) of the square grabbed around a point.
    // Deliberately larger than the 48px reticle so we capture the world AROUND
    // and UNDER the hand, not just the gizmo footprint.
    const DEFAULT_BOX = 176;
    // Longest edge (px) of the stored thumbnail texture. Small on purpose:
    // these are meant to be many, cheap, and prompt-sized.
    const DEFAULT_THUMB = 160;
    const JPEG_QUALITY = 0.82;

    function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

    function liveVideo() {
      const rr = window.ReactorRenderer;
      const showing = rr && typeof rr.isShowing === "function" && rr.isShowing();
      if (!showing) return null;
      const v = document.getElementById("reactor-video");
      if (v && v.videoWidth > 0) return v;
      return null;
    }

    // The still-image scene layer currently faded in (image renderer mode).
    function activeSceneEl() {
      const a = document.getElementById("sceneA");
      const b = document.getElementById("sceneB");
      const pick = (el) => el && el.classList.contains("scene-active") ? el : null;
      return pick(a) || pick(b) || null;
    }

    function sceneBackgroundUrl(el) {
      if (!el) return null;
      const bg = getComputedStyle(el).backgroundImage || "";
      const m = bg.match(/url\((['"]?)(.*?)\1\)/);
      return m ? m[2] : null;
    }

    // Given a media element that is painted with `object-fit: cover` inside a
    // box `rect`, and the media's natural size, map a viewport point + a
    // viewport-space box edge into SOURCE-pixel crop coordinates.
    //   cover scale s = max(rect.w / natW, rect.h / natH)
    //   displayed media size = natW*s x natH*s (>= rect, overflow is clipped)
    //   a viewport length L corresponds to L / s source pixels
    function coverCrop(rect, natW, natH, x, y, box) {
      const s = Math.max(rect.width / natW, rect.height / natH);
      if (!(s > 0)) return null;
      const dispW = natW * s;
      const dispH = natH * s;
      // Top-left of the displayed (overflowing) media, in viewport coords.
      const dispLeft = rect.left + (rect.width - dispW) / 2;
      const dispTop = rect.top + (rect.height - dispH) / 2;
      // Point in source pixels.
      const srcX = (x - dispLeft) / s;
      const srcY = (y - dispTop) / s;
      const srcBox = box / s;
      let sx = srcX - srcBox / 2;
      let sy = srcY - srcBox / 2;
      let sw = srcBox;
      let sh = srcBox;
      // Clamp the crop rect inside the source so drawImage never reads outside.
      sx = clamp(sx, 0, Math.max(0, natW - 1));
      sy = clamp(sy, 0, Math.max(0, natH - 1));
      sw = clamp(sw, 1, natW - sx);
      sh = clamp(sh, 1, natH - sy);
      return { sx, sy, sw, sh };
    }

    function drawCrop(source, crop, thumbMax) {
      const longest = Math.max(crop.sw, crop.sh);
      const scale = Math.min(1, thumbMax / longest);
      const outW = Math.max(1, Math.round(crop.sw * scale));
      const outH = Math.max(1, Math.round(crop.sh * scale));
      const c = document.createElement("canvas");
      c.width = outW;
      c.height = outH;
      const ctx = c.getContext("2d");
      ctx.drawImage(source, crop.sx, crop.sy, crop.sw, crop.sh, 0, 0, outW, outH);
      return { canvas: c, width: outW, height: outH };
    }

    function loadImage(url) {
      return new Promise((resolve, reject) => {
        const img = new Image();
        img.crossOrigin = "anonymous"; // same-origin /serve_image; enables toDataURL
        img.onload = () => resolve(img);
        img.onerror = (e) => reject(e);
        img.src = url;
      });
    }

    // Crop a square region of the current scene around a viewport point.
    // opts: { x, y, box, thumb }  (box/thumb optional; defaults above)
    // Returns a Promise resolving to a result object, or null if nothing on
    // screen can be read.
    //   { dataUrl, width, height, source: "reactor"|"image",
    //     natural: {w,h}, crop: {sx,sy,sw,sh}, box }
    async function captureRegion(opts) {
      opts = opts || {};
      const box = opts.box || DEFAULT_BOX;
      const thumb = opts.thumb || DEFAULT_THUMB;
      const x = opts.x != null ? opts.x : window.innerWidth / 2;
      const y = opts.y != null ? opts.y : window.innerHeight / 2;

      // 1) Prefer the live Reactor video.
      const v = liveVideo();
      if (v) {
        try {
          const rect = v.getBoundingClientRect();
          const crop = coverCrop(rect, v.videoWidth, v.videoHeight, x, y, box);
          if (crop) {
            const out = drawCrop(v, crop, thumb);
            return {
              dataUrl: out.canvas.toDataURL("image/jpeg", JPEG_QUALITY),
              width: out.width,
              height: out.height,
              source: "reactor",
              natural: { w: v.videoWidth, h: v.videoHeight },
              crop,
              box,
            };
          }
        } catch (err) {
          log("video region capture failed", err);
        }
      }

      // 2) Fallback: the still-image scene layer (image renderer mode).
      const sceneEl = activeSceneEl();
      const url = sceneBackgroundUrl(sceneEl);
      if (sceneEl && url) {
        try {
          const img = await loadImage(url);
          const rect = sceneEl.getBoundingClientRect();
          const crop = coverCrop(rect, img.naturalWidth, img.naturalHeight, x, y, box);
          if (crop) {
            const out = drawCrop(img, crop, thumb);
            return {
              dataUrl: out.canvas.toDataURL("image/jpeg", JPEG_QUALITY),
              width: out.width,
              height: out.height,
              source: "image",
              natural: { w: img.naturalWidth, h: img.naturalHeight },
              crop,
              box,
            };
          }
        } catch (err) {
          log("still region capture failed", err);
        }
      }

      return null;
    }

    // Capture the WHOLE visible frame (a framed shot of the world) — the raw
    // grab behind the future journalist-style "photograph" mechanic. Same
    // return shape as captureRegion but source-sized (down to `max`).
    async function captureFull(opts) {
      opts = opts || {};
      const max = opts.max || 640;
      const v = liveVideo();
      if (v) {
        try {
          const scale = Math.min(1, max / v.videoWidth);
          const w = Math.max(1, Math.round(v.videoWidth * scale));
          const h = Math.max(1, Math.round(v.videoHeight * scale));
          const c = document.createElement("canvas");
          c.width = w; c.height = h;
          c.getContext("2d").drawImage(v, 0, 0, w, h);
          return {
            dataUrl: c.toDataURL("image/jpeg", JPEG_QUALITY),
            width: w, height: h, source: "reactor",
            natural: { w: v.videoWidth, h: v.videoHeight }, crop: null, box: null,
          };
        } catch (err) { log("full video capture failed", err); }
      }
      const sceneEl = activeSceneEl();
      const url = sceneBackgroundUrl(sceneEl);
      if (url) {
        try {
          const img = await loadImage(url);
          const scale = Math.min(1, max / img.naturalWidth);
          const w = Math.max(1, Math.round(img.naturalWidth * scale));
          const h = Math.max(1, Math.round(img.naturalHeight * scale));
          const c = document.createElement("canvas");
          c.width = w; c.height = h;
          c.getContext("2d").drawImage(img, 0, 0, w, h);
          return {
            dataUrl: c.toDataURL("image/jpeg", JPEG_QUALITY),
            width: w, height: h, source: "image",
            natural: { w: img.naturalWidth, h: img.naturalHeight }, crop: null, box: null,
          };
        } catch (err) { log("full still capture failed", err); }
      }
      return null;
    }

    function available() {
      return !!(liveVideo() || sceneBackgroundUrl(activeSceneEl()));
    }

    return {
      captureRegion,
      captureFull,
      available,
      DEFAULT_BOX,
      DEFAULT_THUMB,
    };
  })();

  // ==========================================================
  // CaptureStore — a structured, capped, persisted log of captures
  // ==========================================================
  const CaptureStore = (function () {
    const STORAGE_KEY = "investigation_captures_v1";
    // Keep the store small: these carry base64 thumbnails, and localStorage is
    // ~5MB. Cap the count and evict oldest first.
    const MAX_ENTRIES = 24;

    let entries = [];          // newest last in memory; exposed newest-first
    const listeners = new Set();

    function nowIso() { return new Date().toISOString(); }

    function makeId() {
      return "cap_" + Date.now().toString(36) + "_" +
        Math.random().toString(36).slice(2, 8);
    }

    function load() {
      try {
        const raw = window.localStorage.getItem(STORAGE_KEY);
        if (!raw) return;
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) entries = parsed;
      } catch (err) {
        log("store load failed", err);
      }
    }

    function persist() {
      // Best-effort. If we blow the quota, drop oldest entries until it fits.
      let attempt = entries.slice();
      for (let i = 0; i < MAX_ENTRIES; i++) {
        try {
          window.localStorage.setItem(STORAGE_KEY, JSON.stringify(attempt));
          return;
        } catch (err) {
          if (attempt.length <= 1) { log("store persist failed", err); return; }
          attempt = attempt.slice(1); // drop the oldest and retry
          entries = attempt;
        }
      }
    }

    function emit() {
      const snapshot = list();
      listeners.forEach((fn) => { try { fn(snapshot); } catch (_) {} });
    }

    // Normalize a caller-supplied entry into the stored record shape.
    function normalize(input) {
      input = input || {};
      const pt = input.point || null;
      const vw = Math.max(1, window.innerWidth);
      const vh = Math.max(1, window.innerHeight);
      const norm = input.norm || (pt ? { x: pt.x / vw, y: pt.y / vh } : null);
      return {
        id: input.id || makeId(),
        createdAt: input.createdAt || nowIso(),
        kind: input.kind || "investigation", // "investigation" | "photograph"
        thumb: input.thumb || null,           // data URL of the captured texture
        width: input.width || null,
        height: input.height || null,
        source: input.source || null,         // "reactor" | "image"
        point: pt,                             // {x,y} viewport px at capture
        norm,                                  // {x,y} normalized 0..1
        region: input.region || null,         // human label e.g. "top-left of the view"
        box: input.box || null,               // viewport px edge of the grabbed square
        prompt: input.prompt || null,         // player text attached to this capture
        turn: input.turn != null ? input.turn : null,
        tags: Array.isArray(input.tags) ? input.tags.slice() : [],
        used: !!input.used,                    // reserved: consumed into a prompt yet?
        meta: input.meta || {},                // free-form extension bag
      };
    }

    // Add a capture. Returns the stored record (with its id).
    function add(input) {
      const rec = normalize(input);
      entries.push(rec);
      if (entries.length > MAX_ENTRIES) entries = entries.slice(entries.length - MAX_ENTRIES);
      persist();
      emit();
      return rec;
    }

    // Patch an existing capture (e.g. attach the prompt the player typed).
    function update(id, patch) {
      const idx = entries.findIndex((e) => e.id === id);
      if (idx === -1) return null;
      entries[idx] = Object.assign({}, entries[idx], patch || {});
      persist();
      emit();
      return entries[idx];
    }

    function remove(id) {
      const before = entries.length;
      entries = entries.filter((e) => e.id !== id);
      if (entries.length !== before) { persist(); emit(); }
    }

    function clear() {
      entries = [];
      persist();
      emit();
    }

    function get(id) { return entries.find((e) => e.id === id) || null; }

    // Newest-first view (a shallow copy so callers can't mutate internals).
    function list() { return entries.slice().reverse(); }

    function latest() { return entries.length ? entries[entries.length - 1] : null; }

    function count() { return entries.length; }

    // Subscribe to change events. Returns an unsubscribe fn.
    function subscribe(fn) {
      if (typeof fn !== "function") return function () {};
      listeners.add(fn);
      try { fn(list()); } catch (_) {}
      return function () { listeners.delete(fn); };
    }

    load();

    return {
      add, update, remove, clear, get, list, latest, count, subscribe,
      MAX_ENTRIES,
    };
  })();

  // ==========================================================
  // Investigation — convenience facade combining capture + store
  // ==========================================================
  // A thin, one-call API so future features don't have to wire SceneCapture and
  // CaptureStore together themselves. Each returns a Promise for the stored
  // record (or null if nothing could be captured).
  const Investigation = {
    // Grab the patch of world around a point and file it as an investigation.
    // (This is what the TOUCH tool uses under the hood.)
    async investigate(opts) {
      opts = opts || {};
      const cap = await SceneCapture.captureRegion(opts);
      if (!cap) return null;
      return CaptureStore.add({
        kind: "investigation",
        thumb: cap.dataUrl, width: cap.width, height: cap.height,
        source: cap.source, box: cap.box,
        point: opts.point || (opts.x != null ? { x: opts.x, y: opts.y } : null),
        region: opts.region || null,
        prompt: opts.prompt || null,
        turn: opts.turn != null ? opts.turn : null,
        tags: opts.tags || [],
        meta: opts.meta || {},
      });
    },

    // Capture the whole framed shot as a "photograph" — the seed of the future
    // journalist mechanic (frame a subset of the world, with ceremony). Kept
    // deliberately simple; the ceremony/UI belongs to the feature that calls it.
    async photograph(opts) {
      opts = opts || {};
      const cap = await SceneCapture.captureFull(opts);
      if (!cap) return null;
      return CaptureStore.add({
        kind: "photograph",
        thumb: cap.dataUrl, width: cap.width, height: cap.height,
        source: cap.source, box: null,
        prompt: opts.caption || opts.prompt || null,
        turn: opts.turn != null ? opts.turn : null,
        tags: opts.tags || ["photograph"],
        meta: opts.meta || {},
      });
    },
  };

  window.SceneCapture = SceneCapture;
  window.CaptureStore = CaptureStore;
  window.Investigation = Investigation;
})();
