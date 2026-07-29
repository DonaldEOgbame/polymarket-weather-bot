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
        <div className={`nav-item ${activeTab === 'settings' ? 'active' : ''}`} onClick={() => setActiveTab('settings')}>Settings</div>
      </div>
      <div className="top-right">
        <span
          className={`mode-pill ${portfolio.archive_view ? 'mode-archive' : `mode-${portfolio.mode.toLowerCase()}`} ${portfolio.archive_available ? 'mode-toggleable' : ''}`}
          title={portfolio.archive_available
            ? (portfolio.archive_view ? 'Viewing saved paper era — click for live' : 'Click to view saved paper era')
            : undefined}
          onClick={async () => {
            if (!portfolio.archive_available) return;
            await fetch('/api/archive-view', {
              method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({on: !portfolio.archive_view}),
            });
            window.dispatchEvent(new Event('stormedge-refetch'));
          }}
        >
          <span className="mode-dot" />
          {portfolio.archive_view ? 'PAPER · SAVED' : portfolio.mode}
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
  // Measure against total capital paid in (seed + deposits), NOT the original
  // seed: a deposit adds cash without being profit, so dividing by the seed
  // would report a funding event as a gain. Withdrawn cash counts TOWARD the
  // result — money taken off the table is banked profit, not a trading loss.
  const capitalIn = portfolio.total_deposited || portfolio.starting_bankroll;
  const withdrawn = portfolio.total_withdrawn || 0;
  const equityChange = portfolio.total_equity + withdrawn - capitalIn;
  const equityChangePct = capitalIn ? equityChange / capitalIn : 0;
  return (
    <section className="header-strip">
      <KpiCard
        label="Total equity"
        value={fmtUSD(portfolio.total_equity)}
        sub={<span className={equityChange >= 0 ? 'pos' : 'neg'}>
          {fmtUSD(equityChange, true)} <span className="dim">vs capital in</span>
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
        sub={<span className="dim">exposure {fmtPct(portfolio.exposure_pct)} <span className="sep">·</span> cap {fmtPct(portfolio.max_total_exposure_fraction ?? 0.70, 0)}</span>}
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

// ---------- Settings ----------
// Percent-style knobs are stored as fractions but edited as whole percents —
// typing "70" is natural, 0.70 is not.
const PCT_FIELDS = { MAX_TOTAL_EXPOSURE_FRACTION: true, STOP_LOSS_PCT: true };

function NumField({ id, label, help, value, onChange, prefix, suffix, step, error, warn, disabled }) {
  return (
    <div className="field-row">
      <div>
        <div className="field-label">{label}</div>
        {help && <div className="field-help">{help}</div>}
        {error && <div className="field-error">{error}</div>}
        {!error && warn && <div className="field-warn">{warn}</div>}
      </div>
      <div className={`input ${error ? 'has-error' : ''}`}>
        {prefix && <span className="affix">{prefix}</span>}
        <input
          type="number" step={step || 'any'} value={value} disabled={disabled}
          onChange={e => onChange(e.target.value)} aria-label={label} id={id}
        />
        {suffix && <span className="affix">{suffix}</span>}
      </div>
    </div>
  );
}

function NumFieldText({ label, help, value, onChange, placeholder }) {
  return (
    <div className="field-row">
      <div>
        <div className="field-label">{label}</div>
        {help && <div className="field-help">{help}</div>}
      </div>
      <div className="input">
        <input type="text" value={value} placeholder={placeholder}
               onChange={e => onChange(e.target.value)} aria-label={label} />
      </div>
    </div>
  );
}

// What the bot will ACTUALLY do with a given set of values. Recomputed on every
// keystroke — this is the "show me the value as I tweak it" part.
function deriveImpact(v, ctx) {
  const equity = ctx.total_equity || 0;
  const size = Number(v.FIXED_POSITION_SIZE) || 0;
  const ceiling = Number(v.HARD_MAX_POSITION_SIZE) || 0;
  // strategy.py takes min() of the two, so the ceiling silently wins when lower.
  const effective = Math.min(size, ceiling);
  const exposureCap = equity * (Number(v.MAX_TOTAL_EXPOSURE_FRACTION) || 0);
  const slotsByExposure = effective > 0 ? Math.floor(exposureCap / effective) : 0;
  const slotsByCash = effective > 0 ? Math.floor((ctx.available_cash || 0) / effective) : 0;
  const maxConc = Number(v.MAX_CONCURRENT_POSITIONS) || 0;
  const slots = Math.max(0, Math.min(maxConc, slotsByExposure, slotsByCash));
  let binding = 'max concurrent';
  if (slots === slotsByCash && slotsByCash <= slotsByExposure && slotsByCash <= maxConc) binding = 'available cash';
  else if (slots === slotsByExposure && slotsByExposure <= maxConc) binding = 'exposure cap';
  // The daily loss limit is DERIVED: a budget of N full-stake losses, so the
  // dollar figure below rescales live as the stake or the budget is edited.
  const lossStakes = Number(v.DAILY_LOSS_STAKES) || 0;
  return {
    effective,
    clamped: size > ceiling,
    pctOfEquity: equity ? effective / equity : 0,
    exposureCap, slots, binding,
    // On a binary $0/$1 market the max loss per position IS the whole stake,
    // so this is a real worst case, not a scare number.
    worstCase: slots * effective,
    dailyLossDollars: effective * lossStakes,
    lossesToHalt: Math.ceil(lossStakes),
    stopLossPerTrade: v.ENABLE_STOP_LOSS ? effective * (Number(v.STOP_LOSS_PCT) || 0) : effective,
  };
}

function SettingsPanel({ portfolio }) {
  const [server, setServer] = useState(null);
  const [draft, setDraft] = useState(null);
  const [phase, setPhase] = useState('idle');   // idle|confirming|saving
  const [fieldErrors, setFieldErrors] = useState({});
  const [banner, setBanner] = useState(null);
  const [depositAmt, setDepositAmt] = useState('');
  const [depositConfirm, setDepositConfirm] = useState(false);
  const [depositBusy, setDepositBusy] = useState(false);
  const [eras, setEras] = useState(null);
  const [eraLabel, setEraLabel] = useState('');
  const [eraConfirm, setEraConfirm] = useState(false);
  const [eraBusy, setEraBusy] = useState(false);

  const load = async () => {
    try {
      const r = await fetch('/api/settings');
      if (r.status === 401) { window.location.href = '/'; return; }
      const d = await r.json();
      // Percent-style values are edited as whole numbers.
      const shown = { ...d.values };
      Object.keys(PCT_FIELDS).forEach(k => { if (shown[k] != null) shown[k] = +(shown[k] * 100).toFixed(4); });
      setServer({ ...d, shownValues: shown });
      setDraft(shown);
      try {
        const er = await fetch('/api/eras');
        if (er.ok) setEras(await er.json());
      } catch (e) { /* era card just shows less */ }
    } catch (e) { setBanner({ err: true, msg: 'Could not load settings: ' + e.message }); }
  };

  useEffect(() => { load(); }, []);

  if (!server || !draft) return <section className="card"><div className="dim small">Loading settings…</div></section>;

  if (server.archive_view) {
    return (
      <section className="card">
        <header className="card-head"><div><h2>Settings</h2></div></header>
        <div className="settings-note">
          Viewing the frozen paper-era archive. Switch back to live (the mode pill,
          top right) to change settings.
        </div>
      </section>
    );
  }

  // Values in real units (fractions, not percents) for impact + save.
  const realValues = (() => {
    const out = { ...draft };
    Object.keys(PCT_FIELDS).forEach(k => { if (out[k] != null && out[k] !== '') out[k] = Number(out[k]) / 100; });
    return out;
  })();
  const impact = deriveImpact(realValues, server.context);

  const dirtyKeys = Object.keys(server.shownValues)
    .filter(k => String(draft[k]) !== String(server.shownValues[k]));

  const set = (key, val) => {
    setDraft(d => {
      const next = { ...d, [key]: val };
      // The ceiling clamps the stake (strategy.py min()), so raising the stake
      // above it must raise it too — otherwise the change is a silent no-op.
      if (key === 'FIXED_POSITION_SIZE' && Number(val) > Number(d.HARD_MAX_POSITION_SIZE || 0)) {
        next.HARD_MAX_POSITION_SIZE = val;
      }
      return next;
    });
    setFieldErrors(fe => ({ ...fe, [key]: null }));
  };

  const save = async () => {
    setPhase('saving'); setBanner(null); setFieldErrors({});
    const payload = {};
    dirtyKeys.forEach(k => { payload[k] = realValues[k]; });
    // A stake raise auto-raises the ceiling; include it even if the user never
    // touched the field directly.
    if (payload.FIXED_POSITION_SIZE != null) payload.HARD_MAX_POSITION_SIZE = realValues.HARD_MAX_POSITION_SIZE;
    try {
      const r = await fetch('/api/settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings: payload }),
      });
      const d = await r.json();
      setPhase('idle');
      if (!r.ok) {
        if (d.field_errors) setFieldErrors(d.field_errors);
        setBanner({ err: true, msg: d.error || 'Save failed' });
        return;
      }
      // Applied live: the runtime store is already swapped, the next bot
      // decision uses the new values. Just re-sync the panel and the desk.
      await load();
      window.dispatchEvent(new Event('stormedge-refetch'));
      setBanner({ msg: d.changed && d.changed.length
        ? 'Applied immediately: ' + d.changed.join(', ')
        : (d.message || 'No changes.') });
    } catch (e) {
      setPhase('idle');
      setBanner({ err: true, msg: 'Save failed: ' + e.message });
    }
  };

  const doDeposit = async () => {
    setDepositBusy(true); setBanner(null);
    try {
      const r = await fetch('/api/deposit', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ amount: Number(depositAmt), confirm: true }),
      });
      const d = await r.json();
      if (!r.ok) setBanner({ err: true, msg: d.error || 'Deposit failed' });
      else {
        setBanner({ msg: `Deposit of ${fmtUSD(d.amount)} recorded — balance is now ${fmtUSD(d.new_balance)}.` });
        setDepositAmt(''); setDepositConfirm(false);
        await load();
        window.dispatchEvent(new Event('stormedge-refetch'));
      }
    } catch (e) { setBanner({ err: true, msg: e.message }); }
    setDepositBusy(false);
  };

  const startNewEra = async () => {
    setEraBusy(true); setBanner(null);
    try {
      const r = await fetch('/api/new-era', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: true, label: eraLabel.trim() || undefined }),
      });
      const d = await r.json();
      if (!r.ok) setBanner({ err: true, msg: d.error || 'Era cutover failed' });
      else {
        setBanner({ msg: `Era '${d.new_label}' opened at ${fmtUSD(d.seed)} — ` +
          `${d.archived_trades} trade(s) archived. ` +
          (d.seed === 0 ? 'Fund the wallet and the bot will book it automatically.' : '') });
        setEraLabel(''); setEraConfirm(false);
        await load();
        window.dispatchEvent(new Event('stormedge-refetch'));
      }
    } catch (e) { setBanner({ err: true, msg: e.message }); }
    setEraBusy(false);
  };

  const ctx = server.context;
  const depositNum = Number(depositAmt);
  const depositValid = depositNum > 0 && depositNum <= 10000;
  const bigDeposit = depositNum > 10 * (ctx.available_cash || 1);

  return (
    <div className="col-stack">
      {banner && <div className={`settings-note ${banner.err ? 'err' : ''}`}>{banner.msg}</div>}
      <div className="settings-grid">
        {/* ---- sizing ---- */}
        <section className="card">
          <header className="card-head">
            <div>
              <h2>Position sizing</h2>
              <p className="card-sub">how much each trade stakes</p>
            </div>
          </header>
          <NumField
            label="Stake per trade" prefix="$" step="0.25"
            value={draft.FIXED_POSITION_SIZE}
            onChange={v => set('FIXED_POSITION_SIZE', v)}
            error={fieldErrors.FIXED_POSITION_SIZE}
            help={`Every trade stakes exactly this. CLOB minimum is ${fmtUSD(ctx.min_position_size)}.`}
          />
          <NumField
            label="Per-trade ceiling" prefix="$" step="0.25"
            value={draft.HARD_MAX_POSITION_SIZE}
            onChange={v => set('HARD_MAX_POSITION_SIZE', v)}
            error={fieldErrors.HARD_MAX_POSITION_SIZE}
            help="Absolute cap applied on top of the stake."
            warn={Number(draft.HARD_MAX_POSITION_SIZE) === Number(draft.FIXED_POSITION_SIZE)
              && dirtyKeys.includes('HARD_MAX_POSITION_SIZE')
              ? 'Raised automatically to match the stake — the ceiling clamps it, so leaving it lower would keep every trade at the old size.'
              : null}
          />
          <div className="impact-block">
            <div className="impact-row">
              <span className="k">Effective stake</span>
              <span className={`v ${impact.clamped ? 'bad' : ''}`}>
                {fmtUSD(impact.effective)}{impact.clamped ? ' — clamped by ceiling' : ''}
              </span>
            </div>
            <div className="impact-row">
              <span className="k">Share of equity</span>
              <span className="v">{fmtPct(impact.pctOfEquity)} of {fmtUSD(ctx.total_equity)}</span>
            </div>
            <div className="impact-row">
              <span className="k">Positions allowed</span>
              <span className="v">{impact.slots} <span className="dim">— limited by {impact.binding}</span></span>
            </div>
            <div className="impact-row">
              <span className="k">Worst case open</span>
              <span className="v">{fmtUSD(impact.worstCase)} if all resolve to zero</span>
            </div>
            <div className="impact-row">
              <span className="k">Daily loss limit</span>
              <span className={`v ${impact.lossesToHalt <= 2 ? 'warn' : ''}`}>
                {fmtUSD(impact.dailyLossDollars)} — halts after {impact.lossesToHalt} loss{impact.lossesToHalt === 1 ? '' : 'es'}
              </span>
            </div>
          </div>
        </section>

        {/* ---- risk ---- */}
        <section className="card">
          <header className="card-head">
            <div>
              <h2>Risk limits</h2>
              <p className="card-sub">portfolio-level guards</p>
            </div>
          </header>
          <NumField
            label="Max concurrent positions" step="1"
            value={draft.MAX_CONCURRENT_POSITIONS}
            onChange={v => set('MAX_CONCURRENT_POSITIONS', v)}
            error={fieldErrors.MAX_CONCURRENT_POSITIONS}
            help="Refuse new entries once this many are open."
          />
          <NumField
            label="Daily loss budget" suffix="stakes" step="0.5"
            value={draft.DAILY_LOSS_STAKES}
            onChange={v => set('DAILY_LOSS_STAKES', v)}
            error={fieldErrors.DAILY_LOSS_STAKES}
            help={`Halts the day after this many full-stake losses — currently ${fmtUSD(impact.dailyLossDollars)}. Scales automatically when the stake changes.`}
          />
          <NumField
            label="Total exposure cap" suffix="%" step="5"
            value={draft.MAX_TOTAL_EXPOSURE_FRACTION}
            onChange={v => set('MAX_TOTAL_EXPOSURE_FRACTION', v)}
            error={fieldErrors.MAX_TOTAL_EXPOSURE_FRACTION}
            help={`Share of equity allowed in open positions — ${fmtUSD(impact.exposureCap)} right now.`}
          />
        </section>

        {/* ---- exits ---- */}
        <section className="card">
          <header className="card-head">
            <div>
              <h2>Exits</h2>
              <p className="card-sub">when a position is closed early</p>
            </div>
          </header>
          <div className="field-row">
            <div>
              <div className="field-label">Stop loss</div>
              <div className="field-help">Sell when the mid falls far enough below entry.</div>
            </div>
            <div className="seg">
              <button className={draft.ENABLE_STOP_LOSS ? 'on' : ''} onClick={() => set('ENABLE_STOP_LOSS', true)}>ON</button>
              <button className={!draft.ENABLE_STOP_LOSS ? 'on' : ''} onClick={() => set('ENABLE_STOP_LOSS', false)}>OFF</button>
            </div>
          </div>
          <NumField
            label="Stop loss level" suffix="%" step="5"
            value={draft.STOP_LOSS_PCT}
            disabled={!draft.ENABLE_STOP_LOSS}
            onChange={v => set('STOP_LOSS_PCT', v)}
            error={fieldErrors.STOP_LOSS_PCT}
            help={draft.ENABLE_STOP_LOSS
              ? `Cuts a loss at ${fmtUSD(impact.stopLossPerTrade)} instead of the full ${fmtUSD(impact.effective)}.`
              : 'Stop loss is off — a losing position rides to settlement.'}
          />
          <NumField
            label="Take profit price" prefix="$" step="0.01"
            value={draft.TAKE_PROFIT_PRICE}
            onChange={v => set('TAKE_PROFIT_PRICE', v)}
            error={fieldErrors.TAKE_PROFIT_PRICE}
            help="Sell the moment a real bid reaches this."
          />
        </section>

        {/* ---- bankroll ---- */}
        <section className="card">
          <header className="card-head">
            <div>
              <h2>Bankroll</h2>
              <p className="card-sub">record money paid into the account</p>
            </div>
          </header>
          <div className="impact-block" style={{ marginTop: 0 }}>
            <div className="impact-row"><span className="k">Available cash</span><span className="v">{fmtUSD(ctx.available_cash)}</span></div>
            <div className="impact-row"><span className="k">In open positions</span><span className="v">{fmtUSD(ctx.locked_cash)}</span></div>
            <div className="impact-row"><span className="k">Total capital in</span><span className="v">{fmtUSD(ctx.total_deposited)}</span></div>
          </div>
          <NumField
            label="Record a deposit" prefix="$" step="1"
            value={depositAmt}
            onChange={v => { setDepositAmt(v); setDepositConfirm(false); }}
            help="Adds cash to the ledger. Does not count as profit and needs no restart."
            warn={bigDeposit ? 'This is far larger than your current balance — is the figure in dollars, not naira?' : null}
          />
          {depositValid && (
            <div className="impact-row" style={{ marginTop: 4 }}>
              <span className="k">New balance would be</span>
              <span className="v">{fmtUSD((ctx.available_cash || 0) + depositNum)}</span>
            </div>
          )}
          <div style={{ display: 'flex', gap: 9, marginTop: 10 }}>
            {!depositConfirm ? (
              <button className="btn btn-ghost" disabled={!depositValid} onClick={() => setDepositConfirm(true)}>
                Record deposit
              </button>
            ) : (
              <>
                <button className="btn btn-primary" disabled={depositBusy} onClick={doDeposit}>
                  {depositBusy ? 'Recording…' : `Confirm ${fmtUSD(depositNum)}`}
                </button>
                <button className="btn btn-ghost" onClick={() => setDepositConfirm(false)}>Cancel</button>
              </>
            )}
          </div>
          <div className="field-help" style={{ marginTop: 8 }}>
            Deposits and withdrawals made on Polymarket itself are detected and booked
            automatically within two monitor cycles (~10 min) — this field is only for
            recording one before the bot notices.
          </div>
        </section>

        {/* ---- trading era ---- */}
        <section className="card">
          <header className="card-head">
            <div>
              <h2>Trading era</h2>
              <p className="card-sub">archive this run and start fresh</p>
            </div>
          </header>
          <div className="impact-block" style={{ marginTop: 0 }}>
            <div className="impact-row">
              <span className="k">Current era</span>
              <span className="v">{eras && eras.current ? `${eras.current.label} · since ${String(eras.current.started_at).slice(0, 10)}` : 'pre-era history'}</span>
            </div>
            <div className="impact-row">
              <span className="k">Archived eras</span>
              <span className="v">{eras ? (eras.archived || []).length + (eras.legacy_paper_archive ? 1 : 0) : '—'}</span>
            </div>
          </div>
          <NumFieldText
            label="New era label" value={eraLabel} onChange={setEraLabel}
            placeholder="live-2"
            help="Closes the current era: every trade and the bankroll ledger are frozen into a browsable archive, then the ledger re-seeds from the REAL wallet balance. Signals and calibration data carry over. Settings keep their values."
          />
          {ctx.open_positions > 0 && (
            <div className="field-warn">Blocked while {ctx.open_positions} position(s) are open — let them settle first.</div>
          )}
          <div style={{ display: 'flex', gap: 9, marginTop: 10 }}>
            {!eraConfirm ? (
              <button className="btn btn-ghost" disabled={ctx.open_positions > 0 || eraBusy}
                      onClick={() => setEraConfirm(true)}>
                Start new era
              </button>
            ) : (
              <>
                <button className="btn btn-primary" disabled={eraBusy} onClick={startNewEra}>
                  {eraBusy ? 'Archiving…' : 'Confirm — archive & start fresh'}
                </button>
                <button className="btn btn-ghost" disabled={eraBusy} onClick={() => setEraConfirm(false)}>Cancel</button>
              </>
            )}
          </div>
        </section>
      </div>

      <div className="settings-bar">
        <span className={dirtyKeys.length ? 'settings-dirty' : 'dim small'}>
          {dirtyKeys.length ? `${dirtyKeys.length} unsaved change${dirtyKeys.length === 1 ? '' : 's'}` : 'No changes'}
        </span>
        <span className="spacer" />
        <button className="btn btn-ghost" disabled={!dirtyKeys.length} onClick={() => setDraft(server.shownValues)}>Revert</button>
        <button
          className="btn btn-primary"
          disabled={!dirtyKeys.length || phase === 'saving'}
          onClick={() => setPhase('confirming')}
        >
          {phase === 'saving' ? 'Saving…' : 'Save changes'}
        </button>
      </div>

      {phase === 'confirming' && (
        <div className="modal-backdrop" onClick={e => { if (e.target === e.currentTarget) setPhase('idle'); }}>
          <div className="modal">
            <h3>Apply these settings?</h3>
            <div className="modal-diff">
              {dirtyKeys.map(k => (
                <div key={k}>
                  {server.meta[k].label}:{' '}
                  <span className="from">{String(server.shownValues[k])}</span>{' → '}
                  <span className="to">{String(draft[k])}</span>
                </div>
              ))}
            </div>
            <div className="modal-body">
              Changes apply immediately — the very next entry or exit decision uses the
              new values. No restart, no downtime.
              {ctx.open_positions > 0 && ` Note: ${ctx.open_positions} open position(s) are
              affected right away — a tighter stop loss or take profit acts on them at the
              next monitor cycle (within ~5 minutes).`}
            </div>
            <div className="modal-actions">
              <button className="btn btn-ghost" onClick={() => setPhase('idle')}>Cancel</button>
              <button className="btn btn-primary" onClick={save}>Apply now</button>
            </div>
          </div>
        </div>
      )}
    </div>
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
    // Immediate refetch when the archive/live toggle flips — waiting up to 30s
    // for the next poll would make the switch feel broken.
    window.addEventListener('stormedge-refetch', load);
    return () => { clearInterval(iv); window.removeEventListener('stormedge-refetch', load); };
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
      {M.portfolio.archive_view && (
        <div className="archive-banner">
          ◈ SAVED PAPER-ERA STATE — frozen snapshot, nothing here is running.
          Click the mode pill to return to live.
        </div>
      )}
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
        </>
      )}
      {activeTab === 'archive' && (
        <div>
          <RecentTrades trades={M.trades} />
        </div>
      )}
      {activeTab === 'settings' && <SettingsPanel portfolio={M.portfolio} />}
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
