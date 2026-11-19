/**
 * BooMascot (LFM3 Edition) — 100% Local, Zero-Dependency 3D Interactive Web Mascot
 * Based on the Liquid AI LFM3 mascot architecture.
 *
 * Features:
 * - Authentic LFM3 3D sphere-projected face (asymmetric tilted eyes, happy/sad eye arcs, caret/smile & rounded-diamond open mouth)
 * - Two built-in variants: "with-purple" (With Purple Layer) and "without-purple" (Without Purple Layer)
 * - True 3D backface / horizon plane clipping (Sutherland-Hodgman against z >= R²/camZ) so eyes, mouth, and glasses are completely hidden when rotated behind the head during the 360° "thinking" spin
 * - Chained damped harmonic spring physics for cursor lean, secondary head tilt, and click-to-poke squish impulse
 * - No floating hands, no wave animation
 * - Works via <boo-mascot> Web Component or BooMascot.mount(container, options)
 */
(function (global) {
  "use strict";

  const TAU = Math.PI * 2;
  const DEG2RAD = Math.PI / 180;

  function clamp(v, lo, hi) {
    return Math.min(hi, Math.max(lo, v));
  }

  function lerp(a, b, t) {
    return a + (b - a) * t;
  }

  function smoothstep(edge0, edge1, x) {
    const t = clamp((x - edge0) / Math.max(edge1 - edge0, 1e-6), 0, 1);
    return t * t * (3 - 2 * t);
  }

  function easeInOutCubic(t) {
    const c = clamp(t, 0, 1);
    return c * c * (3 - 2 * c);
  }

  function parseHexRgb(hex) {
    if (typeof hex !== "string") return null;
    const m = hex.trim().match(/^#([0-9a-fA-F]{6})$/);
    if (!m) return null;
    const n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function rgbToCss(rgb) {
    return `rgb(${Math.round(rgb[0])}, ${Math.round(rgb[1])}, ${Math.round(rgb[2])})`;
  }

  // ---------------------------------------------------------------------------
  // LFM3 Palettes & Variants (With & Without Purple Layer)
  // ---------------------------------------------------------------------------
  const VARIANTS = {
    "with-purple": {
      name: "With Purple Layer",
      hasPurpleLayer: true,
      ringAlpha: 0.75,
      bodyRadius: 0.745,
      light: {
        ringTop: "#eeeaf9",
        ringBottom: "#b3b2ee",
        headTop: "#f6eefc",
        headBottom: "#cebef5",
        headShadeTop: "#e0d3f5",
        headShadeBottom: "#baace8",
        highlight: "#ffffff",
        auraInner: "rgba(167, 139, 250, 0.22)",
        auraOuter: "rgba(139, 92, 246, 0.0)",
        eye: "#3a3560",
        mouth: "#58537e",
        blush: "#b794f4",
        glasses: "#3a3560",
        gearMain: "#3a3560",
        gearTop: "#5a5388",
        gearPad: "#b3b2ee",
        gearBrim: "#464162",
        gearHandle: "#4a4473",
      },
      dark: {
        ringTop: "#5a53b8",
        ringBottom: "#4a4996",
        headTop: "#f6eefc",
        headBottom: "#cebef5",
        headShadeTop: "#d9cbf2",
        headShadeBottom: "#baace8",
        highlight: "#ffffff",
        auraInner: "rgba(139, 92, 246, 0.28)",
        auraOuter: "rgba(91, 33, 182, 0.0)",
        eye: "#3a3560",
        mouth: "#58537e",
        blush: "#b794f4",
        glasses: "#3a3560",
        gearMain: "#8e86bc",
        gearTop: "#a29acb",
        gearPad: "#d9d4f2",
        gearBrim: "#7a72a6",
        gearHandle: "#7a72a6",
      },
    },

    "without-purple": {
      name: "Without Purple Layer",
      hasPurpleLayer: false,
      ringAlpha: 0,
      bodyRadius: 0.82,
      light: {
        ringTop: "#eeeaf9",
        ringBottom: "#b3b2ee",
        headTop: "#f8f4fc",
        headBottom: "#d8cbf8",
        headShadeTop: "#e4d8f8",
        headShadeBottom: "#c1b2ec",
        highlight: "#ffffff",
        auraInner: "rgba(167, 139, 250, 0.14)",
        auraOuter: "rgba(139, 92, 246, 0.0)",
        eye: "#3a3560",
        mouth: "#58537e",
        blush: "#b794f4",
        glasses: "#3a3560",
        gearMain: "#3a3560",
        gearTop: "#5a5388",
        gearPad: "#b3b2ee",
        gearBrim: "#464162",
        gearHandle: "#4a4473",
      },
      dark: {
        ringTop: "#5a53b8",
        ringBottom: "#4a4996",
        headTop: "#f6eefc",
        headBottom: "#cebef5",
        headShadeTop: "#d9cbf2",
        headShadeBottom: "#baace8",
        highlight: "#ffffff",
        auraInner: "rgba(139, 92, 246, 0.18)",
        auraOuter: "rgba(91, 33, 182, 0.0)",
        eye: "#3a3560",
        mouth: "#58537e",
        blush: "#b794f4",
        glasses: "#3a3560",
        gearMain: "#8e86bc",
        gearTop: "#a29acb",
        gearPad: "#d9d4f2",
        gearBrim: "#7a72a6",
        gearHandle: "#7a72a6",
      },
    },
  };

  // Exact LFM3 face layout options ("soft" from LFM3 simple variant, "tilted" from LFM3 default)
  const FACE_PRESETS = {
    soft: {
      eyes: [
        { center: [0.6, 0.16], rx: 0.112, ry: 0.165, rot: 0.2 },
        { center: [-0.44, -0.12], rx: 0.112, ry: 0.165, rot: 0.2 },
      ],
      caret: { apex: [0.08, 0.0], arm: 0.13, spread: 0.72, width: 0.022 },
      smile: {
        apex: [0.133, -0.104],
        arms: [
          [2.84, 0.145],
          [1.117, 0.13],
        ],
        width: 0.0227,
      },
      diamondDrop: 0.09,
    },
    tilted: {
      eyes: [
        { center: [0.41, 0.45], rx: 0.155, ry: 0.11, rot: -0.55 },
        { center: [-0.4, -0.18], rx: 0.112, ry: 0.158, rot: 0.28 },
      ],
      caret: { apex: [0.05, 0.02], arm: 0.13, spread: 0.72, width: 0.02 },
      smile: {
        apex: [0.09, 0.05],
        arms: [
          [3.24, 0.145],
          [1.517, 0.13],
        ],
        width: 0.02,
      },
      diamondDrop: 0.04,
    },
  };

  // "waving" removed as requested; keeping all other moods
  const VALID_MOODS = [
    "auto",
    "idle",
    "thinking",
    "listening",
    "happy",
    "shy",
    "distressed",
  ];

  // ---------------------------------------------------------------------------
  // 3D Math, Sphere Surface Projection & Backface/Horizon Clipping
  // ---------------------------------------------------------------------------
  function createRotationMatrix(rx, ry, rz) {
    const cx = Math.cos(rx),
      sx = Math.sin(rx);
    const cy = Math.cos(ry),
      sy = Math.sin(ry);
    const cz = Math.cos(rz),
      sz = Math.sin(rz);

    // Three.js Euler('XYZ'): R = Rx * Ry * Rz
    return [
      cy * cz,
      -cy * sz,
      sy,
      sx * sy * cz + cx * sz,
      -sx * sy * sz + cx * cz,
      -sx * cy,
      -cx * sy * cz + sx * sz,
      cx * sy * sz + sx * cz,
      cx * cy,
    ];
  }

  function applyMat3(m, x, y, z) {
    return {
      x: m[0] * x + m[1] * y + m[2] * z,
      y: m[3] * x + m[4] * y + m[5] * z,
      z: m[6] * x + m[7] * y + m[8] * z,
    };
  }

  /**
   * Projects a 2D local point (lx, ly) rotated by angle `rot` and translated to `[cx, cy]`
   * onto the 3D sphere of radius `radius * lift` (exact LFM3 `bn` formula):
   *   u = cx + lx * cos(rot) - ly * sin(rot)
   *   v = cy + lx * sin(rot) + ly * cos(rot)
   *   w = sqrt(max(1 - u² - v², 0))
   */
  function projectToSphere3D(lx, ly, cx, cy, rot, radius, lift = 1.003) {
    const c = Math.cos(rot);
    const s = Math.sin(rot);
    let u = cx + lx * c - ly * s;
    let v = cy + lx * s + ly * c;
    const r2 = u * u + v * v;
    if (r2 > 0.996) {
      const k = Math.sqrt(0.996 / r2);
      u *= k;
      v *= k;
    }
    const w = Math.sqrt(Math.max(1 - u * u - v * v, 0));
    const R = radius * lift;
    return { x: u * R, y: v * R, z: w * R };
  }

  /**
   * Clips a closed 3D polygon against the visible front-hemisphere horizon plane `z >= zHorizon`.
   * When the head rotates 180° during "thinking", all vertices on the back of the head have `z < zHorizon`,
   * so this returns an empty array and the feature is 100% hidden behind the sphere!
   */
  function clipPolygonToHorizon(pts3D, zHorizon) {
    const out = [];
    const n = pts3D.length;
    if (n === 0) return out;

    for (let i = 0; i < n; i++) {
      const a = pts3D[i];
      const b = pts3D[(i + 1) % n];
      const aIn = a.z >= zHorizon;
      const bIn = b.z >= zHorizon;

      if (aIn && bIn) {
        out.push(b);
      } else if (aIn && !bIn) {
        const t = (zHorizon - a.z) / (b.z - a.z);
        out.push({
          x: a.x + (b.x - a.x) * t,
          y: a.y + (b.y - a.y) * t,
          z: zHorizon,
        });
      } else if (!aIn && bIn) {
        const t = (zHorizon - a.z) / (b.z - a.z);
        out.push({
          x: a.x + (b.x - a.x) * t,
          y: a.y + (b.y - a.y) * t,
          z: zHorizon,
        });
        out.push(b);
      }
    }
    return out;
  }

  /**
   * Clips an open 3D polyline against `z >= zHorizon`, returning an array of visible sub-polylines.
   */
  function clipPolylineToHorizon(pts3D, zHorizon) {
    const segments = [];
    let current = [];

    for (let i = 0; i < pts3D.length - 1; i++) {
      const a = pts3D[i];
      const b = pts3D[i + 1];
      const aIn = a.z >= zHorizon;
      const bIn = b.z >= zHorizon;

      if (aIn && bIn) {
        if (current.length === 0) current.push(a);
        current.push(b);
      } else if (aIn && !bIn) {
        if (current.length === 0) current.push(a);
        const t = (zHorizon - a.z) / (b.z - a.z);
        current.push({
          x: a.x + (b.x - a.x) * t,
          y: a.y + (b.y - a.y) * t,
          z: zHorizon,
        });
        segments.push(current);
        current = [];
      } else if (!aIn && bIn) {
        const t = (zHorizon - a.z) / (b.z - a.z);
        current = [
          {
            x: a.x + (b.x - a.x) * t,
            y: a.y + (b.y - a.y) * t,
            z: zHorizon,
          },
          b,
        ];
      }
    }
    if (current.length > 1) segments.push(current);
    return segments;
  }

  // ---------------------------------------------------------------------------
  // Main BooMascotInstance Class
  // ---------------------------------------------------------------------------
  class BooMascotInstance {
    constructor(container, options = {}) {
      if (typeof container === "string") {
        container = document.querySelector(container);
      }
      if (!container) {
        throw new Error("BooMascot: valid container element required.");
      }
      this.container = container;

      // Resolve variant ("with-purple" or "without-purple")
      let initialVariant = options.variant || options.skin || "with-purple";
      if (options.purpleLayer === false || initialVariant === "without-purple" || initialVariant === "no-purple") {
        initialVariant = "without-purple";
      } else if (options.purpleLayer === true || initialVariant === "with-purple" || initialVariant === "lfm3") {
        initialVariant = "with-purple";
      }

      this.opts = {
        variant: VARIANTS[initialVariant] ? initialVariant : "with-purple",
        face: FACE_PRESETS[options.face] ? options.face : "soft",
        theme: options.theme || "auto",
        mood: VALID_MOODS.includes(options.mood) ? options.mood : "auto",
        mouth: options.mouth || "happy", // LFM3 header uses mouth: "happy" by default
        shyOnHover: options.shyOnHover !== undefined ? Boolean(options.shyOnHover) : false,
        accessories: Array.isArray(options.accessories) ? [...options.accessories] : [],
        lookStrength: options.lookStrength ?? 0.34,
        idleLook: options.idleLook ?? 0.12,
        bob: options.bob ?? 0.05,
        bobSpeed: options.bobSpeed ?? 0.28,
        lean: options.lean ?? 0.07,
        softness: options.softness ?? 1.0,
        blinkEvery: options.blinkEvery ?? 4.0,
        thinkSpin: options.thinkSpin ?? 3.0,
        listenTilt: options.listenTilt ?? 0.22,
        shakeRate: options.shakeRate ?? 2.2,
        shakeAmount: options.shakeAmount ?? 0.32,
        cameraZ: options.cameraZ ?? 6.1,
        ringTop: options.ringTop || null,
        ringBottom: options.ringBottom || null,
        onPoke: options.onPoke || null,
        onMoodChange: options.onMoodChange || null,
      };

      // Create high-DPI canvas
      this.canvas = document.createElement("canvas");
      Object.assign(this.canvas.style, {
        display: "block",
        width: "100%",
        height: "100%",
        cursor: "pointer",
        touchAction: "manipulation",
        userSelect: "none",
      });
      this.container.appendChild(this.canvas);
      this.ctx = this.canvas.getContext("2d");

      // Simulation clock
      this.time = 0;
      this.lastFrameTime = performance.now();
      this.lastMoveTime = -10;
      this.reducedMotion =
        typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches
          ? 0.3
          : 1;

      // Pointer & gaze state
      this.pointerNorm = { x: 0, y: 0 };
      this.targetLook = { x: 0, y: 0 };
      this.smoothLook = { x: 0, y: 0 };
      this.idleGaze = { x: 0, y: 0 };
      this.pointerWorld = { x: 0, y: 0 };
      this.pointerNear = 0;
      this.pointerNearSmooth = 0;
      this.isHovered = false;

      // Chained damped springs (At: outer ring, Bt: inner head) matching LFM3
      this.springBase = { p: { x: 0, y: 0, z: 0 }, v: { x: 0, y: 0, z: 0 } };
      this.springHead = { p: { x: 0, y: 0, z: 0 }, v: { x: 0, y: 0, z: 0 } };
      this.ringPos = { x: 0, y: 0 };
      this.headPos = { x: 0, y: 0 };
      this.headRoll = 0;
      this.rotX = 0;
      this.rotY = 0;
      this.rotZ = 0;
      this.currentBlink = 0;
      this.clapWave = 1;

      // Poke squish harmonic oscillators (D/at for ring, X/J for head)
      this.D = 0;
      this.at = 0;
      this.X = 0;
      this.J = 0;
      this.pokeTimer = -1;

      // Purple layer alpha smooth transition
      this.purpleLayerWeight = VARIANTS[this.opts.variant].hasPurpleLayer ? 1 : 0;

      // Mood blend weights (no waving)
      this.moodWeights = {
        listening: 0,
        thinking: 0,
        happy: 0,
        shy: 0,
        distressed: 0,
      };
      this.thinkSpinAngle = 0;
      this.listenNodTimer = -1;

      // Blink state
      this.nextBlinkTime = 1.2;
      this.blinkProgress = -1;
      this.eyesClosedWeight = this.opts.accessories.includes("eyesClosed") ? 1 : 0;

      const initBase = this._getBasePalette();
      this._ringTopRgb = parseHexRgb(this.opts.ringTop || initBase.ringTop);
      this._ringBottomRgb = parseHexRgb(this.opts.ringBottom || initBase.ringBottom);

      this._bindEvents();
      this._resize();

      this.manualPaused = false;
      this.isVisible = true;
      this.rafId = requestAnimationFrame((ts) => this._loop(ts));
    }

    _getResolvedTheme() {
      if (this.opts.theme === "dark" || this.opts.theme === "light") {
        return this.opts.theme;
      }
      if (document.documentElement.classList.contains("dark")) return "dark";
      if (document.documentElement.classList.contains("light")) return "light";
      return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }

    _getVariantConfig() {
      return VARIANTS[this.opts.variant] || VARIANTS["with-purple"];
    }

    _getBasePalette() {
      const v = this._getVariantConfig();
      const theme = this._getResolvedTheme();
      return v[theme] || v.light;
    }

    _getPalette() {
      const base = this._getBasePalette();
      const top = this._ringTopRgb ? rgbToCss(this._ringTopRgb) : (this.opts.ringTop || base.ringTop);
      const bot = this._ringBottomRgb ? rgbToCss(this._ringBottomRgb) : (this.opts.ringBottom || base.ringBottom);
      if (top === base.ringTop && bot === base.ringBottom) return base;
      return { ...base, ringTop: top, ringBottom: bot };
    }

    _bindEvents() {
      this._onPointerMove = (e) => {
        if (this.manualPaused) return;
        const rect = this.canvas.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return;

        const P = ((e.clientX - rect.left) / rect.width) * 2 - 1;
        const Q = -(((e.clientY - rect.top) / rect.height) * 2 - 1);

        this.pointerNorm.x = P;
        this.pointerNorm.y = Q;
        this.pointerNear = Math.abs(P) < 1.3 && Math.abs(Q) < 1.3 ? 1 : 0;
        this.isHovered = Math.hypot(P, Q) < 0.85;

        this.targetLook.x = clamp(P, -1.5, 1.5);
        this.targetLook.y = clamp(Q, -1.5, 1.5);

        // Unproject pointer into world plane z=0
        const fovScale = Math.tan(14 * DEG2RAD) * this.opts.cameraZ;
        this.pointerWorld.x = P * fovScale;
        this.pointerWorld.y = Q * fovScale;
        this.lastMoveTime = this.time;
      };

      this._onPointerLeaveDocument = () => {
        this.targetLook.x = 0;
        this.targetLook.y = 0;
        this.pointerNear = 0;
        this.isHovered = false;
      };

      this._onPointerDown = (e) => {
        e.stopPropagation();
        this.poke();
      };

      window.addEventListener("pointermove", this._onPointerMove, { passive: true });
      document.addEventListener("pointerleave", this._onPointerLeaveDocument);
      this.canvas.addEventListener("pointerdown", this._onPointerDown);

      if (typeof ResizeObserver !== "undefined") {
        this.resizeObserver = new ResizeObserver(() => this._resize());
        this.resizeObserver.observe(this.container);
      }

      if ("IntersectionObserver" in window) {
        this.intersectionObserver = new IntersectionObserver(([entry]) => {
          this.isVisible = !this.manualPaused && entry.isIntersecting;
          if (this.isVisible && !this.rafId) {
            this.lastFrameTime = performance.now();
            this.rafId = requestAnimationFrame((ts) => this._loop(ts));
          }
        });
        this.intersectionObserver.observe(this.container);
      }
    }

    _resize() {
      const w = Math.max(this.container.clientWidth, 1);
      const h = Math.max(this.container.clientHeight, 1);
      const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
      this.width = w;
      this.height = h;
      this.dpr = dpr;
      this.canvas.width = Math.round(w * dpr);
      this.canvas.height = Math.round(h * dpr);
    }

    _stepSpring(state, target, stiffness, damping, dt) {
      const et = Math.max(this.opts.softness, 0.1);
      const k = stiffness / et;
      const d = damping / Math.sqrt(et);
      state.v.x += (target.x - state.p.x) * k * dt;
      state.v.y += (target.y - state.p.y) * k * dt;
      state.v.z += ((target.z || 0) - state.p.z) * k * dt;
      const decay = Math.exp(-d * dt);
      state.v.x *= decay;
      state.v.y *= decay;
      state.v.z *= decay;
      state.p.x += state.v.x * dt;
      state.p.y += state.v.y * dt;
      state.p.z += state.v.z * dt;
    }

    // -------------------------------------------------------------------------
    // Public API
    // -------------------------------------------------------------------------
    poke() {
      this.pokeTimer = 0;
      this.at += 3.5;
      if (typeof this.opts.onPoke === "function") {
        this.opts.onPoke(this);
      }
    }

    setMood(mood) {
      if (!VALID_MOODS.includes(mood)) return;
      if (mood === "listening" && this.opts.mood !== "listening") {
        this.listenNodTimer = 0;
      }
      this.opts.mood = mood;
      if (typeof this.opts.onMoodChange === "function") {
        this.opts.onMoodChange(mood, this);
      }
    }

    setVariant(variantName) {
      if (variantName === "with-purple" || variantName === true) {
        this.opts.variant = "with-purple";
      } else if (variantName === "without-purple" || variantName === false) {
        this.opts.variant = "without-purple";
      }
    }

    setSkin(skinName) {
      this.setVariant(skinName);
    }

    setPurpleLayer(enabled) {
      this.opts.variant = enabled ? "with-purple" : "without-purple";
    }

    setFace(faceType) {
      if (FACE_PRESETS[faceType]) {
        this.opts.face = faceType;
      }
    }

    setMouth(mouthType) {
      if (["default", "happy", "open"].includes(mouthType)) {
        this.opts.mouth = mouthType;
      }
    }

    setTheme(theme) {
      this.opts.theme = theme;
    }

    setAccessories(list) {
      this.opts.accessories = Array.isArray(list) ? [...list] : [];
    }

    toggleAccessory(name) {
      const idx = this.opts.accessories.indexOf(name);
      if (idx >= 0) {
        this.opts.accessories.splice(idx, 1);
      } else {
        this.opts.accessories.push(name);
      }
      return [...this.opts.accessories];
    }

    setShyOnHover(enabled) {
      this.opts.shyOnHover = Boolean(enabled);
    }

    setRingColors(ringTop, ringBottom) {
      this.opts.ringTop = ringTop || null;
      this.opts.ringBottom = ringBottom || null;
      if (this.manualPaused) {
        const base = this._getBasePalette();
        this._ringTopRgb = parseHexRgb(this.opts.ringTop || base.ringTop);
        this._ringBottomRgb = parseHexRgb(this.opts.ringBottom || base.ringBottom);
      }
    }

    setPaused(paused) {
      const want = Boolean(paused);
      if (this.manualPaused === want) return;
      this.manualPaused = want;
      this.isVisible = !want;
      if (this.isVisible) {
        this._resize();
        if (!this.rafId) {
          this.lastFrameTime = performance.now();
          this.rafId = requestAnimationFrame((ts) => this._loop(ts));
        }
      } else if (this.rafId) {
        cancelAnimationFrame(this.rafId);
        this.rafId = 0;
      }
    }

    destroy() {
      if (this.rafId) cancelAnimationFrame(this.rafId);
      this.rafId = 0;
      this.isVisible = false;
      window.removeEventListener("pointermove", this._onPointerMove);
      document.removeEventListener("pointerleave", this._onPointerLeaveDocument);
      this.canvas.removeEventListener("pointerdown", this._onPointerDown);
      this.resizeObserver?.disconnect();
      this.intersectionObserver?.disconnect();
      this.canvas.remove();
    }

    // -------------------------------------------------------------------------
    // Simulation Update (Exact LFM3 Spring & Rotation Equations)
    // -------------------------------------------------------------------------
    _loop(now) {
      this.rafId = 0;
      if (!this.isVisible) return;

      const dt = Math.min((now - this.lastFrameTime) / 1000, 1 / 20);
      this.lastFrameTime = now;
      this._update(dt);
      this._render();

      this.rafId = requestAnimationFrame((ts) => this._loop(ts));
    }

    _update(dt) {
      const zt = this.reducedMotion;
      const L = (this.time += dt * zt);
      const f = this.opts;
      const variant = this._getVariantConfig();

      // Smoothly transition purple outer layer on/off and ring color
      const targetPurple = variant.hasPurpleLayer ? 1 : 0;
      this.purpleLayerWeight += (targetPurple - this.purpleLayerWeight) * Math.min(1, dt * 7);

      const basePal = this._getBasePalette();
      const targetTop = parseHexRgb(f.ringTop || basePal.ringTop);
      const targetBot = parseHexRgb(f.ringBottom || basePal.ringBottom);
      const colStep = Math.min(1, dt * 8);
      if (targetTop && this._ringTopRgb) {
        for (let i = 0; i < 3; i++) this._ringTopRgb[i] += (targetTop[i] - this._ringTopRgb[i]) * colStep;
      } else {
        this._ringTopRgb = targetTop;
      }
      if (targetBot && this._ringBottomRgb) {
        for (let i = 0; i < 3; i++) this._ringBottomRgb[i] += (targetBot[i] - this._ringBottomRgb[i]) * colStep;
      } else {
        this._ringBottomRgb = targetBot;
      }

      this.pointerNearSmooth += (this.pointerNear * zt - this.pointerNearSmooth) * Math.min(1, dt * 3);
      const P = Math.sin(L * f.bobSpeed * TAU) * f.bob;
      this.bobOffset = P;

      // Determine active mood
      let activeMood = f.mood;
      if (activeMood === "auto") {
        activeMood = f.shyOnHover && this.isHovered ? "shy" : "idle";
      }
      this.effectiveMood = activeMood;

      for (const k of Object.keys(this.moodWeights)) {
        this.moodWeights[k] += ((activeMood === k ? 1 : 0) - this.moodWeights[k]) * Math.min(1, dt * 4.5);
      }
      const dtMood = this.moodWeights;

      // Pointer lean vector
      const distSq = this.pointerWorld.x * this.pointerWorld.x + this.pointerWorld.y * this.pointerWorld.y;
      const Q = Math.exp(-distSq / 3) * this.pointerNearSmooth;
      const len = Math.hypot(this.pointerWorld.x, this.pointerWorld.y) || 1;
      const clampedLen = Math.min(len, 1.5);
      const leanTarget = {
        x: (this.pointerWorld.x / len) * clampedLen * f.lean * Q,
        y: (this.pointerWorld.y / len) * clampedLen * f.lean * Q,
        z: 0,
      };

      this._stepSpring(this.springBase, leanTarget, 10, 3.2, dt);
      this._stepSpring(this.springHead, this.springBase.p, 14, 3.0, dt);

      // Poke squish harmonic oscillators
      this.at += (-90 * this.D - 7 * this.at) * dt;
      this.D += this.at * dt;
      this.J += (-60 * (this.X - this.D) - 6 * this.J) * dt;
      this.X += this.J * dt;

      if (this.pokeTimer >= 0) {
        this.pokeTimer += dt;
        if (this.pokeTimer > 1.05) this.pokeTimer = -1;
      }

      const Kt = 0.5 + 0.5 * Math.cos(L * TAU * 2.0);
      this.clapWave = Kt;

      // Outer ring & inner head positions
      this.ringPos = {
        x: this.springBase.p.x,
        y: this.springBase.p.y + P,
      };
      this.headPos = {
        x: this.springHead.p.x * 1.2,
        y: this.springHead.p.y * 1.2 + P + (1 - Kt) * 0.045 * dtMood.happy,
      };
      this.headRoll = -(this.springHead.p.x - this.springBase.p.x) * 1.5;

      // Idle wandering look vs pointer tracking (exact LFM3 equations)
      const isIdle = L - this.lastMoveTime > 2.5;
      const idleMul = f.idleLook / Math.max(f.lookStrength, 0.001);
      this.idleGaze.x = (Math.sin(L * 0.33) + Math.sin(L * 0.71) * 0.4) * idleMul;
      this.idleGaze.y = Math.sin(L * 0.27 + 1) * 0.7 * idleMul;

      const desiredLook = isIdle ? this.idleGaze : this.targetLook;
      const lookRate = Math.min(1, dt * (isIdle ? 1.2 : 2.2));
      this.smoothLook.x += (desiredLook.x - this.smoothLook.x) * lookRate;
      this.smoothLook.y += (desiredLook.y - this.smoothLook.y) * lookRate;

      // Thinking spin (jt in LFM3)
      if (activeMood === "thinking") {
        this.thinkSpinAngle += dtMood.thinking * f.thinkSpin * dt * zt;
      } else {
        const nextTurn = Math.ceil(this.thinkSpinAngle / TAU - 0.001) * TAU;
        this.thinkSpinAngle += (nextTurn - this.thinkSpinAngle) * Math.min(1, dt * 2.5);
      }

      // Listening nod (ke in LFM3)
      let ke = 0;
      if (this.listenNodTimer >= 0) {
        this.listenNodTimer += dt;
        ke =
          this.listenNodTimer < 1.3
            ? Math.sin(this.listenNodTimer * TAU * 1.55) * 0.26 * (1 - this.listenNodTimer / 1.3)
            : 0;
        if (this.listenNodTimer >= 1.3) this.listenNodTimer = -1;
      }

      const St = Math.max(
        dtMood.listening,
        dtMood.thinking,
        dtMood.happy,
        dtMood.shy,
        dtMood.distressed
      );
      const Zt = 1 - St * 0.75;
      const bt = dtMood.listening;
      const Mt =
        Math.sin(L * TAU * f.shakeRate) * f.shakeAmount * easeInOutCubic(dtMood.distressed) +
        Math.sin(L * TAU * 4.2) * 0.04 * easeInOutCubic(dtMood.shy);

      // 3D Euler angles of the head group
      this.rotY =
        this.smoothLook.x * f.lookStrength * Zt +
        this.thinkSpinAngle +
        bt * (0.16 + Math.sin(L * 0.8) * 0.07) +
        Mt;

      this.rotX =
        -this.smoothLook.y * f.lookStrength * 0.8 * Zt -
        bt * 0.08 +
        ke +
        dtMood.happy * -0.12 +
        dtMood.shy * 0.22;

      this.rotZ =
        Math.sin(L * 0.5) * 0.04 +
        bt * (f.listenTilt + Math.sin(L * 0.6) * 0.05) +
        this.headRoll;

      // Natural blinking
      if (this.blinkProgress < 0 && L > this.nextBlinkTime) {
        this.blinkProgress = 0;
      }
      let blinkVal = 0;
      if (this.blinkProgress >= 0) {
        this.blinkProgress += dt;
        const Nt = this.blinkProgress / 0.18;
        blinkVal = Nt < 1 ? Math.sin(Nt * Math.PI) : 0;
        if (Nt >= 1) {
          this.blinkProgress = -1;
          this.nextBlinkTime =
            L + (Math.random() < 0.2 ? 0.25 : f.blinkEvery * (0.6 + Math.random() * 0.8));
        }
      }
      this.currentBlink = blinkVal;

      const wantClosed = f.accessories.includes("eyesClosed") ? 1 : 0;
      this.eyesClosedWeight += (wantClosed - this.eyesClosedWeight) * Math.min(1, dt * 8);
    }

    // -------------------------------------------------------------------------
    // 3D Rendering with True Back-Hemisphere / Horizon Clipping
    // -------------------------------------------------------------------------
    _project(x, y, z) {
      const camZ = this._currentCamZ;
      const persp = camZ / Math.max(camZ - z, 0.5);
      return {
        x: this._cx + x * persp * this._scale,
        y: this._cy - y * persp * this._scale,
        s: persp,
        z,
      };
    }

    _render() {
      const ctx = this.ctx;
      const w = this.width;
      const h = this.height;
      const dpr = this.dpr;
      const variant = this._getVariantConfig();
      const pal = this._getPalette();
      const f = this.opts;

      ctx.save();
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, w, h);

      const hasLargeGear =
        f.accessories.includes("hat") ||
        f.accessories.includes("magnifier");
      const ne = hasLargeGear ? 1.18 : 1.0;
      this._currentCamZ = f.cameraZ * ne;
      this._cx = w / 2;
      this._cy = h / 2;
      this._scale = (Math.min(w, h) * 0.39) / ne;

      const ringX = this.ringPos.x;
      const ringY = this.ringPos.y;
      const headX = this.headPos.x;
      const headY = this.headPos.y;

      // 1. Headphones Back Band
      if (f.accessories.includes("headphones")) {
        this._drawHeadphonesBack(ctx, ringX, ringY, pal);
      }

      // 2. Purple Outer Layer (when purpleLayerWeight > 0.01)
      if (this.purpleLayerWeight > 0.01) {
        this._drawPurpleOuterLayer(ctx, ringX, ringY, pal, this.purpleLayerWeight * 0.75);
      }

      // 3. Inner 3D Head Sphere + 3D Horizon-Clipped Face
      const bodyRadius = lerp(0.82, 0.745, this.purpleLayerWeight) * (1 + this.X * 0.04);
      this._drawHeadAndFace3D(ctx, headX, headY, bodyRadius, pal);

      // 4. Front Accessories (Headphones Earcups, Hat, Magnifier)
      if (f.accessories.includes("headphones")) {
        this._drawHeadphonesFront(ctx, ringX, ringY, pal);
      }
      if (f.accessories.includes("hat")) {
        this._drawHat(ctx, headX, headY, pal);
      }
      if (f.accessories.includes("magnifier")) {
        this._drawMagnifier(ctx, ringX, ringY, pal);
      }

      ctx.restore();
    }

    _drawPurpleOuterLayer(ctx, rx, ry, pal, alpha) {
      const p = this._project(rx, ry, 0);
      const R = (1.0 + this.D * 0.05) * this._scale * p.s;

      ctx.save();
      ctx.globalAlpha = alpha;
      const grad = ctx.createLinearGradient(p.x, p.y - R, p.x, p.y + R);
      grad.addColorStop(0, pal.ringTop);
      grad.addColorStop(1, pal.ringBottom);
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(p.x, p.y, R, 0, TAU);
      ctx.fill();
      ctx.restore();
    }

    _drawArcShine(ctx, cx, cy, radius, centerAngle, halfSpan, thickness, color, alpha) {
      ctx.save();
      ctx.strokeStyle = color;
      ctx.globalAlpha = alpha;
      ctx.lineWidth = thickness;
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.arc(cx, cy, radius, centerAngle - halfSpan, centerAngle + halfSpan);
      ctx.stroke();
      ctx.restore();
    }

    _drawHeadAndFace3D(ctx, hx, hy, bodyR, pal) {
      const pHead = this._project(hx, hy, 0);
      const R = bodyR * this._scale * pHead.s;

      ctx.save();

      // Clip both the head shading AND the 3D face to the sphere silhouette circle!
      ctx.beginPath();
      ctx.arc(pHead.x, pHead.y, R, 0, TAU);
      ctx.clip();

      // 1. Shaded sphere base (bottom-left crescent shadow matching LFM3's $g shader)
      const shadeGrad = ctx.createLinearGradient(pHead.x, pHead.y - R, pHead.x, pHead.y + R);
      shadeGrad.addColorStop(0, pal.headShadeTop);
      shadeGrad.addColorStop(1, pal.headShadeBottom);
      ctx.fillStyle = shadeGrad;
      ctx.fillRect(pHead.x - R, pHead.y - R, R * 2, R * 2);

      // 2. Lit disc nudged up-right (+0.1, -0.1) leaving a soft crescent of shade along bottom-left
      const litGrad = ctx.createLinearGradient(pHead.x, pHead.y - R, pHead.x, pHead.y + R);
      litGrad.addColorStop(0, pal.headTop);
      litGrad.addColorStop(1, pal.headBottom);
      ctx.fillStyle = litGrad;
      ctx.beginPath();
      ctx.arc(pHead.x + R * 0.1, pHead.y - R * 0.1, R * 0.9, 0, TAU);
      ctx.fill();

      // 3. Curved specular shine following top-right edge (LFM3 arcShine(N.xy, 0.74, 0.98, 0.36, 0.085))
      this._drawArcShine(ctx, pHead.x, pHead.y, R * 0.74, -0.98, 0.36, R * 0.15, pal.highlight, 0.85);

      // 4. 3D Projected Face with Horizon / Back-Hemisphere Clipping
      // Horizon plane of a sphere of radius `bodyR` viewed from `camZ`: z_horizon = bodyR² / camZ
      const zHorizon = (bodyR * bodyR) / this._currentCamZ;
      const rotMat = createRotationMatrix(this.rotX, this.rotY, this.rotZ);

      this._drawLfm3Face3D(ctx, hx, hy, bodyR, rotMat, zHorizon, pal);

      ctx.restore();

      // 5. Glasses (drawn outside sphere clip so frames can slightly overhang the rim, but clipped to front hemisphere z >= zHorizon!)
      if (this.opts.accessories.includes("glasses")) {
        const zHorizon = (bodyR * bodyR) / this._currentCamZ;
        const rotMat = createRotationMatrix(this.rotX, this.rotY, this.rotZ);
        this._drawGlasses3D(ctx, hx, hy, bodyR, rotMat, zHorizon, pal);
      }
    }

    _fillClippedPolygon3D(ctx, localPts3D, hx, hy, rotMat, zHorizon) {
      const rotated = localPts3D.map((pt) => applyMat3(rotMat, pt.x, pt.y, pt.z));
      const visible = clipPolygonToHorizon(rotated, zHorizon);
      if (visible.length < 3) return false;

      ctx.beginPath();
      for (let i = 0; i < visible.length; i++) {
        const p = this._project(hx + visible[i].x, hy + visible[i].y, visible[i].z);
        if (i === 0) ctx.moveTo(p.x, p.y);
        else ctx.lineTo(p.x, p.y);
      }
      ctx.closePath();
      ctx.fill();
      return true;
    }

    _strokeClippedPolyline3D(ctx, localPts3D, hx, hy, rotMat, zHorizon) {
      const rotated = localPts3D.map((pt) => applyMat3(rotMat, pt.x, pt.y, pt.z));
      const segments = clipPolylineToHorizon(rotated, zHorizon);
      if (segments.length === 0) return false;

      for (const seg of segments) {
        if (seg.length < 2) continue;
        ctx.beginPath();
        for (let i = 0; i < seg.length; i++) {
          const p = this._project(hx + seg[i].x, hy + seg[i].y, seg[i].z);
          if (i === 0) ctx.moveTo(p.x, p.y);
          else ctx.lineTo(p.x, p.y);
        }
        ctx.stroke();
      }
      return true;
    }

    _drawLfm3Face3D(ctx, hx, hy, bodyR, rotMat, zHorizon, pal) {
      const face = FACE_PRESETS[this.opts.face] || FACE_PRESETS.soft;
      const dtMood = this.moodWeights;

      // Poke reaction (cn in LFM3)
      const cn =
        this.pokeTimer >= 0
          ? Math.min(1, this.pokeTimer * 14) * (1 - smoothstep(0.75, 1.05, this.pokeTimer))
          : 0;
      const Le = Math.max(dtMood.distressed, dtMood.shy, this.eyesClosedWeight);
      const Ee = Math.max(Le, cn, dtMood.happy * 0.85);
      const showEyeArcs = Ee > 0.55;
      const arcKind = cn > Le || dtMood.happy > Le ? "happy" : "sad";
      const morphSquint = clamp(Math.max(this.currentBlink, Ee), 0, 0.9);

      // Face orientation angle `f` between the two eyes (exact LFM3 formula)
      const [d, p] = [face.eyes[0].center, face.eyes[1].center];
      const faceAngle = Math.atan2(d[1] - p[1], d[0] - p[0]);
      const gVec = [Math.sin(faceAngle), -Math.cos(faceAngle)];
      const midEye = [(d[0] + p[0]) / 2, (d[1] + p[1]) / 2];

      // Optional subtle blush when shy
      if (dtMood.shy > 0.05) {
        ctx.save();
        ctx.fillStyle = pal.blush;
        ctx.globalAlpha = dtMood.shy * 0.45;
        for (const eye of face.eyes) {
          const bx = eye.center[0] + gVec[0] * 0.16;
          const by = eye.center[1] + gVec[1] * 0.16;
          const blushPts = [];
          for (let i = 0; i < 20; i++) {
            const a = (i / 20) * TAU;
            blushPts.push(
              projectToSphere3D(
                Math.cos(a) * 0.11,
                Math.sin(a) * 0.065,
                bx,
                by,
                faceAngle,
                bodyR,
                1.002
              )
            );
          }
          this._fillClippedPolygon3D(ctx, blushPts, hx, hy, rotMat, zHorizon);
        }
        ctx.restore();
      }

      // 1. Eyes or Eye Arcs (happy / sad)
      ctx.fillStyle = pal.eye;
      ctx.strokeStyle = pal.eye;

      for (const eye of face.eyes) {
        const [cx, cy] = eye.center;
        if (!showEyeArcs) {
          // Morph target squint: scale minor axis toward 0.1 just like LFM3
          const rx = eye.rx >= eye.ry ? lerp(eye.rx, eye.rx * 0.1, morphSquint) : eye.rx;
          const ry = eye.ry > eye.rx ? lerp(eye.ry, eye.ry * 0.1, morphSquint) : eye.ry;
          const pts3D = [];
          const steps = 36;
          for (let i = 0; i < steps; i++) {
            const a = (i / steps) * TAU;
            pts3D.push(
              projectToSphere3D(Math.cos(a) * rx, Math.sin(a) * ry, cx, cy, eye.rot, bodyR, 1.003)
            );
          }
          this._fillClippedPolygon3D(ctx, pts3D, hx, hy, rotMat, zHorizon);
        } else {
          // LFM3 quadratic Bezier eye arcs: sad: [0.024, -0.073], happy: [-0.033, 0.073]
          const [yEnd, yCtrl] = arcKind === "happy" ? [-0.033, 0.073] : [0.024, -0.073];
          const arcPts3D = [];
          const steps = 14;
          for (let i = 0; i <= steps; i++) {
            const t = i / steps;
            const u = 1 - t;
            const lx = u * u * -0.13 + 2 * u * t * 0 + t * t * 0.13;
            const ly = u * u * yEnd + 2 * u * t * yCtrl + t * t * yEnd;
            arcPts3D.push(projectToSphere3D(lx, ly, cx, cy, faceAngle, bodyR, 1.004));
          }
          const pRef = this._project(hx, hy, bodyR);
          ctx.save();
          ctx.lineWidth = Math.max(2.2, 0.053 * bodyR * this._scale * pRef.s);
          ctx.lineCap = "round";
          ctx.lineJoin = "round";
          this._strokeClippedPolyline3D(ctx, arcPts3D, hx, hy, rotMat, zHorizon);
          ctx.restore();
        }
      }

      // 2. Mouth (LFM3 open diamond bounce `pe` vs happy smile vs caret mouth)
      let pe = 0;
      if (this.pokeTimer >= 0) {
        const Nt = 1 - Math.exp(-this.pokeTimer * 18) * Math.cos(this.pokeTimer * 14);
        const he = smoothstep(0.7, 1.05, this.pokeTimer);
        pe = Math.max(0, Nt * (1 - he));
      }
      pe = Math.max(
        pe,
        dtMood.happy * (0.9 + (1 - this.clapWave) * 0.1),
        this.opts.mouth === "open" ? 1 : 0
      );

      if (pe > 0.08) {
        // LFM3 Rounded-Diamond Open Mouth (`jg(1, 1, 0.22)` scaled by `C=0.11, I=0.14`)
        const scaleOpen = lerp(0.12, 1.0, clamp(pe, 0, 1));
        const C = 0.11 * scaleOpen;
        const I = 0.14 * scaleOpen;
        const wx = midEye[0] + gVec[0] * face.diamondDrop;
        const wy = midEye[1] + gVec[1] * face.diamondDrop;

        const diamond2D = this._sampleRoundedDiamond(C, I, 0.22, 8);
        const diamond3D = diamond2D.map(([lx, ly]) =>
          projectToSphere3D(lx, ly, wx, wy, faceAngle, bodyR, 1.003)
        );
        ctx.fillStyle = pal.eye;
        this._fillClippedPolygon3D(ctx, diamond3D, hx, hy, rotMat, zHorizon);
      }

      if (pe < 0.15) {
        const pRef = this._project(hx, hy, bodyR);
        ctx.save();
        ctx.strokeStyle = pal.mouth;
        ctx.lineCap = "round";
        ctx.lineJoin = "round";

        if (this.opts.mouth === "happy" && dtMood.shy < 0.4 && dtMood.distressed < 0.4) {
          // LFM3 Asymmetric Smile Arms
          const { apex, arms, width } = face.smile;
          ctx.lineWidth = Math.max(2.0, width * 2 * bodyR * this._scale * pRef.s);
          for (const [ang, len] of arms) {
            const armPts = [];
            for (let i = 0; i <= 8; i++) {
              const dLen = (i / 8) * len;
              armPts.push(projectToSphere3D(dLen, 0, apex[0], apex[1], ang, bodyR, 1.003));
            }
            this._strokeClippedPolyline3D(ctx, armPts, hx, hy, rotMat, zHorizon);
          }
        } else {
          // LFM3 Caret Mouth
          const { apex, arm, spread, width } = face.caret;
          ctx.lineWidth = Math.max(2.0, width * 2 * bodyR * this._scale * pRef.s);
          for (const Z of [-1, 1]) {
            const j = faceAngle - Math.PI / 2 + Z * (Math.PI / 2 - spread);
            const armPts = [];
            for (let i = 0; i <= 8; i++) {
              const dLen = (i / 8) * arm;
              armPts.push(projectToSphere3D(dLen, 0, apex[0], apex[1], j, bodyR, 1.003));
            }
            this._strokeClippedPolyline3D(ctx, armPts, hx, hy, rotMat, zHorizon);
          }
        }
        ctx.restore();
      }
    }

    _sampleRoundedDiamond(rx, ry, roundness, segsPerCorner = 8) {
      const verts = [
        [0, ry],
        [rx, 0],
        [0, -ry],
        [-rx, 0],
      ];
      const lerp2 = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
      const pts = [];
      for (let o = 0; o < 4; o++) {
        const prev = verts[(o + 3) % 4];
        const curr = verts[o];
        const next = verts[(o + 1) % 4];
        const h = lerp2(curr, prev, roundness);
        const d = lerp2(curr, next, roundness);
        for (let s = 0; s <= segsPerCorner; s++) {
          const t = s / segsPerCorner;
          const u = 1 - t;
          pts.push([
            u * u * h[0] + 2 * u * t * curr[0] + t * t * d[0],
            u * u * h[1] + 2 * u * t * curr[1] + t * t * d[1],
          ]);
        }
      }
      return pts;
    }

    // -------------------------------------------------------------------------
    // LFM3 Accessories (Glasses, Headphones, Hat, Magnifier)
    // -------------------------------------------------------------------------
    _drawGlasses3D(ctx, hx, hy, bodyR, rotMat, zHorizon, pal) {
      const face = FACE_PRESETS[this.opts.face] || FACE_PRESETS.soft;
      const [_, m] = face.eyes.map((e) => e.center);
      const u = 0.277;
      const S = 0.049;
      const M = _[0] - m[0];
      const x = _[1] - m[1];
      const F = Math.hypot(M, x) || 1;
      const w = [M / F, x / F];
      const C = [-w[1], w[0]];

      const pRef = this._project(hx, hy, bodyR);
      const strokeW = Math.max(2.2, S * bodyR * this._scale * pRef.s);

      ctx.save();
      for (const G of [m, _]) {
        const ringPts3D = [];
        const steps = 40;
        for (let i = 0; i <= steps; i++) {
          const a = (i / steps) * TAU;
          ringPts3D.push(
            projectToSphere3D(Math.cos(a) * u, Math.sin(a) * u, G[0], G[1], 0, bodyR, 1.008)
          );
        }

        // Translucent lens fill (clipped to front horizon)
        ctx.fillStyle = "rgba(255, 255, 255, 0.22)";
        this._fillClippedPolygon3D(ctx, ringPts3D, hx, hy, rotMat, zHorizon);

        // Glasses rim stroke (clipped to front horizon)
        ctx.strokeStyle = pal.glasses;
        ctx.lineWidth = strokeW;
        ctx.lineCap = "round";
        this._strokeClippedPolyline3D(ctx, ringPts3D, hx, hy, rotMat, zHorizon);
      }

      // Bridge & temples (exact LFM3 coordinates)
      const I = [m[0] + w[0] * u, m[1] + w[1] * u];
      const E = [_[0] - w[0] * u, _[1] - w[1] * u];
      const y = [(I[0] + E[0]) / 2 + C[0] * 0.09, (I[1] + E[1]) / 2 + C[1] * 0.09];

      const bridgePts3D = [];
      for (let i = 0; i <= 10; i++) {
        const t = i / 10;
        const inv = 1 - t;
        const bx = inv * inv * I[0] + 2 * inv * t * y[0] + t * t * E[0];
        const by = inv * inv * I[1] + 2 * inv * t * y[1] + t * t * E[1];
        bridgePts3D.push(projectToSphere3D(bx, by, 0, 0, 0, bodyR, 1.008));
      }

      const templeLeft3D = [
        projectToSphere3D(m[0] - w[0] * u, m[1] - w[1] * u, 0, 0, 0, bodyR, 1.008),
        projectToSphere3D(
          m[0] - w[0] * (u + 0.25),
          m[1] - w[1] * (u + 0.25) - 0.02,
          0,
          0,
          0,
          bodyR,
          1.008
        ),
      ];
      const templeRight3D = [
        projectToSphere3D(_[0] + w[0] * u, _[1] + w[1] * u, 0, 0, 0, bodyR, 1.008),
        projectToSphere3D(_[0] + w[0] * (u + 0.09), _[1] + w[1] * (u + 0.09), 0, 0, 0, bodyR, 1.008),
      ];

      ctx.strokeStyle = pal.glasses;
      ctx.lineWidth = strokeW;
      ctx.lineCap = "round";
      this._strokeClippedPolyline3D(ctx, bridgePts3D, hx, hy, rotMat, zHorizon);
      this._strokeClippedPolyline3D(ctx, templeLeft3D, hx, hy, rotMat, zHorizon);
      this._strokeClippedPolyline3D(ctx, templeRight3D, hx, hy, rotMat, zHorizon);
      ctx.restore();
    }

    _drawHeadphonesBack(ctx, rx, ry, pal) {
      const p = this._project(rx, ry, 0);
      const R = (172 / 165) * this._scale * p.s;
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(-12 * DEG2RAD);
      ctx.strokeStyle = pal.gearMain;
      ctx.lineWidth = Math.max(3.5, (12 / 165) * this._scale * p.s);
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.arc(0, -(13.9 / 165) * this._scale * p.s, R, Math.PI + 8.7 * DEG2RAD, -8.7 * DEG2RAD);
      ctx.stroke();
      ctx.restore();
    }

    _drawHeadphonesFront(ctx, rx, ry, pal) {
      const p = this._project(rx, ry, 0.15);
      const s = (this._scale * p.s) / 165;
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(-12 * DEG2RAD);

      for (const M of [-1, 1]) {
        const cupX = (M < 0 ? 87 : 425) - 256;
        const padX = (M < 0 ? 102.5 : 409.5) - 256;
        const cupY = 270 - 276;

        ctx.fillStyle = pal.gearPad;
        this._roundRect(ctx, (padX - 4.5) * s, (cupY - 28) * s, 9 * s, 56 * s, 4.5 * s);
        ctx.fill();

        ctx.fillStyle = pal.gearMain;
        this._roundRect(ctx, (cupX - 17) * s, (cupY - 42) * s, 34 * s, 84 * s, 17 * s);
        ctx.fill();
      }
      ctx.restore();
    }

    _drawHat(ctx, hx, hy, pal) {
      const baseX = (228 - 256) / 165;
      const baseY = (276 - 146) / 165;
      const p = this._project(hx + baseX, hy + baseY, 0.9);
      const s = ((this._scale * p.s) / 165) * 0.86;

      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(-(12 * DEG2RAD + this.headRoll));
      ctx.scale(s, s);

      // Brim
      ctx.fillStyle = pal.gearBrim;
      ctx.beginPath();
      ctx.ellipse(0, 0, 150, 24, 0, 0, TAU);
      ctx.fill();

      // Crown with vertical gradient
      const grad = ctx.createLinearGradient(0, -120, 0, 10);
      grad.addColorStop(0, pal.gearTop);
      grad.addColorStop(1, pal.gearMain);
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.moveTo(-82, -2);
      ctx.bezierCurveTo(-88, -62, -72, -112, -32, -118);
      ctx.bezierCurveTo(-12, -121, -3, -103, 0, -103);
      ctx.bezierCurveTo(3, -103, 12, -121, 32, -118);
      ctx.bezierCurveTo(72, -112, 88, -62, 82, -2);
      ctx.bezierCurveTo(40, 8, -40, 8, -82, -2);
      ctx.closePath();
      ctx.fill();

      // Ribbon band
      ctx.fillStyle = pal.gearPad;
      ctx.beginPath();
      ctx.moveTo(-85, -30);
      ctx.bezierCurveTo(-40, -20, 40, -20, 85, -30);
      ctx.lineTo(84, -8);
      ctx.bezierCurveTo(40, 2, -40, 2, -84, -8);
      ctx.closePath();
      ctx.fill();

      // Hat sheen
      ctx.strokeStyle = "rgba(255, 255, 255, 0.22)";
      ctx.lineWidth = 8;
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.moveTo(-52, -62);
      ctx.bezierCurveTo(-50, -86, -38, -104, -24, -108);
      ctx.stroke();

      ctx.restore();
    }

    _drawMagnifier(ctx, rx, ry, pal) {
      // Positioned cleanly beside the mascot without hands
      const p = this._project(rx - 0.95, ry - 0.72, 0.75);
      const s = (this._scale * p.s) / 165;

      const h1 = [(84 - 48) * s, -(468 - 432) * s];
      const h2 = [(22 - 48) * s, -(468 - 494) * s];
      const cx = (126 - 48) * s;
      const cy = -(468 - 390) * s;
      const rGlass = 60 * s;

      ctx.save();
      ctx.translate(p.x, p.y);

      // Handle
      ctx.strokeStyle = pal.gearHandle;
      ctx.lineWidth = Math.max(3, 17 * s);
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.moveTo(h1[0], h1[1]);
      ctx.lineTo(h2[0], h2[1]);
      ctx.stroke();

      // Glass fill
      ctx.fillStyle = "rgba(255, 255, 255, 0.35)";
      ctx.beginPath();
      ctx.arc(cx, cy, rGlass, 0, TAU);
      ctx.fill();

      // Rim
      ctx.strokeStyle = pal.gearMain;
      ctx.lineWidth = Math.max(2.5, 12 * s);
      ctx.beginPath();
      ctx.arc(cx, cy, rGlass, 0, TAU);
      ctx.stroke();

      // White glint arc
      ctx.strokeStyle = "rgba(255, 255, 255, 0.9)";
      ctx.lineWidth = Math.max(2, 8 * s);
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.arc(cx, cy, 40 * s, -149.5 * DEG2RAD, -96 * DEG2RAD);
      ctx.stroke();

      ctx.restore();
    }

    _roundRect(ctx, x, y, w, h, r) {
      const rad = Math.min(r, w / 2, h / 2);
      ctx.beginPath();
      ctx.moveTo(x + rad, y);
      ctx.lineTo(x + w - rad, y);
      ctx.arcTo(x + w, y, x + w, y + rad, rad);
      ctx.lineTo(x + w, y + h - rad);
      ctx.arcTo(x + w, y + h, x + w - rad, y + h, rad);
      ctx.lineTo(x + rad, y + h);
      ctx.arcTo(x, y + h, x, y + h - rad, rad);
      ctx.lineTo(x, y + rad);
      ctx.arcTo(x, y, x + rad, y, rad);
      ctx.closePath();
    }
  }

  // ---------------------------------------------------------------------------
  // <boo-mascot> Web Component
  // ---------------------------------------------------------------------------
  if (typeof customElements !== "undefined" && !customElements.get("boo-mascot")) {
    class BooMascotElement extends HTMLElement {
      static get observedAttributes() {
        return ["variant", "skin", "purple-layer", "face", "theme", "mood", "accessories", "shy-on-hover"];
      }

      connectedCallback() {
        if (!this.style.display) this.style.display = "inline-block";
        if (!this.style.width && !this.clientWidth) this.style.width = "160px";
        if (!this.style.height && !this.clientHeight) this.style.height = "160px";

        const accAttr = this.getAttribute("accessories");
        const accessories = accAttr ? accAttr.split(/\s+/).filter(Boolean) : [];
        const purpleAttr = this.getAttribute("purple-layer");
        const variantAttr = this.getAttribute("variant") || this.getAttribute("skin");

        this.instance = new BooMascotInstance(this, {
          variant: variantAttr || "with-purple",
          purpleLayer: purpleAttr !== null ? purpleAttr !== "false" : undefined,
          face: this.getAttribute("face") || "soft",
          theme: this.getAttribute("theme") || "auto",
          mood: this.getAttribute("mood") || "auto",
          accessories,
          shyOnHover: this.getAttribute("shy-on-hover") === "true",
        });
      }

      disconnectedCallback() {
        this.instance?.destroy();
        this.instance = null;
      }

      attributeChangedCallback(name, oldVal, newVal) {
        if (!this.instance || oldVal === newVal) return;
        if (name === "variant" || name === "skin") {
          this.instance.setVariant(newVal || "with-purple");
        } else if (name === "purple-layer") {
          this.instance.setPurpleLayer(newVal !== "false");
        } else if (name === "face") {
          this.instance.setFace(newVal || "soft");
        } else if (name === "theme") {
          this.instance.setTheme(newVal || "auto");
        } else if (name === "mood") {
          this.instance.setMood(newVal || "auto");
        } else if (name === "accessories") {
          this.instance.setAccessories(newVal ? newVal.split(/\s+/).filter(Boolean) : []);
        } else if (name === "shy-on-hover") {
          this.instance.setShyOnHover(newVal === "true");
        }
      }

      poke() {
        this.instance?.poke();
      }
    }

    customElements.define("boo-mascot", BooMascotElement);
  }

  const BooMascot = {
    mount(container, options) {
      return new BooMascotInstance(container, options);
    },
    Instance: BooMascotInstance,
    VARIANTS,
    SKINS: VARIANTS,
    MOODS: VALID_MOODS,
  };

  global.BooMascot = BooMascot;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = BooMascot;
  }
})(typeof window !== "undefined" ? window : globalThis);
