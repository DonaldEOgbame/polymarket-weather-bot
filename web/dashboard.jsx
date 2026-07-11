// stormedge — dashboard React app
// Fetches live data from /api/data and refreshes every 30s.

const { useState, useEffect, useRef } = React;

// ---------- helpers ----------
const fmtUSD = (n, signed = false) => {
  const sign = signed && n > 0 ? '+' : '';
  return sign + '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};
const fmtPct = (n, digits = 1) => (n * 100).toFixed(digits) + '%';
const fmtPctSigned = (n, digits = 1) => (n > 0 ? '+' : '') + (n * 100).toFixed(digits) + '%';
const fmtAgo = (d) => {
  const now = window.MOCK ? window.MOCK.now : new Date();
  const ms = now - d;
  const s = ms / 1000;
  if (s < 60) return Math.round(s) + 's';
  const m = s / 60;
  if (m < 60) return Math.round(m) + 'm';
  const h = m / 60;
  if (h < 24) return h.toFixed(1) + 'h';
  return Math.round(h / 24) + 'd';
};
const fmtHold = h => {
  if (h < 1) return Math.round(h * 60) + 'm';
  if (h < 24) return h.toFixed(1) + 'h';
  return (h / 24).toFixed(1) + 'd';
};
// Live countdown to a resolution timestamp. Returns null when no target known.
const fmtCountdown = (resolvesAt) => {
  if (!resolvesAt) return null;
  const target = resolvesAt instanceof Date ? resolvesAt : new Date(resolvesAt);
  if (isNaN(target)) return null;
  const now = window.MOCK ? window.MOCK.now : new Date();
  let s = Math.floor((target - now) / 1000);
  if (s <= 0) return '00:00:00';
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600);  s -= h * 3600;
  const m = Math.floor(s / 60);    s -= m * 60;
  const pad = n => String(n).padStart(2, '0');
  if (d > 0) return `${d}d ${pad(h)}:${pad(m)}:${pad(s)}`;
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
};

// ---------- NotificationBell ----------
// Self-contained: fetches /api/notifications on its own 30s cycle, independent
// of the main /api/data loop. Bell icon + unread-error badge; click opens a popup.
function NotificationBell() {
  const [items, setItems] = useState([]);
  const [errorCount, setErrorCount] = useState(0);
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    const load = async () => {
      try {
        const r = await fetch('/api/notifications?limit=100');
        if (!r.ok) return;
        const d = await r.json();
        setItems(d.notifications || []);
        setErrorCount(d.error_count || 0);
      } catch (e) { /* leave last-known list on a transient failure */ }
    };
    load();
    const iv = setInterval(load, 30_000);
    return () => clearInterval(iv);
  }, []);

  // Close the popup on any outside click.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  const sevIcon = { error: '⛔', warning: '⚠', info: 'ℹ' };

  return (
    <div className="notif" ref={ref}>
      <button
        className="notif-bell"
        title="Notifications"
        onClick={() => setOpen(o => !o)}
      >
        🔔
        {errorCount > 0 && <span className="notif-badge">{errorCount > 99 ? '99+' : errorCount}</span>}
      </button>
      {open && (
        <div className="notif-popup">
          <div className="notif-popup-head">
            <span>Notifications</span>
            <span className="dim">{items.length}</span>
          </div>
          <div className="notif-list">
            {items.length === 0 && (
              <div className="notif-empty">No notifications</div>
            )}
            {items.map(n => (
              <div key={n.id} className={`notif-item notif-${n.severity || 'info'}`}>
                <span className="notif-item-icon">{sevIcon[n.severity] || 'ℹ'}</span>
                <div className="notif-item-body">
                  <div className="notif-item-msg">{n.message}</div>
                  <div className="notif-item-meta mono">
                    {n.kind} · {fmtAgo(new Date(n.timestamp))} ago
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------- TopBar ----------
function TopBar({ portfolio, scanLog, activeTab, setActiveTab }) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const i = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(i);
  }, []);
  const lastScanAgo = fmtAgo(scanLog.last_scan_at);
  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true" />
        <span className="brand-word">stormedge<em>.</em></span>
        <span className="brand-tag">desk</span>
      </div>
      <div className="top-nav">
        <div className={`nav-item ${activeTab === 'desk' ? 'active' : ''}`} onClick={() => setActiveTab('desk')}>Desk</div>
        <div className={`nav-item ${activeTab === 'archive' ? 'active' : ''}`} onClick={() => setActiveTab('archive')}>Archive</div>
        <div className={`nav-item ${activeTab === 'models' ? 'active' : ''}`} onClick={() => setActiveTab('models')}>Signals</div>
      </div>
      <div className="top-right">
        <span className={`mode-pill mode-${portfolio.mode.toLowerCase()}`}>
          <span className="mode-dot" />
          {portfolio.mode}
        </span>
        <div className="last-scan">
          <span className="dim">last scan</span>
          <span className="mono">{lastScanAgo} ago</span>
          <span className="scan-pulse" />
        </div>
        <NotificationBell />
        <a href="/api/logout" className="user-avatar" title="Sign out">↩</a>
      </div>
    </header>
  );
}

// ---------- HeaderStrip (KPIs) ----------
function KpiCard({ label, value, sub, tone, mono = true, children }) {
  return (
    <div className={`kpi kpi-${tone || 'neutral'}`}>
      <div className="kpi-label">{label}</div>
      <div className={`kpi-value ${mono ? 'mono' : ''}`}>{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
      {children}
    </div>
  );
}

function CircuitMeter({ used, limit, pnl }) {
  const pct = Math.max(0, Math.min(1, used));
  const tripped = pct >= 1.0;
  return (
    <div className="circuit">
      <div className="circuit-bar">
        <div className="circuit-fill" style={{ width: (pct * 100).toFixed(1) + '%' }} />
        {[0.25, 0.5, 0.75].map(t => (
          <div key={t} className="circuit-tick" style={{ left: (t * 100) + '%' }} />
        ))}
      </div>
      <div className="circuit-meta mono">
        <span>{fmtUSD(pnl, true)}</span>
        <span className="dim">limit {fmtUSD(limit)}</span>
      </div>
      {tripped && <span className="circuit-tripped">DAILY LIMIT EXCEEDED</span>}
    </div>
  );
}

function CircuitBreakerBanner({ portfolio }) {
  if (!portfolio.circuit_tripped) return null;
  return (
    <div className="circuit-banner">
      <span className="circuit-banner-icon">⚠</span>
      <span>Daily loss limit of ${Math.abs(portfolio.daily_loss_limit).toFixed(2)} reached. Trading halted until midnight UTC.</span>
    </div>
  );
}

function HeaderStrip({ portfolio }) {
  const equityChange = portfolio.total_equity - portfolio.starting_bankroll;
  const equityChangePct = equityChange / portfolio.starting_bankroll;
  return (
    <section className="header-strip">
      <KpiCard
        label="Total equity"
        value={fmtUSD(portfolio.total_equity)}
        sub={<span className={equityChange >= 0 ? 'pos' : 'neg'}>
          {fmtUSD(equityChange, true)} <span className="dim">since start</span>
        </span>}
        tone="hero"
      />
      <KpiCard
        label="Available cash"
        value={fmtUSD(portfolio.available_cash)}
        sub={<span className="dim">{fmtPct(portfolio.available_cash / portfolio.total_equity)} of equity</span>}
      />
      <KpiCard
        label="Locked in positions"
        value={fmtUSD(portfolio.locked_cash)}
        sub={<span className="dim">exposure {fmtPct(portfolio.exposure_pct)} <span className="sep">·</span> cap 30%</span>}
      />
      <KpiCard
        label="Today's P&L"
        value={fmtUSD(portfolio.daily_pnl, true)}
        tone={portfolio.daily_pnl < 0 ? 'neg' : 'pos'}
        mono={true}
      >
        <CircuitMeter used={portfolio.circuit_breaker_used} limit={portfolio.daily_loss_limit} pnl={portfolio.daily_pnl} />
      </KpiCard>
    </section>
  );
}

const CITY_PAGE_SIZE = 8;

// ---------- GlobePanel ----------
function GlobePanel({ cities, cityActivity, positions, scanLog }) {
  const wrapRef = useRef(null);
  const [hover, setHover] = useState(null);
  const [hoverPos, setHoverPos] = useState({ x: 0, y: 0 });
  const [selected, setSelected] = useState(null);
  const [cityPage, setCityPage] = useState(0);

  useEffect(() => {
    if (!wrapRef.current || !window.StormGlobe) return;
    const g = new window.StormGlobe(wrapRef.current, {
      cities,
      cityActivity,
      onCityHover: (c, m) => {
        setHover(c);
        if (m) setHoverPos({ x: m.x, y: m.y });
      },
      onCityClick: (c) => setSelected(c),
    });
    g.start();
    return () => g.stop();
  }, []);

  const activeCities = cities.filter(c => (cityActivity[c.key] || cityActivity[c.name]));
  const counts = {
    active: positions.length,
    signal: Object.values(cityActivity).filter(a => a.state === 'signal').length,
    scanned: Object.values(cityActivity).filter(a => a.state === 'scanned').length,
  };

  const hoverActivity = hover && (cityActivity[hover.key] || cityActivity[hover.name]);
  const totalCityPages = Math.max(1, Math.ceil(activeCities.length / CITY_PAGE_SIZE));
  const citySlice = activeCities.slice(cityPage * CITY_PAGE_SIZE, (cityPage + 1) * CITY_PAGE_SIZE);

  return (
    <section className="card globe-card">
      <header className="card-head">
        <div>
          <h2>Live coverage</h2>
          <p className="card-sub">{cities.length} weather stations · {counts.active} active · {counts.signal} shadow · {counts.scanned} scanned in last cycle</p>
        </div>
        <div className="globe-legend">
          <span className="lg lg-active"><i /> open position</span>
          <span className="lg lg-signal"><i /> shadow signal (skipped)</span>
          <span className="lg lg-scanned"><i /> scanned (no signal)</span>
        </div>
      </header>
      <div className="globe-body">
        <div className="globe-canvas" ref={wrapRef}>
          {hover && (
            <div className="globe-tip" style={{ left: hoverPos.x + 12, top: hoverPos.y + 12 }}>
              <div className="tip-name">{hover.name}</div>
              <div className="tip-coords mono">{hover.lat.toFixed(2)}°, {hover.lon.toFixed(2)}°</div>
              {hoverActivity && hoverActivity.state === 'active' && hoverActivity.position && (
                <div className="tip-row">
                  <span className="dot pos" /> open · {hoverActivity.position.side} @ {hoverActivity.position.entry_price.toFixed(2)}
                </div>
              )}
              {hoverActivity && hoverActivity.state === 'signal' && (
                <div className="tip-row"><span className="dot sig" /> flagged signal</div>
              )}
              {hoverActivity && hoverActivity.state === 'scanned' && (
                <div className="tip-row"><span className="dot sc" /> in last scan</div>
              )}
            </div>
          )}
        </div>
        <aside className="globe-side">
          <div className="side-head">
            <span className="side-title">station activity</span>
            <span className="side-count mono">{activeCities.length}</span>
          </div>
          <ul className="city-list">
            {citySlice.map(c => {
              const act = cityActivity[c.key] || cityActivity[c.name];
              const isPos = act.state === 'active';
              const pos = isPos ? act.position : null;
              return (
                <li
                  key={c.key}
                  className={`city-row state-${act.state} ${selected === c ? 'sel' : ''}`}
                  onClick={() => setSelected(c)}
                  onMouseEnter={() => setHover(c)}
                  onMouseLeave={() => setHover(null)}
                >
                  <span className={`state-dot dot-${act.state}`} />
                  <span className="city-name">{c.name}</span>
                  {isPos && (
                    <span className="city-meta mono">
                      <span className={`side-tag side-${pos.side.toLowerCase()}`}>{pos.side}</span>
                      <span className="dim">{fmtUSD(pos.size_usdc)}</span>
                    </span>
                  )}
                  {act.state === 'signal' && <span className="city-meta dim">shadow</span>}
                  {act.state === 'scanned' && act.skip && (
                    <span className="city-meta dim trunc">{act.skip.bucket}</span>
                  )}
                </li>
              );
            })}
          </ul>
          <div className="side-foot">
            {totalCityPages > 1 ? (
              <Pagination page={cityPage} total={totalCityPages} onChange={setCityPage} />
            ) : (
              <span className="dim"></span>
            )}
            <span className="dim">{cityPage + 1} / {totalCityPages}</span>
          </div>
        </aside>
      </div>
    </section>
  );
}

// ---------- OpenPositions ----------
function OpenPositions({ positions, maxPositions }) {
  const cap = maxPositions || 4;
  const [, setTick] = useState(0);
  useEffect(() => {
    const i = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(i);
  }, []);
  return (
    <section className="card positions-card">
      <header className="card-head">
        <div>
          <h2>Open positions</h2>
          <p className="card-sub">{positions.length} open · cap {cap}</p>
        </div>
        <span className="pill subtle">{positions.length} / {cap} cap</span>
      </header>
      {positions.length === 0 ? (
        <div style={{ padding: '8px 4px' }} />
      ) : (
        <div className="table positions-table">
          <div className="thead">
            <div>City</div>
            <div>Side</div>
            <div className="r">Entry</div>
            <div className="r">Mid</div>
            <div className="r">Size</div>
            <div className="r">P&L</div>
            <div className="r">Resolves in</div>
          </div>
          <div className="positions-scroll-wrapper">
            {positions.map(p => {
            const countdown = fmtCountdown(p.resolves_at);
            // entry_price and current_price are both the token's own price (YES or NO).
            // PnL = (current - entry) / entry * size for both sides.
            const pnl = (p.current_price - p.entry_price) / p.entry_price * p.size_usdc;
            const pnlPct = p.size_usdc > 0 ? pnl / p.size_usdc : 0;
            return (
              <div className="trow" key={p.id}>
                <div className="cell-city">
                  <div className="city-line">{p.city}</div>
                  <div className="city-q">{p.question}</div>
                </div>
                <div>
                  <span className={`side-tag side-${p.side.toLowerCase()}`}>{p.side}</span>
                  {p.bucket && <div className="dim small">{p.bucket}</div>}
                </div>
                <div className="r mono">{p.entry_price.toFixed(2)}</div>
                <div className="r mono">
                  {p.price_status === 'live'
                    ? p.current_price.toFixed(2)
                    : p.price_status !== 'unavailable' && p.current_price != null
                      ? <span className="dim">{p.current_price.toFixed(2)}</span>
                      : <span className="dim">—</span>}
                </div>
                <div className="r mono">{fmtUSD(p.size_usdc)}</div>
                <div className={`r mono ${p.price_status === 'live' ? (pnl >= 0 ? 'pos' : 'neg') : ''}`}>
                  {p.price_status === 'live'
                    ? <>{fmtUSD(pnl, true)}<div className="small">{fmtPctSigned(pnlPct)}</div></>
                    : p.price_status === 'unavailable'
                      ? <span className="dim small">Price unavailable</span>
                      : <span className="dim small">Pending resolution</span>
                  }
                </div>
                <div className="r mono dim">{countdown || '—'}</div>
              </div>
            );
          })}
          </div>
        </div>
      )}
    </section>
  );
}

// ---------- EquityCurve ----------
function EquityCurve({ equity, startingBankroll, totalEquity }) {
  const [W, setWidth] = useState(1000);
  const containerRef = useRef(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const observer = new ResizeObserver(entries => {
      for (let entry of entries) {
        const w = entry.contentRect.width;
        if (w > 0) setWidth(w);
      }
    });
    observer.observe(containerRef.current);
    const rect = containerRef.current.getBoundingClientRect();
    if (rect.width > 0) setWidth(rect.width);
    return () => observer.disconnect();
  }, []);

  const H = 200, padL = 38, padR = 12, padT = 18, padB = 28;

  // Normalise: ensure Date objects
  const pts = equity.map(p => ({
    t: p.t instanceof Date ? p.t : new Date(p.t),
    balance: p.balance,
  }));

  // Patch the last point to total equity (cash + locked) so open positions
  // don't make the curve look like a loss when cash was simply deployed.
  const nowDate = window.MOCK ? window.MOCK.now : new Date();
  if (totalEquity != null && pts.length > 0) {
    pts[pts.length - 1] = { t: nowDate, balance: totalEquity };
  }

  // Need at least 2 distinct points
  if (pts.length < 2 || pts[0].t.getTime() === pts[pts.length - 1].t.getTime()) {
    const seed = startingBankroll || 20;
    pts.push({ t: nowDate, balance: pts.length > 0 ? pts[pts.length - 1].balance : seed });
  }

  const xs = pts.map(p => p.t.getTime());
  const ys = pts.map(p => p.balance);
  const xMin = xs[0], xMax = xs[xs.length - 1];
  const yMin = Math.min(...ys) - 0.4;
  const yMax = Math.max(...ys) + 0.4;
  const xFn = t => padL + (xMax > xMin ? (t - xMin) / (xMax - xMin) : 0.5) * (W - padL - padR);
  const yFn = v => padT + (1 - (yMax > yMin ? (v - yMin) / (yMax - yMin) : 0.5)) * (H - padT - padB);

  const path = pts.map((p, i) => (i === 0 ? 'M' : 'L') + xFn(p.t.getTime()).toFixed(1) + ',' + yFn(p.balance).toFixed(1)).join(' ');
  const areaPath = path + ' L' + xFn(xMax).toFixed(1) + ',' + (H - padB) + ' L' + xFn(xMin).toFixed(1) + ',' + (H - padB) + ' Z';

  const seed = startingBankroll || 20;
  const last = totalEquity != null ? totalEquity : ys[ys.length - 1];
  const change = last - seed;
  const changePct = seed > 0 ? change / seed : 0;
  const yGrid = [yMin, (yMin + yMax) / 2, yMax];

  return (
    <section className="card">
      <header className="card-head">
        <div>
          <h2>Equity curve</h2>
          <p className="card-sub">cash + open positions · all time · initial bankroll {fmtUSD(seed)}</p>
        </div>
        <div className="equity-stat">
          <div className={`mono lg ${change >= 0 ? 'pos' : 'neg'}`}>{fmtUSD(change, true)}</div>
          <div className={`mono small ${change >= 0 ? 'pos' : 'neg'}`}>{fmtPctSigned(changePct)}</div>
        </div>
      </header>
      <div className="equity-chart" ref={containerRef}>
        <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H}>
          <defs>
            <linearGradient id="area-grad" x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor="rgba(245,177,60,0.28)" />
              <stop offset="100%" stopColor="rgba(245,177,60,0)" />
            </linearGradient>
          </defs>
          {yGrid.map((g, i) => (
            <g key={i}>
              <line x1={padL} x2={W - padR} y1={yFn(g)} y2={yFn(g)} stroke="rgba(255,255,255,0.05)" />
              <text x={padL - 6} y={yFn(g) + 3} fill="rgba(255,255,255,0.32)" fontSize="10" textAnchor="end" fontFamily="JetBrains Mono">${g.toFixed(1)}</text>
            </g>
          ))}
          <line x1={padL} x2={W - padR} y1={yFn(seed)} y2={yFn(seed)} stroke="rgba(255,255,255,0.18)" strokeDasharray="2 3" />
          <text x={W - padR} y={yFn(seed) - 4} fill="rgba(255,255,255,0.4)" fontSize="9.5" textAnchor="end" fontFamily="JetBrains Mono">SEED</text>
          <path d={areaPath} fill="url(#area-grad)" />
          <path d={path} stroke="#f5b13c" strokeWidth="1.4" fill="none" strokeLinejoin="round" />
          {[0, 0.5, 1].map((f, i) => {
            const t = xMin + (xMax - xMin) * f;
            const d = new Date(t);
            const label = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
            return <text key={i} x={padL + f * (W - padL - padR)} y={H - 8} fill="rgba(255,255,255,0.32)" fontSize="10" textAnchor={i === 0 ? 'start' : (i === 1 ? 'middle' : 'end')} fontFamily="JetBrains Mono">{label}</text>;
          })}
          <circle cx={xFn(xMax)} cy={yFn(last)} r="3" fill="#f5b13c" />
          <circle cx={xFn(xMax)} cy={yFn(last)} r="6" fill="rgba(245,177,60,0.2)" />
        </svg>
      </div>
    </section>
  );
}

// ---------- PerformanceStats ----------
const PERF_PERIODS = ['30d', '6m', '1y'];
const PERF_LABELS  = { '30d': '30 days', '6m': '6 months', '1y': '1 year' };

function PerformanceStats({ stats }) {
  const [period, setPeriod] = useState('30d');
  // Support both the new nested shape {30d:{…},6m:{…},1y:{…}} and the old flat shape
  const isNested = stats && typeof stats['30d'] === 'object';
  const s = isNested ? (stats[period] || stats['30d']) : (stats || {});
  const periodLabel = PERF_LABELS[period];

  const items = [
    { label: 'Win rate',          value: fmtPct(s.win_rate),            sub: `${s.total_trades} trades` },
    { label: 'Realized P&L',      value: fmtUSD(s.realized_pnl, true),  sub: periodLabel,
      tone: s.realized_pnl >= 0 ? 'pos' : 'neg' },
    { label: 'Avg edge at entry',  value: fmtPct(s.avg_edge),            sub: 'threshold 8.0%' },
    { label: 'Avg hold',           value: fmtHold(s.avg_hold_hours),     sub: 'time in position' },
    { label: 'Best trade',         value: fmtUSD(s.best_trade, true),    sub: 'single trade', tone: 'pos' },
    { label: 'Worst trade',        value: fmtUSD(s.worst_trade, true),   sub: 'single trade', tone: s.worst_trade >= 0 ? 'pos' : 'neg' },
  ];

  return (
    <section className="card">
      <header className="card-head">
        <div>
          <h2>Performance · {period}</h2>
          <p className="card-sub">resolved trades only · realized cash</p>
        </div>
        <div className="period-tabs">
          {PERF_PERIODS.map(p => (
            <button
              key={p}
              className={`period-tab ${p === period ? 'active' : ''}`}
              onClick={() => setPeriod(p)}
            >
              {p}
            </button>
          ))}
        </div>
      </header>
      <div className="perf-grid">
        {items.map(it => (
          <div key={it.label} className="perf-tile">
            <div className="kpi-label">{it.label}</div>
            <div className={`mono perf-val ${it.tone || ''}`}>{it.value}</div>
            <div className="kpi-sub dim">{it.sub}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

const TRADES_PAGE_SIZE = 15;

// ---------- Pagination ----------
function Pagination({ page, total, onChange }) {
  if (total <= 1) return null;
  return (
    <div className="pagination">
      <button className="pg-btn" onClick={() => onChange(page - 1)} disabled={page === 0}>‹</button>
      <span className="mono pg-info">{page + 1} / {total}</span>
      <button className="pg-btn" onClick={() => onChange(page + 1)} disabled={page === total - 1}>›</button>
    </div>
  );
}

// ---------- RecentTrades ----------
function RecentTrades({ trades }) {
  const [page, setPage] = useState(0);
  const totalPages = Math.max(1, Math.ceil(trades.length / TRADES_PAGE_SIZE));
  const safePage = Math.min(page, totalPages - 1);
  const slice = trades.slice(safePage * TRADES_PAGE_SIZE, (safePage + 1) * TRADES_PAGE_SIZE);
  return (
    <section className="card">
      <header className="card-head">
        <div>
          <h2>All trades</h2>
          <p className="card-sub">{trades.length} closed · sorted by exit time · page {safePage + 1} of {totalPages}</p>
        </div>
        <Pagination page={safePage} total={totalPages} onChange={setPage} />
      </header>
      {trades.length === 0 ? (
        <div style={{ padding: '8px 4px' }} />
      ) : (
        <div className="table trades-table">
          <div className="thead">
            <div>City</div>
            <div>Side</div>
            <div className="r">Entry → Exit</div>
            <div className="r">Size</div>
            <div className="r">P&L</div>
            <div>Exit reason</div>
            <div className="r">Held</div>
            <div className="r">Ago</div>
          </div>
          {slice.map(t => {
            const closedAt = t.closed_at instanceof Date ? t.closed_at : new Date(t.closed_at);
            const reasonClass = t.exit_reason.includes('Stop') ? 'stop'
              : t.exit_reason.includes('Edge') || t.exit_reason.includes('decay') ? 'decay'
              : t.exit_reason.includes('YES') ? 'resyes'
              : 'resno';
            return (
              <div className="trow" key={t.id}>
                <div className="cell-city">
                  <div className="city-line">{t.city}</div>
                  <div className="city-q trunc">{t.question}</div>
                </div>
                <div><span className={`side-tag side-${t.side.toLowerCase()}`}>{t.side}</span></div>
                <div className="r mono">
                  <span>{t.entry_price.toFixed(2)}</span>
                  <span className="arrow">→</span>
                  <span>{t.exit_price.toFixed(2)}</span>
                </div>
                <div className="r mono">{fmtUSD(t.size_usdc)}</div>
                <div className={`r mono ${t.pnl >= 0 ? 'pos' : 'neg'}`}>
                  {fmtUSD(t.pnl, true)}
                  <div className="small">{(t.pnl_pct > 0 ? '+' : '') + t.pnl_pct.toFixed(1) + '%'}</div>
                </div>
                <div className={`reason reason-${reasonClass}`}>{t.exit_reason}</div>
                <div className="r mono dim">{fmtHold(t.hold_hours)}</div>
                <div className="r mono dim">{fmtAgo(closedAt)}</div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

// ---------- RecentSignals ----------
const GATE_TONE = {
  'Taken':                    'pos',
  'Models disagreed':         'neutral',
  'Model spread too wide':    'neutral',
  'Market spread too wide':   'neutral',
  'Too close to bucket edge': 'neutral',
  'Direction mismatch':       'neutral',
  'YES disabled':             'dim',
  'Edge below threshold':     'dim',
  'Other skip':               'dim',
};
// Fixed draw order so the breakdown bar and legend read left-to-right by
// severity/interest rather than shuffling with whatever the data happens to contain.
const GATE_ORDER = [
  'Taken', 'Models disagreed', 'Model spread too wide', 'Market spread too wide',
  'Too close to bucket edge', 'Direction mismatch', 'YES disabled', 'Edge below threshold', 'Other skip',
];

function OutcomeBreakdown({ rows, activeFilter, setActiveFilter }) {
  const counts = {};
  for (const r of rows) counts[r.gate_outcome] = (counts[r.gate_outcome] || 0) + 1;
  const present = GATE_ORDER.filter(k => counts[k]);
  const total = rows.length || 1;
  return (
    <div className="outcome-panel">
      <div className="outcome-title">Outcome breakdown — click a segment to filter</div>
      <div className="outcome-bar">
        {present.map(k => {
          const pct = (counts[k] / total) * 100;
          const tone = GATE_TONE[k] || 'dim';
          return (
            <div
              key={k}
              className={`outcome-seg tone-${tone} ${activeFilter === k ? 'active' : ''}`}
              style={{ width: pct + '%' }}
              title={`${k}: ${counts[k]}`}
              onClick={() => setActiveFilter(activeFilter === k ? null : k)}
            >
              {pct > 6 ? counts[k] : ''}
            </div>
          );
        })}
      </div>
      <div className="outcome-legend">
        {present.map(k => (
          <span
            key={k}
            className={`outcome-legend-item ${activeFilter && activeFilter !== k ? 'disabled' : ''}`}
            onClick={() => setActiveFilter(activeFilter === k ? null : k)}
          >
            <span className={`outcome-swatch tone-${GATE_TONE[k] || 'dim'}`} />
            {k} <span className="mono dim">{counts[k]}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

function SignalDetail({ s }) {
  const models = Object.entries(s.raw_models || {});
  const bucketLabel = s.bucket_low != null && s.bucket_high != null
    ? `${s.bucket_low.toFixed(1)}–${s.bucket_high.toFixed(1)}°F`
    : s.bucket_low != null ? `> ${s.bucket_low.toFixed(1)}°F`
    : s.bucket_high != null ? `< ${s.bucket_high.toFixed(1)}°F`
    : '—';
  return (
    <div className="detail-row">
      <div className="detail-block">
        <h4>Model forecasts</h4>
        <div className="model-grid">
          {models.length === 0 && <div className="dim small">no data</div>}
          {models.map(([name, temp]) => (
            <div className="model-row" key={name}>
              <span className="dim">{name}</span>
              <span className="mono">{temp.toFixed(2)}°F</span>
            </div>
          ))}
        </div>
      </div>
      <div className="detail-block">
        <h4>Market snapshot</h4>
        <div className="kv-grid">
          <div className="kv-row"><span className="dim">Bucket</span><span className="mono">{bucketLabel}</span></div>
          <div className="kv-row"><span className="dim">Model probability</span><span className="mono">{s.model_prob != null ? fmtPct(s.model_prob, 0) : '—'}</span></div>
          <div className="kv-row"><span className="dim">YES price</span><span className="mono">{s.yes_price != null ? '$' + s.yes_price.toFixed(3) : '—'}</span></div>
          <div className="kv-row"><span className="dim">NO price</span><span className="mono">{s.no_price != null ? '$' + s.no_price.toFixed(3) : '—'}</span></div>
          <div className="kv-row"><span className="dim">Ensemble σ</span><span className="mono">{s.ensemble_std != null ? s.ensemble_std.toFixed(2) + '°F' : '—'}</span></div>
          <div className="kv-row"><span className="dim">Market spread</span><span className="mono">{s.market_spread_frac != null ? fmtPct(s.market_spread_frac) : '—'}</span></div>
        </div>
      </div>
      <div className="detail-block">
        <h4>{s.gate_outcome === 'Taken' ? 'Entry reason' : 'Skip reason'}</h4>
        <div className="reason-full">{s.reason}</div>
      </div>
    </div>
  );
}

function RecentSignals({ signals }) {
  const rows = signals || [];
  const [activeFilter, setActiveFilter] = useState(null);
  const [search, setSearch] = useState('');
  const [expanded, setExpanded] = useState(null);

  const filtered = rows.filter(s => {
    if (activeFilter && s.gate_outcome !== activeFilter) return false;
    if (search) {
      const q = search.toLowerCase();
      if (!(s.city || '').toLowerCase().includes(q) && !(s.market_id || '').toLowerCase().includes(q)) return false;
    }
    return true;
  });
  const maxEdge = Math.max(...rows.map(s => Math.abs(s.edge || 0)), 0.01);

  return (
    <section className="card">
      <header className="card-head">
        <div>
          <h2>Recently scanned signals</h2>
          <p className="card-sub">{rows.length} candidates from the last scan cycle · every market the bot looked at, taken or skipped</p>
        </div>
      </header>
      <OutcomeBreakdown rows={rows} activeFilter={activeFilter} setActiveFilter={setActiveFilter} />
      <div className="signals-controls">
        <input
          type="search"
          className="signals-search"
          placeholder="Search city or market ID…"
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
        <span className="result-count mono dim">{filtered.length} of {rows.length} shown</span>
      </div>
      {filtered.length === 0 ? (
        <div style={{ padding: '8px 4px' }} className="dim small">No signals match.</div>
      ) : (
        <div className="table signals-table">
          <div className="thead">
            <div>City</div>
            <div>Target date</div>
            <div className="r">Edge</div>
            <div className="r">Agreement</div>
            <div className="r">Model spread °F</div>
            <div className="r">Mean gap °F</div>
            <div>Gate outcome</div>
            <div>Reason</div>
          </div>
          <div className="signals-scroll-wrapper">
            {filtered.map((s, i) => {
              const tone = GATE_TONE[s.gate_outcome] || 'dim';
              const barPct = maxEdge > 0 ? Math.min(100, (Math.abs(s.edge || 0) / maxEdge) * 100) : 0;
              const key = s.ts + '_' + s.market_id + '_' + i;
              const isOpen = expanded === key;
              return (
                <React.Fragment key={key}>
                  <div className={`trow clickable ${isOpen ? 'expanded' : ''}`} onClick={() => setExpanded(isOpen ? null : key)}>
                    <div className="cell-city">
                      <div className="city-line">{s.city || '—'}</div>
                      <div className="city-q dim small">{fmtAgo(new Date(s.ts))} ago</div>
                    </div>
                    <div className="dim mono small">{s.target_date || '—'}</div>
                    <div className="r">
                      <div className="edge-bar-wrap">
                        <div className="edge-bar-track"><div className="edge-bar-fill" style={{ width: barPct.toFixed(0) + '%' }} /></div>
                        <span className="mono">{s.edge != null ? fmtPctSigned(s.edge) : '—'}</span>
                      </div>
                    </div>
                    <div className="r mono dim">{s.agreement != null ? fmtPct(s.agreement, 0) : '—'}</div>
                    <div className="r mono dim">{s.model_spread != null ? s.model_spread.toFixed(1) : '—'}</div>
                    <div className="r mono dim">{s.mean_gap != null ? s.mean_gap.toFixed(1) : '—'}</div>
                    <div><span className={`gate-pill gate-${tone}`}>{s.gate_outcome}</span></div>
                    <div className="reason dim small trunc" title={s.reason}>{s.reason}</div>
                  </div>
                  {isOpen && <SignalDetail s={s} />}
                </React.Fragment>
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
}

// ---------- ScanFeed ----------
function ScanFeed({ scanLog }) {
  const [open, setOpen] = useState(true);
  return (
    <section className={`card scan-feed ${open ? 'open' : 'closed'}`}>
      <header className="card-head clickable" onClick={() => setOpen(o => !o)}>
        <div>
          <h2>Scan feed</h2>
          <p className="card-sub">
            {fmtAgo(scanLog.last_scan_at)} ago
            <span className="sep">·</span>
            {scanLog.duration_ms.toLocaleString()}ms
            <span className="sep">·</span>
            {scanLog.markets_seen.toLocaleString()} markets · {scanLog.candidates} candidates · <span className="pos">{scanLog.filled} filled</span>
          </p>
        </div>
        <span className="chev">{open ? '▾' : '▸'}</span>
      </header>
      {open && (
        <div className="scan-body">
          <div className="scan-funnel">
            {[
              { label: 'Markets seen',  v: scanLog.markets_seen,  tone: 'dim' },
              { label: 'Candidates',    v: scanLog.candidates,    tone: 'neutral' },
              { label: 'Shadow passed', v: scanLog.shadow_passed, tone: 'signal' },
              { label: 'Filled',        v: scanLog.filled,        tone: 'pos' },
            ].map((s, i, arr) => {
              const prev = i > 0 ? arr[i - 1].v : null;
              const conv = prev && prev > 0 ? (s.v / prev) * 100 : null;
              return (
                <div className="funnel-step" key={s.label}>
                  <div className="kpi-label">{s.label}</div>
                  <div className={`mono funnel-val tone-${s.tone}`}>{s.v.toLocaleString()}</div>
                  <div className="funnel-conv mono">
                    {conv !== null ? `${conv.toFixed(1)}% of prev` : ' '}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
}

// ---------- App ----------
function App() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [activeTab, setActiveTab] = useState('desk');

  useEffect(() => {
    const load = async () => {
      try {
        const r = await fetch('/api/data');
        if (r.status === 401) { window.location.href = '/'; return; }
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = await r.json();
        // Coerce ISO strings → Date objects so helpers work
        d.now = new Date(d.now);
        d.equity = d.equity.map(e => ({ ...e, t: new Date(e.t) }));
        d.positions = d.positions.map(p => ({ ...p, entry_time: new Date(p.entry_time) }));
        d.trades = d.trades.map(t => ({ ...t, closed_at: new Date(t.closed_at) }));
        d.scanLog.last_scan_at = new Date(d.scanLog.last_scan_at);
        d.scanLog.recent_skips = d.scanLog.recent_skips.map(s => ({ ...s, ts: new Date(s.ts) }));
        window.MOCK = d;
        setData(d);
      } catch (e) {
        setErr(e.message);
      }
    };
    load();
    const iv = setInterval(load, 30_000);
    return () => clearInterval(iv);
  }, []);

  if (!data) {
    return (
      <div className="loading-screen">
        <span>{err ? `⚠ ${err}` : '· loading ·'}</span>
        {err && <span style={{ fontSize: 11, color: 'var(--dim)', marginTop: 4 }}>
          <a href="/" style={{ color: 'var(--signal)' }}>← back to login</a>
        </span>}
      </div>
    );
  }

  const M = data;
  return (
    <div className="app">
      <TopBar portfolio={M.portfolio} scanLog={M.scanLog} activeTab={activeTab} setActiveTab={setActiveTab} />
      {activeTab === 'desk' && (
        <>
          <CircuitBreakerBanner portfolio={M.portfolio} />
          <HeaderStrip portfolio={M.portfolio} />
          <PerformanceStats stats={M.stats} />
          <div className="row row-main">
            <GlobePanel
              cities={M.cities}
              cityActivity={M.cityActivity}
              positions={M.positions}
              scanLog={M.scanLog}
            />
            <OpenPositions positions={M.positions} maxPositions={M.portfolio?.max_concurrent_positions} />
          </div>
          <EquityCurve equity={M.equity} startingBankroll={M.portfolio.starting_bankroll} totalEquity={M.portfolio.total_equity} />
        </>
      )}
      {activeTab === 'archive' && (
        <div>
          <RecentTrades trades={M.trades} />
        </div>
      )}
      {activeTab === 'models' && (
        <div>
          <RecentSignals signals={M.recentSignals} />
        </div>
      )}
      <footer className="page-foot">
        <span className="dim">stormedge · {M.portfolio.mode.toLowerCase()}-mode · polymarket weather bot</span>
        <span className="mono dim">UTC {M.now.toISOString().replace('T', ' ').slice(0, 19)}</span>
      </footer>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
