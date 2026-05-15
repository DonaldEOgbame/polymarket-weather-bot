// Stippled orthographic globe — vanilla canvas, d3-geo for projection.
// Continents rendered as ~6k dots over land. City markers state-coloured.
// Drag to rotate. Auto-rotates after idle.
//
// Usage:
//   const g = new StormGlobe(container, { cities, cityActivity, onCityClick, onCityHover });
//   g.start();
(() => {
  const PI = Math.PI;
  const DEG = PI / 180;

  // ---- minimal projection (orthographic) — avoids loading d3 ----
  function project(lat, lon, rotLambda, rotPhi, radius, cx, cy) {
    // rotate lon
    const l = (lon - rotLambda) * DEG;
    const p = lat * DEG;
    const f = rotPhi * DEG;
    // standard orthographic with two-axis rotation (yaw=lambda, pitch=phi)
    const cosP = Math.cos(p);
    const x = cosP * Math.sin(l);
    const y = Math.sin(p) * Math.cos(f) - cosP * Math.cos(l) * Math.sin(f);
    const z = Math.sin(p) * Math.sin(f) + cosP * Math.cos(l) * Math.cos(f);
    if (z < 0) return null; // back of sphere
    return { x: cx + x * radius, y: cy - y * radius, z };
  }

  function projectAlways(lat, lon, rotLambda, rotPhi, radius, cx, cy) {
    const l = (lon - rotLambda) * DEG;
    const p = lat * DEG;
    const f = rotPhi * DEG;
    const cosP = Math.cos(p);
    const x = cosP * Math.sin(l);
    const y = Math.sin(p) * Math.cos(f) - cosP * Math.cos(l) * Math.sin(f);
    const z = Math.sin(p) * Math.sin(f) + cosP * Math.cos(l) * Math.cos(f);
    return { x: cx + x * radius, y: cy - y * radius, z };
  }

  // ---- hand-traced continent polygons (lon, lat) — fallback when CDN blocked.
  // Coarse but recognizable on an orthographic globe. Outer rings only.
  const CONTINENT_POLYS = [
    // North America (incl. Alaska, Mex, Greenland-less)
    [[-168,66],[-156,71],[-141,70],[-128,70],[-104,69],[-95,73],[-84,73],[-75,67],[-62,60],[-55,54],[-58,48],[-66,45],[-71,42],[-75,38],[-77,34],[-80,28],[-82,25],[-87,29],[-91,29],[-95,27],[-98,26],[-101,25],[-106,23],[-110,22],[-115,30],[-120,33],[-123,38],[-124,44],[-124,48],[-130,54],[-140,59],[-152,60],[-160,57],[-166,55],[-168,62]],
    // Greenland
    [[-72,60],[-58,63],[-44,60],[-22,69],[-22,82],[-58,83],[-72,80],[-72,70]],
    // Central America narrow
    [[-92,17],[-83,15],[-78,9],[-77,8],[-83,8],[-87,12],[-94,16]],
    // South America
    [[-81,12],[-72,11],[-62,11],[-53,5],[-50,1],[-48,-3],[-42,-7],[-35,-7],[-37,-12],[-39,-18],[-43,-23],[-48,-26],[-53,-34],[-58,-38],[-65,-42],[-70,-50],[-73,-54],[-75,-50],[-72,-44],[-71,-36],[-72,-30],[-71,-22],[-72,-14],[-78,-8],[-80,-3],[-79,1],[-77,5],[-78,9]],
    // Europe (mainland + UK shape simplified)
    [[-10,36],[-9,42],[-9,44],[-2,48],[2,51],[6,53],[8,57],[10,58],[14,55],[12,54],[19,55],[22,57],[24,60],[28,65],[30,69],[42,68],[60,68],[60,55],[55,48],[48,46],[42,46],[37,46],[32,45],[28,43],[24,42],[20,41],[16,40],[13,38],[11,42],[7,44],[3,42],[-1,38],[-7,37]],
    // UK / Ireland (small island blob)
    [[-10,52],[-6,55],[-4,58],[0,58],[1,53],[-2,51],[-6,50],[-10,53]],
    // Scandinavia (above Europe — already partly covered)
    [[5,58],[10,59],[14,61],[18,63],[20,66],[24,68],[27,70],[30,71],[20,69],[14,67],[8,63],[5,62]],
    // Africa
    [[-17,21],[-17,15],[-12,7],[-6,5],[5,4],[9,4],[12,2],[15,-2],[14,-8],[12,-15],[14,-22],[18,-32],[24,-34],[28,-34],[32,-29],[35,-25],[40,-20],[42,-15],[44,-10],[46,-2],[48,4],[51,11],[44,11],[42,15],[38,17],[34,22],[31,29],[30,31],[25,32],[20,32],[15,32],[10,33],[5,35],[0,33],[-7,33],[-10,29],[-15,25]],
    // Madagascar
    [[44,-25],[48,-23],[50,-15],[48,-13],[44,-19]],
    // Middle East / Arabia
    [[34,30],[39,29],[44,30],[48,30],[53,26],[56,22],[55,17],[52,14],[48,13],[44,13],[42,18],[39,22],[36,28]],
    // Asia mainland
    [[40,38],[45,40],[52,42],[60,42],[65,40],[70,38],[75,36],[80,34],[88,30],[95,30],[100,25],[104,22],[108,20],[112,21],[118,24],[122,30],[126,40],[131,45],[135,52],[140,58],[145,62],[150,65],[160,68],[170,68],[178,68],[178,75],[160,73],[140,73],[120,73],[100,75],[80,72],[68,72],[55,68],[45,67],[40,62],[35,55],[36,48],[38,42]],
    // India subcontinent
    [[68,24],[72,22],[75,18],[78,12],[80,8],[80,16],[83,18],[88,22],[91,25],[88,28],[80,30],[73,32],[68,30]],
    // SE Asia (Indochina)
    [[95,22],[100,22],[105,22],[109,21],[107,15],[105,11],[103,7],[100,4],[97,8],[94,16]],
    // Indonesia islands (cluster)
    [[95,5],[102,3],[108,-1],[115,-3],[120,-5],[124,-5],[128,-3],[132,-2],[136,-3],[140,-3],[140,-7],[130,-9],[120,-9],[110,-8],[100,-2]],
    // Philippines blob
    [[119,5],[122,7],[125,9],[126,14],[124,18],[121,18],[119,13],[118,8]],
    // Japan (chain)
    [[130,31],[133,34],[136,35],[138,35],[141,38],[142,41],[145,44],[143,43],[140,40],[138,37],[135,34],[131,32]],
    // Australia
    [[114,-22],[118,-20],[122,-17],[128,-15],[133,-12],[138,-12],[142,-11],[145,-15],[151,-25],[153,-28],[151,-32],[148,-37],[143,-38],[140,-37],[136,-35],[131,-32],[126,-32],[121,-33],[115,-33],[114,-26]],
    // New Zealand
    [[166,-46],[171,-44],[174,-42],[176,-38],[178,-37],[175,-37],[170,-43]],
    // Iceland
    [[-24,63],[-13,64],[-13,67],[-23,66]],
  ];

  // ---- main class ----
  class StormGlobe {
    constructor(container, opts = {}) {
      this.el = container;
      this.cities = opts.cities || [];
      this.activity = opts.cityActivity || {};
      this.onCityClick = opts.onCityClick || (() => {});
      this.onCityHover = opts.onCityHover || (() => {});
      this.rotLambda = -20;
      this.rotPhi = 18;
      this.dragging = false;
      this.lastInteraction = Date.now();
      this.land = null; // GeoJSON land features when loaded
      this.landSamples = null; // pre-computed [lon,lat] points over land
      this.hovered = null;
      this.t = 0;

      this._build();
      // Synchronous fallback so the globe never shows empty.
      this.landSamples = this._sampleLandBoxes();
      // Then upgrade to detailed topojson if reachable.
      this._loadLand();
    }

    _build() {
      const wrap = this.el;
      wrap.style.position = wrap.style.position || 'relative';
      const canvas = document.createElement('canvas');
      canvas.style.cssText = 'display:block;width:100%;height:100%;cursor:grab;';
      wrap.appendChild(canvas);
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this._resize();
      window.addEventListener('resize', () => this._resize());

      // interactions
      let lx = 0, ly = 0;
      canvas.addEventListener('pointerdown', e => {
        this.dragging = true;
        canvas.setPointerCapture(e.pointerId);
        canvas.style.cursor = 'grabbing';
        lx = e.clientX; ly = e.clientY;
        this.lastInteraction = Date.now();
      });
      canvas.addEventListener('pointermove', e => {
        const rect = canvas.getBoundingClientRect();
        this.mouse = { x: e.clientX - rect.left, y: e.clientY - rect.top };
        if (this.dragging) {
          const dx = e.clientX - lx;
          const dy = e.clientY - ly;
          this.rotLambda -= dx * 0.45;
          this.rotPhi = Math.max(-85, Math.min(85, this.rotPhi + dy * 0.35));
          lx = e.clientX; ly = e.clientY;
          this.lastInteraction = Date.now();
        } else {
          this._updateHover();
        }
      });
      canvas.addEventListener('pointerup', e => {
        this.dragging = false;
        canvas.style.cursor = 'grab';
        // detect click on city if not dragged
        if (this.hovered) {
          this.onCityClick(this.hovered);
        }
      });
      canvas.addEventListener('pointerleave', () => {
        this.mouse = null;
        if (this.hovered) {
          this.hovered = null;
          this.onCityHover(null);
        }
      });
    }

    _resize() {
      const r = this.el.getBoundingClientRect();
      const DPR = Math.min(window.devicePixelRatio || 1, 2);
      this.W = r.width;
      this.H = r.height;
      this.canvas.width = this.W * DPR;
      this.canvas.height = this.H * DPR;
      this.ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
      // radius — leave room for labels
      this.R = Math.min(this.W, this.H) * 0.42;
      this.cx = this.W / 2;
      this.cy = this.H / 2;
    }

    async _loadLand() {
      // Try CDNs in order. If all fail, fall back to bounding-box sampling.
      const urls = [
        'https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/land-110m.json',
        'https://unpkg.com/world-atlas@2.0.2/land-110m.json',
      ];
      for (const url of urls) {
        try {
          const res = await fetch(url, { mode: 'cors' });
          if (!res.ok) throw new Error('http ' + res.status);
          const topo = await res.json();
          // tiny inline topojson feature converter
          this.landSamples = this._sampleLandTopo(topo);
          return;
        } catch (e) {
          console.warn('globe land fetch failed', url, e.message);
        }
      }
      // fallback — use bounding boxes
      this.landSamples = this._sampleLandBoxes();
    }

    _sampleLandTopo(topo) {
      // convert TopoJSON land-110m to a polygon list, then test grid points
      const obj = topo.objects.land;
      const arcs = topo.arcs;
      const transform = topo.transform;
      const tx = transform ? transform.translate : [0, 0];
      const sc = transform ? transform.scale : [1, 1];

      function decodeArc(idx) {
        const flip = idx < 0;
        const arc = arcs[flip ? ~idx : idx];
        let x = 0, y = 0;
        const out = [];
        for (let i = 0; i < arc.length; i++) {
          x += arc[i][0];
          y += arc[i][1];
          out.push([x * sc[0] + tx[0], y * sc[1] + tx[1]]);
        }
        return flip ? out.reverse() : out;
      }
      function ringFromArcs(arcIdxs) {
        const ring = [];
        arcIdxs.forEach((idx, i) => {
          const pts = decodeArc(idx);
          if (i > 0) pts.shift();
          ring.push(...pts);
        });
        return ring;
      }

      const polygons = []; // each: [outerRing, hole1, hole2, ...]
      function addGeom(g) {
        if (!g) return;
        if (g.type === 'Polygon') {
          polygons.push(g.arcs.map(ringFromArcs));
        } else if (g.type === 'MultiPolygon') {
          g.arcs.forEach(poly => polygons.push(poly.map(ringFromArcs)));
        } else if (g.type === 'GeometryCollection') {
          g.geometries.forEach(addGeom);
        }
      }
      addGeom(obj);

      // point-in-ring helper
      function pip(point, ring) {
        let inside = false;
        const [x, y] = point;
        for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
          const xi = ring[i][0], yi = ring[i][1];
          const xj = ring[j][0], yj = ring[j][1];
          const intersect = ((yi > y) !== (yj > y)) &&
            (x < (xj - xi) * (y - yi) / ((yj - yi) || 1e-12) + xi);
          if (intersect) inside = !inside;
        }
        return inside;
      }
      function inLand(lon, lat) {
        for (const poly of polygons) {
          if (pip([lon, lat], poly[0])) {
            // check holes
            let hole = false;
            for (let i = 1; i < poly.length; i++) {
              if (pip([lon, lat], poly[i])) { hole = true; break; }
            }
            if (!hole) return true;
          }
        }
        return false;
      }

      // grid sample — denser near equator, sparser at poles
      const samples = [];
      const step = 1.6; // degrees
      for (let lat = -85; lat <= 85; lat += step) {
        // adjust lon step by cos(lat) so density is roughly even on sphere
        const lonStep = step / Math.max(0.18, Math.cos(lat * DEG));
        for (let lon = -180; lon < 180; lon += lonStep) {
          if (inLand(lon, lat)) samples.push([lon, lat]);
        }
      }
      return samples;
    }

    _sampleLandBoxes() {
      // Point-in-polygon test against hand-traced continents.
      function pip(lon, lat, ring) {
        let inside = false;
        for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
          const xi = ring[i][0], yi = ring[i][1];
          const xj = ring[j][0], yj = ring[j][1];
          if (((yi > lat) !== (yj > lat)) &&
              (lon < (xj - xi) * (lat - yi) / ((yj - yi) || 1e-12) + xi)) {
            inside = !inside;
          }
        }
        return inside;
      }
      const samples = [];
      const step = 1.6;
      for (let lat = -85; lat <= 85; lat += step) {
        const lonStep = step / Math.max(0.18, Math.cos(lat * DEG));
        for (let lon = -180; lon < 180; lon += lonStep) {
          for (const poly of CONTINENT_POLYS) {
            if (pip(lon, lat, poly)) { samples.push([lon, lat]); break; }
          }
        }
      }
      return samples;
    }

    _updateHover() {
      if (!this.mouse) { return; }
      let best = null, bestD = 18 * 18;
      for (const city of this.cities) {
        const p = project(city.lat, city.lon, this.rotLambda, this.rotPhi, this.R, this.cx, this.cy);
        if (!p) continue;
        const dx = p.x - this.mouse.x;
        const dy = p.y - this.mouse.y;
        const d = dx * dx + dy * dy;
        if (d < bestD) { bestD = d; best = city; }
      }
      if (best !== this.hovered) {
        this.hovered = best;
        this.canvas.style.cursor = best ? 'pointer' : 'grab';
        this.onCityHover(best, this.mouse);
      } else if (best && this.mouse) {
        this.onCityHover(best, this.mouse);
      }
    }

    _idleRotate() {
      if (this.dragging) return;
      const idleMs = Date.now() - this.lastInteraction;
      if (idleMs > 4000) {
        this.rotLambda -= 0.06;
      }
    }

    start() {
      const loop = () => {
        this._idleRotate();
        this.draw();
        this.t += 1;
        this.raf = requestAnimationFrame(loop);
      };
      loop();
    }

    stop() {
      if (this.raf) cancelAnimationFrame(this.raf);
    }

    draw() {
      const ctx = this.ctx;
      const { W, H, R, cx, cy } = this;
      ctx.clearRect(0, 0, W, H);

      // --- ocean disc (very dark gradient) ---
      const grad = ctx.createRadialGradient(cx - R * 0.3, cy - R * 0.3, R * 0.1, cx, cy, R);
      grad.addColorStop(0, '#10141a');
      grad.addColorStop(0.7, '#0a0c10');
      grad.addColorStop(1, '#07090c');
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, PI * 2);
      ctx.fillStyle = grad;
      ctx.fill();

      // outer limb glow
      ctx.beginPath();
      ctx.arc(cx, cy, R + 0.5, 0, PI * 2);
      ctx.strokeStyle = 'rgba(245,177,60,0.18)';
      ctx.lineWidth = 1;
      ctx.stroke();

      ctx.beginPath();
      ctx.arc(cx, cy, R + 8, 0, PI * 2);
      ctx.strokeStyle = 'rgba(245,177,60,0.05)';
      ctx.lineWidth = 6;
      ctx.stroke();

      // --- graticule (every 30°) ---
      ctx.strokeStyle = 'rgba(255,255,255,0.045)';
      ctx.lineWidth = 0.6;
      for (let lon = -180; lon < 180; lon += 30) {
        ctx.beginPath();
        let started = false;
        for (let lat = -85; lat <= 85; lat += 5) {
          const p = project(lat, lon, this.rotLambda, this.rotPhi, R, cx, cy);
          if (!p) { started = false; continue; }
          if (!started) { ctx.moveTo(p.x, p.y); started = true; }
          else ctx.lineTo(p.x, p.y);
        }
        ctx.stroke();
      }
      for (let lat = -60; lat <= 60; lat += 30) {
        ctx.beginPath();
        let started = false;
        for (let lon = -180; lon <= 180; lon += 3) {
          const p = project(lat, lon, this.rotLambda, this.rotPhi, R, cx, cy);
          if (!p) { started = false; continue; }
          if (!started) { ctx.moveTo(p.x, p.y); started = true; }
          else ctx.lineTo(p.x, p.y);
        }
        ctx.stroke();
      }

      // --- land stipple ---
      if (this.landSamples) {
        // Faint base
        ctx.fillStyle = 'rgba(220,215,200,0.75)';
        for (const [lon, lat] of this.landSamples) {
          const p = project(lat, lon, this.rotLambda, this.rotPhi, R, cx, cy);
          if (!p) continue;
          // size + opacity dim toward limb
          const fade = Math.pow(p.z, 0.6);
          if (fade < 0.15) continue;
          const sz = fade > 0.7 ? 1.2 : 1;
          ctx.globalAlpha = 0.32 + fade * 0.55;
          ctx.fillRect(p.x - sz/2, p.y - sz/2, sz, sz);
        }
        ctx.globalAlpha = 1;
      } else {
        // loading hint
        ctx.fillStyle = 'rgba(245,177,60,0.5)';
        ctx.font = '11px JetBrains Mono, monospace';
        ctx.textAlign = 'center';
        ctx.fillText('· loading terrain ·', cx, cy + R + 24);
      }

      // --- city markers ---
      for (const city of this.cities) {
        const p = project(city.lat, city.lon, this.rotLambda, this.rotPhi, R, cx, cy);
        if (!p) continue;
        const act = this.activity[city.key] || this.activity[city.name];
        const isActive = act && act.state === 'active';
        const isSignal = act && act.state === 'signal';
        const isScanned = act && act.state === 'scanned';
        const isHovered = this.hovered === city;

        if (isActive) {
          // pulsing ring
          const pulse = (Math.sin(this.t * 0.06) + 1) / 2;
          ctx.beginPath();
          ctx.arc(p.x, p.y, 7 + pulse * 4, 0, PI * 2);
          ctx.strokeStyle = `rgba(108,191,133,${0.5 - pulse * 0.35})`;
          ctx.lineWidth = 1.2;
          ctx.stroke();

          ctx.beginPath();
          ctx.arc(p.x, p.y, 3.2, 0, PI * 2);
          ctx.fillStyle = '#6cbf85';
          ctx.fill();
          ctx.strokeStyle = '#0a0c10';
          ctx.lineWidth = 1.4;
          ctx.stroke();
        } else if (isSignal) {
          ctx.beginPath();
          ctx.arc(p.x, p.y, 2.6, 0, PI * 2);
          ctx.fillStyle = '#f5b13c';
          ctx.fill();
          ctx.strokeStyle = '#0a0c10';
          ctx.lineWidth = 1;
          ctx.stroke();
        } else if (isScanned) {
          ctx.beginPath();
          ctx.arc(p.x, p.y, 1.8, 0, PI * 2);
          ctx.fillStyle = 'rgba(245,177,60,0.55)';
          ctx.fill();
        } else {
          // dim baseline dot
          ctx.beginPath();
          ctx.arc(p.x, p.y, 1.4, 0, PI * 2);
          ctx.fillStyle = 'rgba(233,230,223,0.5)';
          ctx.fill();
        }

        if (isHovered) {
          ctx.beginPath();
          ctx.arc(p.x, p.y, 9, 0, PI * 2);
          ctx.strokeStyle = 'rgba(245,177,60,0.7)';
          ctx.lineWidth = 1;
          ctx.stroke();

          // tick + label
          const lblY = p.y - 14;
          ctx.fillStyle = '#e9e6df';
          ctx.font = '600 11px Manrope, sans-serif';
          ctx.textAlign = 'center';
          ctx.fillText(city.name, p.x, lblY);
        }

        // label active cities always (small)
        if (isActive && !isHovered) {
          ctx.fillStyle = 'rgba(233,230,223,0.7)';
          ctx.font = '500 10px Manrope, sans-serif';
          ctx.textAlign = 'left';
          ctx.fillText(city.name, p.x + 7, p.y + 3);
        }
      }

      // --- compass / crosshair tick ---
      ctx.strokeStyle = 'rgba(245,177,60,0.18)';
      ctx.lineWidth = 0.6;
      ctx.beginPath();
      ctx.moveTo(cx - R - 14, cy); ctx.lineTo(cx - R - 4, cy);
      ctx.moveTo(cx + R + 4, cy);  ctx.lineTo(cx + R + 14, cy);
      ctx.moveTo(cx, cy - R - 14); ctx.lineTo(cx, cy - R - 4);
      ctx.moveTo(cx, cy + R + 4);  ctx.lineTo(cx, cy + R + 14);
      ctx.stroke();
    }
  }

  window.StormGlobe = StormGlobe;
})();
