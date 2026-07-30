// Stippled cyclone — self-starting, no framework lifecycle.
// Paints any <canvas data-cyclone> present in the DOM, re-sizing every frame.
// Shared by login.html; safe to include on pages that have no such canvas.
(() => {
  if (window.__cycloneRunning) return;
  window.__cycloneRunning = true;
  const hash = (x, y) => { const h = Math.sin(x * 12.9898 + y * 78.233) * 43758.5453; return h - Math.floor(h); };
  let t = 0;
  const frame = () => {
    requestAnimationFrame(frame);
    const c = document.querySelector('canvas[data-cyclone]');
    if (!c || !c.isConnected) return;
    const W = c.clientWidth, H = c.clientHeight;
    if (!W || !H) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const bw = Math.round(W * dpr), bh = Math.round(H * dpr);
    if (c.width !== bw || c.height !== bh) { c.width = bw; c.height = bh; }
    const g = c.getContext('2d');
    if (!g) return;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    const cx = W * 0.5, cy = H * 0.5, R = Math.min(W, H) * 0.46;
    for (let i = 0; i < 380; i++) {
      const a = hash(i, 11) * Math.PI * 2, r = R * (1.05 + hash(i, 41) * 0.6);
      const x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r * 0.92;
      if (x < 0 || x > W || y < 0 || y > H) continue;
      g.fillStyle = 'rgba(220,215,200,' + (0.06 + hash(i, 71) * 0.12).toFixed(3) + ')';
      g.fillRect(x, y, 1, 1);
    }
    const POINTS = 4200;
    for (let i = 0; i < POINTS; i++) {
      const u = i / POINTS, r = Math.pow(u, 0.55) * R;
      const ang = (i % 2) * Math.PI + u * 3.6 * Math.PI * 2 + t * 0.0008;
      const aw = 12 + (1 - u) * 26;
      const px = cx + Math.cos(ang) * r + Math.cos(ang + Math.PI / 2) * (hash(i, 3) - 0.5) * aw;
      const py = cy + Math.sin(ang) * r * 0.96 + Math.sin(ang + Math.PI / 2) * (hash(i, 7) - 0.5) * aw * 0.6;
      if (px < 0 || px > W || py < 0 || py > H) continue;
      if (u < 0.06 && hash(i, 13) > 0.05) continue;
      if (hash(i, 17) > 0.78) continue;
      const op = Math.min(0.95, (0.18 + (1 - u) * 0.7) * (0.85 + hash(i + Math.floor(t / 8), 23) * 0.3));
      g.fillStyle = 'rgba(' + (u < 0.18 ? '245,200,140' : '220,215,200') + ',' + op.toFixed(3) + ')';
      const sz = u < 0.04 ? 1.4 : u < 0.2 ? 1.1 : 1;
      g.fillRect(px, py, sz, sz);
    }
    g.beginPath(); g.arc(cx, cy, R * 0.04, 0, Math.PI * 2); g.fillStyle = '#06070a'; g.fill();
    g.strokeStyle = 'rgba(245,177,60,0.55)'; g.lineWidth = 0.6;
    g.beginPath(); g.arc(cx, cy, R * 0.045, 0, Math.PI * 2); g.stroke();
    g.strokeStyle = 'rgba(245,177,60,0.3)'; g.lineWidth = 0.5; g.beginPath();
    g.moveTo(cx - R * 0.08, cy); g.lineTo(cx - R * 0.05, cy);
    g.moveTo(cx + R * 0.05, cy); g.lineTo(cx + R * 0.08, cy);
    g.moveTo(cx, cy - R * 0.08); g.lineTo(cx, cy - R * 0.05);
    g.moveTo(cx, cy + R * 0.05); g.lineTo(cx, cy + R * 0.08);
    g.stroke();
    t += 16;
  };
  requestAnimationFrame(frame);
})();
