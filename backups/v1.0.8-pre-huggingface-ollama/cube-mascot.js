/**
 * CubeMascot — 100% Local, Zero-Dependency Matte Black 3D Living Cube
 * - ZERO mouse tracking (fixed isometric 3D angle, never follows the cursor)
 * - 4 Inner Radial Core Light Colors: "red" | "white" | "purple" | "azure"
 * - 4 Animation States:
 *     "closed"    — Seamless solid matte black cube floating gently (cut lines invisible)
 *     "opened"    — 4 vertical floating slices with gentle wave & on/off radial core light
 *     "loading"   — Fast traveling wave across the 4 vertical slices + rapid core pulse
 *     "alternate" — Smoothly alternates between Closed Solid Cube and Opened 4-Slice Cube
 *
 * Usage:
 *   <script src="./cube-mascot.js"></script>
 *   <cube-mascot id="myCube" color="red" state="alternate" style="width:180px;height:180px;"></cube-mascot>
 */
(function (global) {
  "use strict";

  const TAU = Math.PI * 2;
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const smoothstep = (e0, e1, x) => {
    const t = clamp((x - e0) / Math.max(e1 - e0, 1e-6), 0, 1);
    return t * t * (3 - 2 * t);
  };
  const mixRgb = (c1, c2, t) => {
    const k = clamp(t, 0, 1);
    return [
      c1[0] + (c2[0] - c1[0]) * k,
      c1[1] + (c2[1] - c1[1]) * k,
      c1[2] + (c2[2] - c1[2]) * k,
    ];
  };
  const rgbStr = (rgb, a = 1) => {
    const r = clamp(Math.round(rgb[0]), 0, 255);
    const g = clamp(Math.round(rgb[1]), 0, 255);
    const b = clamp(Math.round(rgb[2]), 0, 255);
    return a >= 0.999 ? `rgb(${r},${g},${b})` : `rgba(${r},${g},${b},${clamp(a, 0, 1).toFixed(3)})`;
  };

  const MATTE_BLACK = {
    outerBase: [38, 39, 46],
    outerShade: [11, 11, 15],
    outerRim: [72, 74, 86],
    outerEdgeRgb: [74, 76, 88],
  };

  const LIGHT_COLORS = {
    red: {
      innerCenterHot: [255, 238, 235],
      innerMidBright: [255, 38, 68],
      innerMidDeep: [185, 16, 36],
      innerBorderDark: [18, 4, 7],
      innerBorderEdgeRgb: [255, 75, 95],
      coreBloomInnerRgb: [255, 90, 100],
      coreBloomMidRgb: [220, 38, 38],
      coreBloomOuterRgb: [127, 29, 29],
    },
    white: {
      innerCenterHot: [255, 255, 255],
      innerMidBright: [236, 240, 248],
      innerMidDeep: [148, 156, 176],
      innerBorderDark: [18, 19, 24],
      innerBorderEdgeRgb: [240, 244, 255],
      coreBloomInnerRgb: [255, 255, 255],
      coreBloomMidRgb: [200, 210, 230],
      coreBloomOuterRgb: [120, 130, 150],
    },
    purple: {
      innerCenterHot: [255, 242, 255],
      innerMidBright: [217, 70, 239],
      innerMidDeep: [126, 34, 206],
      innerBorderDark: [19, 7, 34],
      innerBorderEdgeRgb: [232, 121, 249],
      coreBloomInnerRgb: [245, 208, 254],
      coreBloomMidRgb: [168, 85, 247],
      coreBloomOuterRgb: [88, 28, 135],
    },
    azure: {
      innerCenterHot: [240, 252, 255],
      innerMidBright: [56, 189, 248],
      innerMidDeep: [2, 132, 199],
      innerBorderDark: [4, 17, 32],
      innerBorderEdgeRgb: [103, 232, 249],
      coreBloomInnerRgb: [186, 230, 253],
      coreBloomMidRgb: [14, 165, 233],
      coreBloomOuterRgb: [3, 105, 161],
    },
  };

  const LIGHT_KEYS = [
    "innerCenterHot",
    "innerMidBright",
    "innerMidDeep",
    "innerBorderDark",
    "innerBorderEdgeRgb",
    "coreBloomInnerRgb",
    "coreBloomMidRgb",
    "coreBloomOuterRgb",
  ];

  function cloneLightRgb(src) {
    const out = {};
    for (const k of LIGHT_KEYS) out[k] = src[k].slice();
    return out;
  }

  function parseHexRgb(hex) {
    const h = String(hex || "").trim().replace(/^#/, "");
    if (h.length === 3) {
      return [parseInt(h[0] + h[0], 16), parseInt(h[1] + h[1], 16), parseInt(h[2] + h[2], 16)];
    }
    if (h.length === 6) {
      const n = parseInt(h, 16);
      if (Number.isFinite(n)) return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
    }
    return null;
  }

  function buildLightFromPalette(pal) {
    if (!Array.isArray(pal) || pal.length < 3) return null;
    const c0 = parseHexRgb(pal[0]) || [250, 250, 250];
    const c1 = parseHexRgb(pal[1]) || c0;
    const c2 = parseHexRgb(pal[2]) || c1;
    const c3 = parseHexRgb(pal[3]) || c2;
    const c4 = parseHexRgb(pal[4]) || c3;
    return {
      innerCenterHot: mixRgb(c0, [255, 255, 255], 0.45),
      innerMidBright: c2,
      innerMidDeep: c4,
      innerBorderDark: [Math.round(c4[0] * 0.09), Math.round(c4[1] * 0.09), Math.round(c4[2] * 0.09)],
      innerBorderEdgeRgb: c1,
      coreBloomInnerRgb: c0,
      coreBloomMidRgb: c3,
      coreBloomOuterRgb: c4,
    };
  }

  function resolveColorKey(key) {
    if (!key) return "red";
    const k = String(key).toLowerCase();
    if (k.includes("white") || k.includes("withe")) return "white";
    if (k.includes("purple") || k.includes("violet")) return "purple";
    if (k.includes("azure") || k.includes("blue") || k.includes("cyan")) return "azure";
    return "red";
  }

  function resolveStateKey(state) {
    if (!state) return "alternate";
    const s = String(state).toLowerCase();
    if (s === "closed" || s === "close") return "closed";
    if (s === "opened" || s === "open" || s === "split") return "opened";
    if (s === "loading" || s === "load" || s === "fast") return "loading";
    return "alternate";
  }

  function createRotationMatrix(rx, ry, rz) {
    const cx = Math.cos(rx), sx = Math.sin(rx);
    const cy = Math.cos(ry), sy = Math.sin(ry);
    const cz = Math.cos(rz), sz = Math.sin(rz);
    return [
      cy * cz, -cy * sz, sy,
      sx * sy * cz + cx * sz, -sx * sy * sz + cx * cz, -sx * cy,
      -cx * sy * cz + sx * sz, cx * sy * sz + sx * cz, cx * cy,
    ];
  }

  function applyMat3(m, v) {
    return {
      x: m[0] * v.x + m[1] * v.y + m[2] * v.z,
      y: m[3] * v.x + m[4] * v.y + m[5] * v.z,
      z: m[6] * v.x + m[7] * v.y + m[8] * v.z,
    };
  }

  function dot3(a, b) {
    return a.x * b.x + a.y * b.y + a.z * b.z;
  }

  const CUBE_FACES = [
    { idx: [0, 1, 2, 3], n: { x: 0, y: 0, z: 1 }, id: "front" },
    { idx: [5, 4, 7, 6], n: { x: 0, y: 0, z: -1 }, id: "back" },
    { idx: [1, 5, 6, 2], n: { x: 1, y: 0, z: 0 }, id: "right" },
    { idx: [4, 0, 3, 7], n: { x: -1, y: 0, z: 0 }, id: "left" },
    { idx: [3, 2, 6, 7], n: { x: 0, y: 1, z: 0 }, id: "top" },
    { idx: [4, 5, 1, 0], n: { x: 0, y: -1, z: 0 }, id: "bottom" },
  ];

  function makeBoxVertices(x0, x1, y0, y1, z0, z1) {
    return [
      { x: x0, y: y0, z: z1 }, { x: x1, y: y0, z: z1 },
      { x: x1, y: y1, z: z1 }, { x: x0, y: y1, z: z1 },
      { x: x0, y: y0, z: z0 }, { x: x1, y: y0, z: z0 },
      { x: x1, y: y1, z: z0 }, { x: x0, y: y1, z: z0 },
    ];
  }

  // Fixed isometric rotation matrix (never changes with the mouse)
  const FIXED_ROT_MAT = createRotationMatrix(0.37, -0.53, 0);

  class CubeMascotInstance {
    constructor(container, options = {}) {
      if (typeof container === "string") container = document.querySelector(container);
      if (!container) throw new Error("CubeMascot: valid container element required.");
      this.container = container;

      this.opts = {
        color: resolveColorKey(options.color || options.variant || "red"),
        customLight: buildLightFromPalette(options.palette),
        state: resolveStateKey(options.state || options.mood || "alternate"),
        glowMode: options.glowMode || "pulse",
        cameraZ: options.cameraZ ?? 5.6,
      };
      this._lightRgb = cloneLightRgb(this._getTargetLight());

      this.canvas = document.createElement("canvas");
      Object.assign(this.canvas.style, {
        display: "block", width: "100%", height: "100%", cursor: "pointer", userSelect: "none",
      });
      this.container.appendChild(this.canvas);
      this.ctx = this.canvas.getContext("2d");

      this.time = 0;
      this.lastFrameTime = performance.now();
      this.bobOffset = 0;

      this.openFactor = this.opts.state === "closed" ? 0.0 : 1.0;
      this.openVelocity = 0;
      this.glowIntensity = 0.85;
      this.seamVisibility = this.openFactor;

      const initSep = this.openFactor * 0.064;
      this.slabSprings = [
        { sep: initSep, vSep: 0, y: 0, vY: 0, z: 0, vZ: 0 },
        { sep: initSep, vSep: 0, y: 0, vY: 0, z: 0, vZ: 0 },
        { sep: initSep, vSep: 0, y: 0, vY: 0, z: 0, vZ: 0 },
        { sep: initSep, vSep: 0, y: 0, vY: 0, z: 0, vZ: 0 },
      ];

      this.resizeObserver = new ResizeObserver(() => this._resize());
      this.resizeObserver.observe(this.container);
      this._resize();

      this.manualPaused = false;
      this.isVisible = true;
      this.rafId = requestAnimationFrame((ts) => this._loop(ts));
    }

    _getTargetLight() {
      return this.opts.customLight || LIGHT_COLORS[this.opts.color] || LIGHT_COLORS.red;
    }

    _resize() {
      const w = Math.max(this.container.clientWidth, 1);
      const h = Math.max(this.container.clientHeight, 1);
      const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
      this.width = w; this.height = h; this.dpr = dpr;
      this.canvas.width = Math.round(w * dpr);
      this.canvas.height = Math.round(h * dpr);
    }

    setColor(color) {
      this.opts.color = resolveColorKey(color);
      this.opts.customLight = null;
    }
    setPalette(pal, colorKey) {
      if (colorKey) this.opts.color = resolveColorKey(colorKey);
      this.opts.customLight = buildLightFromPalette(pal);
    }
    setVariant(variant) { this.setColor(variant); }
    setState(state) { this.opts.state = resolveStateKey(state); }
    setMood(mood) { this.setState(mood); }
    setGlowMode(mode) { if (mode === "pulse" || mode === "steady") this.opts.glowMode = mode; }
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
      this.resizeObserver?.disconnect();
      this.canvas.remove();
    }

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
      const L = (this.time += dt);
      const state = this.opts.state;
      const isLoading = state === "loading";

      // 1. Vertical Floating Bobbing
      const bobRate = isLoading ? 0.45 : 0.28;
      const bobAmp = isLoading ? 0.028 : 0.024;
      this.bobOffset = Math.sin(L * bobRate * TAU) * bobAmp;

      // 2. Open / Close Target for the 4 States
      let targetOpen = 1.0;
      if (state === "closed") {
        targetOpen = 0.0;
      } else if (state === "opened") {
        targetOpen = 1.0;
      } else if (isLoading) {
        targetOpen = 1.06;
      } else {
        // "alternate": stays open for 7s, closes for ~2.4s, then reopens
        const tCycle = L % 11.0;
        if (tCycle < 7.0) targetOpen = 1.0;
        else if (tCycle < 7.8) targetOpen = 1.0 - smoothstep(7.0, 7.8, tCycle);
        else if (tCycle < 10.2) targetOpen = 0.0;
        else targetOpen = smoothstep(10.2, 11.0, tCycle);
      }

      if (targetOpen === 0 && this.openFactor < 0.012 && Math.abs(this.openVelocity) < 0.04) {
        this.openFactor = 0;
        this.openVelocity = 0;
      } else {
        this.openVelocity += (targetOpen - this.openFactor) * (isLoading ? 32 : 28) * dt;
        this.openVelocity *= Math.exp(-6.5 * dt);
        this.openFactor = Math.max(0, this.openFactor + this.openVelocity * dt);
      }

      // 3. On/Off Breathing Core Glow
      if (this.opts.glowMode === "steady") {
        this.glowIntensity = 0.95;
      } else {
        const glowFreq = isLoading ? 3.8 : 2.3;
        const shapedPulse = Math.pow(0.5 + 0.5 * Math.sin(L * glowFreq), 1.45);
        const minGlow = isLoading ? 0.2 : 0.12;
        this.glowIntensity = clamp(minGlow + shapedPulse * (1 - minGlow), minGlow, 1.0);
      }

      // 4. Individual 4-Slab Springs
      const open = this.openFactor;
      const baseGap = 0.064 * open;
      const waveSpeedY = isLoading ? 3.6 : 2.1;
      const waveAmpY = isLoading ? 0.056 : 0.042;
      const waveSpeedZ = isLoading ? 3.0 : 1.75;
      const waveAmpZ = isLoading ? 0.032 : 0.024;
      const kSpring = isLoading ? 54 : 46;
      const damp = isLoading ? 7.8 : 7.6;

      for (let i = 0; i < 4; i++) {
        const alt = i % 2 === 0 ? 1 : -1;
        const waveSep = Math.sin(L * (isLoading ? 3.4 : 2.0) + i * 0.9) * (isLoading ? 0.013 : 0.01) * open;
        const targetSep = open < 0.01 ? 0 : Math.max(0, baseGap + waveSep);
        const targetY =
          open < 0.01
            ? 0
            : (Math.sin(L * waveSpeedY + i * 1.15) * waveAmpY +
                alt * (isLoading ? 0.026 : 0.02) * Math.sin(L * (isLoading ? 2.0 : 1.1))) *
              Math.pow(open, 1.2);
        const targetZ =
          open < 0.01 ? 0 : Math.cos(L * waveSpeedZ + i * 1.3) * waveAmpZ * Math.pow(open, 1.2);

        const s = this.slabSprings[i];
        s.vSep += (targetSep - s.sep) * kSpring * dt; s.vSep *= Math.exp(-damp * dt); s.sep = Math.max(0, s.sep + s.vSep * dt);
        if (targetSep === 0 && s.sep < 0.0008) { s.sep = 0; s.vSep = 0; }
        s.vY += (targetY - s.y) * kSpring * dt; s.vY *= Math.exp(-damp * dt); s.y += s.vY * dt;
        if (targetY === 0 && Math.abs(s.y) < 0.0008) { s.y = 0; s.vY = 0; }
        s.vZ += (targetZ - s.z) * kSpring * dt; s.vZ *= Math.exp(-damp * dt); s.z += s.vZ * dt;
        if (targetZ === 0 && Math.abs(s.z) < 0.0008) { s.z = 0; s.vZ = 0; }
      }

      const maxSlabDisp = Math.max(
        ...this.slabSprings.map((s) => s.sep + Math.abs(s.y) * 0.5 + Math.abs(s.z) * 0.5)
      );
      this.seamVisibility = smoothstep(0.0012, 0.018, maxSlabDisp);

      const targetPal = this._getTargetLight();
      if (!this._lightRgb) {
        this._lightRgb = cloneLightRgb(targetPal);
      } else {
        const blend = 1 - Math.exp(-10 * dt);
        for (const k of LIGHT_KEYS) {
          this._lightRgb[k] = mixRgb(this._lightRgb[k], targetPal[k], blend);
        }
      }
    }

    _project(x, y, z) {
      const camZ = this.opts.cameraZ;
      const persp = camZ / Math.max(camZ - z, 0.5);
      return {
        x: this._cx + x * persp * this._scale,
        y: this._cy - (y + this.bobOffset) * persp * this._scale,
        s: persp,
        z,
      };
    }

    _shadeMatteBlackFace(normal) {
      const dotKey = clamp(dot3(normal, { x: -0.42, y: 0.72, z: 0.55 }) * 0.5 + 0.5, 0, 1);
      const diffuse = smoothstep(0.05, 0.98, dotKey);
      let rgb = mixRgb(MATTE_BLACK.outerShade, MATTE_BLACK.outerBase, diffuse);
      const topGrazing = Math.pow(Math.max(0, normal.y * 0.65 + normal.z * 0.5), 3) * 0.15;
      return mixRgb(rgb, MATTE_BLACK.outerRim, topGrazing);
    }

    _render() {
      const ctx = this.ctx, w = this.width, h = this.height, dpr = this.dpr;
      const curPal = this._lightRgb || this.opts.customLight || LIGHT_COLORS[this.opts.color] || LIGHT_COLORS.red;
      const glow = this.glowIntensity;
      const seamVis = this.seamVisibility;

      ctx.save();
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, w, h);

      this._cx = w / 2;
      this._cy = h / 2;
      this._scale = Math.min(w, h) * 0.38;

      if (glow * seamVis > 0.02) {
        const p0 = this._project(0, 0, 0);
        const R = 0.92 * this._scale * p0.s;
        const grad = ctx.createRadialGradient(p0.x, p0.y, R * 0.04, p0.x, p0.y, R);
        grad.addColorStop(0, rgbStr(curPal.coreBloomInnerRgb || curPal.innerCenterHot, 0.91));
        grad.addColorStop(0.4, rgbStr(curPal.coreBloomMidRgb || curPal.innerMidBright, 0.53));
        grad.addColorStop(1, rgbStr(curPal.coreBloomOuterRgb || curPal.innerMidDeep, 0.0));
        ctx.save();
        ctx.globalAlpha = clamp(glow * seamVis * 0.55, 0, 0.85);
        ctx.fillStyle = grad;
        ctx.beginPath(); ctx.arc(p0.x, p0.y, R, 0, TAU); ctx.fill();
        ctx.restore();
      }

      const S = 0.53;
      const slabW = (S * 2) / 4;
      const rotMat = FIXED_ROT_MAT;
      const Nx = applyMat3(rotMat, { x: 1, y: 0, z: 0 });
      const eyeLocalX = dot3({ x: 0, y: 0, z: this.opts.cameraZ }, Nx);

      const slabs = [0, 1, 2, 3].map((i) => {
        const x0 = -S + i * slabW, x1 = x0 + slabW, k = i - 1.5;
        const s = this.slabSprings[i];
        const offset = { x: k * s.sep, y: s.y, z: s.z };
        const actualX0 = x0 + offset.x, actualX1 = x1 + offset.x;
        const actualCenterX = (actualX0 + actualX1) * 0.5;
        const innerFaces = [];
        if (i < 3) innerFaces.push("right");
        if (i > 0) innerFaces.push("left");

        const rotVerts = makeBoxVertices(x0, x1, -S, S, -S, S).map((v) =>
          applyMat3(rotMat, { x: v.x + offset.x, y: v.y + offset.y, z: v.z + offset.z })
        );
        const rightLightOrigin3D = applyMat3(rotMat, { x: actualX1, y: 0, z: 0 });
        const leftLightOrigin3D = applyMat3(rotMat, { x: actualX0, y: 0, z: 0 });
        const bspDist = Math.abs(eyeLocalX - actualCenterX);

        return { i, actualX0, actualX1, bspDist, innerFaces, rotVerts, rightLightOrigin3D, leftLightOrigin3D };
      }).sort((a, b) => b.bspDist - a.bspDist);

      for (const slab of slabs) {
        const visibleFaces = [];
        for (const face of CUBE_FACES) {
          const isInnerCut = slab.innerFaces.includes(face.id);
          if (isInnerCut && seamVis < 0.01) continue;
          if (isInnerCut) {
            if (face.id === "right" && eyeLocalX <= slab.actualX1 + 0.002) continue;
            if (face.id === "left" && eyeLocalX >= slab.actualX0 - 0.002) continue;
          }
          const quad3D = face.idx.map((idx) => slab.rotVerts[idx]);
          const quad2D = quad3D.map((v) => this._project(v.x, v.y, v.z));
          const area =
            (quad2D[1].x - quad2D[0].x) * (quad2D[2].y - quad2D[0].y) -
            (quad2D[1].y - quad2D[0].y) * (quad2D[2].x - quad2D[0].x);
          if (area >= 0) continue;
          const zAvg = (quad3D[0].z + quad3D[1].z + quad3D[2].z + quad3D[3].z) / 4;
          visibleFaces.push({ face, isInnerCut, quad2D, zAvg });
        }
        visibleFaces.sort((a, b) => (b.isInnerCut ? 1 : 0) - (a.isInnerCut ? 1 : 0) || a.zAvg - b.zAvg);

        for (const vf of visibleFaces) {
          const rotN = applyMat3(rotMat, vf.face.n);
          ctx.save();
          ctx.beginPath();
          vf.quad2D.forEach((p, idx) => (idx === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y)));
          ctx.closePath();

          if (vf.isInnerCut) {
            const light3D = vf.face.id === "right" ? slab.rightLightOrigin3D : slab.leftLightOrigin3D;
            const pLight = this._project(light3D.x, light3D.y, light3D.z);
            let maxDist = 10;
            for (const c of vf.quad2D) maxDist = Math.max(maxDist, Math.hypot(c.x - pLight.x, c.y - pLight.y));

            const effGlow = glow * seamVis;
            const radGrad = ctx.createRadialGradient(pLight.x, pLight.y, maxDist * 0.02, pLight.x, pLight.y, maxDist * 1.02);
            radGrad.addColorStop(0.0, rgbStr(mixRgb(curPal.innerBorderDark, curPal.innerCenterHot, effGlow)));
            radGrad.addColorStop(0.26, rgbStr(mixRgb(curPal.innerBorderDark, curPal.innerMidBright, effGlow)));
            radGrad.addColorStop(0.62, rgbStr(mixRgb(curPal.innerBorderDark, curPal.innerMidDeep, effGlow * 0.92)));
            radGrad.addColorStop(1.0, rgbStr(curPal.innerBorderDark));

            ctx.globalAlpha = seamVis;
            ctx.fillStyle = radGrad; ctx.fill();
            ctx.strokeStyle = rgbStr(curPal.innerBorderEdgeRgb, (0.25 + glow * 0.75) * seamVis);
            ctx.lineWidth = 1.0; ctx.lineJoin = "round"; ctx.stroke();
          } else {
            const faceRgb = this._shadeMatteBlackFace(rotN);
            const strokeRgb = mixRgb(faceRgb, MATTE_BLACK.outerEdgeRgb, seamVis * 0.42);
            ctx.fillStyle = rgbStr(faceRgb); ctx.fill();
            ctx.strokeStyle = rgbStr(strokeRgb);
            ctx.lineWidth = 1.4; ctx.lineJoin = "round"; ctx.stroke();
          }
          ctx.restore();
        }
      }
      ctx.restore();
    }
  }

  if (typeof customElements !== "undefined" && !customElements.get("cube-mascot")) {
    class CubeMascotElement extends HTMLElement {
      static get observedAttributes() { return ["color", "variant", "state", "mood", "glow-mode"]; }
      connectedCallback() {
        if (!this.style.display) this.style.display = "inline-block";
        if (!this.style.width && !this.clientWidth) this.style.width = "180px";
        if (!this.style.height && !this.clientHeight) this.style.height = "180px";
        this.instance = new CubeMascotInstance(this, {
          color: this.getAttribute("color") || this.getAttribute("variant") || "red",
          state: this.getAttribute("state") || this.getAttribute("mood") || "alternate",
          glowMode: this.getAttribute("glow-mode") || "pulse",
        });
      }
      disconnectedCallback() { this.instance?.destroy(); this.instance = null; }
      attributeChangedCallback(name, oldVal, newVal) {
        if (!this.instance || oldVal === newVal) return;
        if (name === "color" || name === "variant") this.instance.setColor(newVal || "red");
        else if (name === "state" || name === "mood") this.instance.setState(newVal || "alternate");
        else if (name === "glow-mode") this.instance.setGlowMode(newVal || "pulse");
      }
    }
    customElements.define("cube-mascot", CubeMascotElement);
  }

  global.CubeMascot = {
    mount: (container, options) => new CubeMascotInstance(container, options),
    Instance: CubeMascotInstance,
    COLORS: ["red", "white", "purple", "azure"],
    STATES: ["closed", "opened", "loading", "alternate"],
  };
})(typeof window !== "undefined" ? window : globalThis);
