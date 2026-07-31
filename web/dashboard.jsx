// stormedge — dashboard React app
// Fetches live data from /api/data and refreshes every 30s.

const { useState, useEffect, useRef } = React;

// ---------- helpers ----------
const fmtUSD = (n, signed = false) => {
  const v = Number(n) || 0;
  const sign = signed && v > 0 ? '+' : v < 0 ? '-' : '';
  return sign + '$' + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};
const fmtPct = (n, digits = 1) => ((Number(n) || 0) * 100).toFixed(digits) + '%';
const fmtPctSigned = (n, digits = 1) => (n > 0 ? '+' : '') + ((Number(n) || 0) * 100).toFixed(digits) + '%';
const fmtAgo = (d) => {
  const now = window.MOCK ? window.MOCK.now : new Date();
  const s = (now - d) / 1000;
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
  if (d > 0) return `${d}d ${pad(h)}:${pad(m)}`;
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
};

// Read a response body as JSON without assuming it IS JSON. A Flask 500, a
// proxy 502, or a login redirect all return an HTML page, and calling
// r.json() on one surfaces as `Unexpected token '<'` — which says nothing
// about what broke. Report the status instead, and keep the body for the log.
async function readJSON(r) {
  const text = await r.text();
  try {
    return JSON.parse(text);
  } catch (e) {
    console.error('Non-JSON response', r.status, r.url, text.slice(0, 500));
    const err = new Error(r.ok
      ? 'The server sent a non-JSON response'
      : `Server error (HTTP ${r.status}) — check the bot log`);
    err.status = r.status;
    throw err;
  }
}

// Mobile breakpoint — the design switches layout below 860px wide, and the CSS
// media queries in dashboard.html use the same cutover.
const MOBILE_BP = 860;
function useIsMobile() {
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== 'undefined' && window.innerWidth < MOBILE_BP
  );
  useEffect(() => {
    const mq = window.matchMedia(`(max-width: ${MOBILE_BP - 0.02}px)`);
    const on = e => setIsMobile(e.matches);
    setIsMobile(mq.matches);
    // Safari <14 only has the deprecated listener API
    mq.addEventListener ? mq.addEventListener('change', on) : mq.addListener(on);
    return () => {
      mq.removeEventListener ? mq.removeEventListener('change', on) : mq.removeListener(on);
    };
  }, []);
  return isMobile;
}

const TABS = [
  ['desk', 'Desk'],
  ['archive', 'Archive'],
  ['models', 'Signals'],
  ['settings', 'Settings'],
];

// ---------- brand mark ----------
// Cyclone eye: concentric arcs thinning outward around a lit centre.
function Logo({ size = 28 }) {
  return (
    <svg className="mark" width={size} height={size} viewBox="0 0 32 32" fill="none" aria-hidden="true">
      <defs>
        <radialGradient id="eyeH" cx="50%" cy="50%" r="50%">
          <stop offset="0" stopColor="#ffd694" />
          <stop offset="1" stopColor="#e39c33" />
        </radialGradient>
      </defs>
      <circle cx="16" cy="16" r="15" stroke="rgba(245,177,60,0.22)" strokeWidth="1" />
      <path d="M16 3.4c7 0 12.6 5.6 12.6 12.6S23 28.6 16 28.6" stroke="rgba(245,177,60,0.42)" strokeWidth="1.6" strokeLinecap="round" />
      <path d="M16 28.6c-7 0-12.6-5.6-12.6-12.6" stroke="rgba(245,177,60,0.18)" strokeWidth="1.6" strokeLinecap="round" />
      <path d="M16 8.2c4.3 0 7.8 3.5 7.8 7.8s-3.5 7.8-7.8 7.8" stroke="rgba(245,177,60,0.72)" strokeWidth="1.8" strokeLinecap="round" />
      <path d="M16 23.8c-4.3 0-7.8-3.5-7.8-7.8" stroke="rgba(245,177,60,0.3)" strokeWidth="1.8" strokeLinecap="round" />
      <circle cx="16" cy="16" r="3.4" fill="url(#eyeH)" />
    </svg>
  );
}

// ---------- NotificationBell ----------
// Self-contained: fetches /api/notifications on its own 30s cycle, independent
// of the main /api/data loop. Badge counts unread errors; click opens the list.
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
        const d = await readJSON(r);
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

  return (
    <div className="notif" ref={ref}>
      <button className="icon-btn" title="Alerts" aria-label="Alerts" onClick={() => setOpen(o => !o)}>
        <span className="glyph">!</span>
        {errorCount > 0 && <span className="notif-badge">{errorCount > 99 ? '99+' : errorCount}</span>}
      </button>
      {open && (
        <div className="notif-popup">
          <div className="notif-popup-head">
            <span>Alerts</span>
            <span>{items.length}</span>
          </div>
          <div className="notif-list">
            {items.length === 0 && <div className="notif-empty">Nothing to report</div>}
            {items.map(n => (
              <div key={n.id} className={`notif-item notif-${n.severity || 'info'}`}>
                <span className="notif-dot" />
                <div>
                  <div className="notif-item-msg">{n.message}</div>
                  <div className="notif-item-meta">
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

// ---------- Trading-mode dialog ----------
// The one control in the app that starts real money moving, so it never fires
// from a single click: opening it runs the readiness preflight, and Go live
// stays disabled until every blocking check passes.
function TradingModeDialog({ portfolio, onClose }) {
  const [pre, setPre] = useState(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState(null);
  const live = portfolio.mode === 'LIVE';

  useEffect(() => {
    (async () => {
      try {
        const r = await fetch('/api/live-preflight');
        setPre(await readJSON(r));
      } catch (e) { setPre({ ok: false, checks: [], error: e.message }); }
    })();
  }, []);

  const switchTo = async (paper) => {
    setBusy(true); setNote(null);
    try {
      const r = await fetch('/api/trading-mode', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paper, confirm: true }),
      });
      const d = await readJSON(r);
      if (!r.ok) {
        setNote({ err: true, msg: d.error, blocked: d.blocked });
      } else {
        setNote({ msg: d.warning ? `${d.message} ${d.warning}` : d.message, err: !!d.warning });
        window.dispatchEvent(new Event('stormedge-refetch'));
      }
    } catch (e) { setNote({ err: true, msg: e.message }); }
    setBusy(false);
  };

  return (
    <div className="modal-backdrop" onClick={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal">
        <h3>Trading mode</h3>
        <div className="mode-now">
          Right now the bot is trading in <b className={live ? 'neg' : ''}>{live ? 'LIVE' : 'PAPER'}</b> mode
          {live ? ' — orders are real and cost real money.' : ' — every fill is simulated.'}
        </div>

        {!live && (
          <>
            <div className="preflight-title">Before going live</div>
            {!pre && <div className="dim small">Checking credentials and funding…</div>}
            {pre && pre.checks.map(c => (
              <div key={c.id} className={`preflight-row ${c.ok ? 'ok' : (c.blocking ? 'bad' : 'warn')}`}>
                <span className="glyph">{c.ok ? '✓' : (c.blocking ? '✕' : '!')}</span>
                <div>
                  <div className="preflight-label">{c.label}</div>
                  <div className="preflight-detail">{c.detail}</div>
                </div>
              </div>
            ))}
            {pre && pre.error && <div className="preflight-detail">{pre.error}</div>}
          </>
        )}

        {note && (
          <div className={`settings-note ${note.err ? 'err' : ''}`}>
            <div>
              {note.msg}
              {note.blocked && note.blocked.map((b, i) => (
                <div key={i} className="small">· {b.label}: {b.detail}</div>
              ))}
            </div>
          </div>
        )}


        <div className="modal-actions">
          <button className="btn-undo" onClick={onClose}>Close</button>
          {live ? (
            <button className="btn-save armed" disabled={busy} onClick={() => switchTo(true)}>
              {busy ? 'Switching…' : 'Back to paper'}
            </button>
          ) : (
            <button
              className={`btn-save ${pre && pre.ok ? 'armed danger' : ''}`}
              disabled={busy || !pre || !pre.ok}
              title={pre && !pre.ok ? 'Every check above must pass first' : undefined}
              onClick={() => switchTo(false)}
            >
              {busy ? 'Switching…' : 'Go live with real money'}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------- TopBar ----------
function TopBar({ portfolio, scanLog, activeTab, setActiveTab }) {
  const [, setTick] = useState(0);
  const [modeOpen, setModeOpen] = useState(false);
  useEffect(() => {
    const i = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(i);
  }, []);
  const live = portfolio.mode === 'LIVE';

  return (
    <header className="topbar">
      <div className="brand">
        <Logo size={28} />
        <span className="brand-word">stormedge<em>.</em></span>
      </div>

      <div className="top-nav">
        {TABS.map(([id, label]) => (
          <button
            key={id}
            className={`nav-item ${activeTab === id ? 'active' : ''}`}
            onClick={() => setActiveTab(id)}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="spacer" />

      <button
        className={`mode-pill mode-toggleable ${live ? 'mode-live' : ''}`}
        title={`Trading in ${portfolio.mode} mode — click to change`}
        onClick={() => setModeOpen(true)}
      >
        <span className="mode-dot" />
        {portfolio.mode}
      </button>
      {modeOpen && (
        <TradingModeDialog portfolio={portfolio} onClose={() => setModeOpen(false)} />
      )}

      <div className="last-scan">
        <span>SCANNED</span>
        <b>{fmtAgo(scanLog.last_scan_at)} ago</b>
        <span className="scan-pulse" />
      </div>

      <NotificationBell />
      <a href="/api/logout" className="icon-btn" title="Sign out">↩</a>
    </header>
  );
}

// ---------- KPI strip ----------
// Equity sparkline — 12 bars from the recent equity curve when the API
// supplies one, otherwise nothing rather than a fake flat rail.
function Sparkline({ series }) {
  const pts = Array.isArray(series) && series.length >= 2 ? series.slice(-12) : null;
  if (!pts) return null;
  const lo = Math.min(...pts), hi = Math.max(...pts);
  const span = hi - lo || 1;
  return (
    <div className="spark" aria-hidden="true">
      {pts.map((v, i) => (
        <span key={i} style={{ height: (6 + ((v - lo) / span) * 20).toFixed(1) + 'px' }} />
      ))}
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
  const cap = portfolio.max_total_exposure_fraction ?? 0.70;
  // The exposure rail reads as "how much of the cap is used", so a 35%
  // exposure against a 70% cap fills the bar halfway — not to 35%.
  const capUsed = cap ? Math.min(1, portfolio.exposure_pct / cap) : 0;
  const halted = portfolio.circuit_tripped;

  return (
    <section className="header-strip">
      <div className="kpi kpi-hero">
        <div className="kpi-label">Total equity</div>
        <div className="kpi-hero-row">
          <div className="kpi-value">{fmtUSD(portfolio.total_equity)}</div>
          <Sparkline series={portfolio.equity_series} />
        </div>
        <div className={`kpi-sub ${equityChange >= 0 ? 'pos' : 'neg'}`}>
          {fmtUSD(equityChange, true)} <span className="dim">since first deposit</span>
        </div>
      </div>

      <div className="kpi">
        <div className="kpi-label">Cash free to trade</div>
        <div className="kpi-value">{fmtUSD(portfolio.available_cash)}</div>
        <div className="kpi-sub">
          {portfolio.total_equity ? fmtPct(portfolio.available_cash / portfolio.total_equity) : '—'} of equity
        </div>
      </div>

      <div className="kpi">
        <div className="kpi-label">Tied up in trades</div>
        <div className="kpi-value">{fmtUSD(portfolio.locked_cash)}</div>
        <div className="kpi-rail"><i style={{ width: (capUsed * 100).toFixed(0) + '%' }} /></div>
        <div className="kpi-sub">{fmtPct(portfolio.exposure_pct)} used of {fmtPct(cap, 0)} cap</div>
      </div>

      <div className="kpi kpi-today">
        <div className="kpi-label">Today</div>
        <div className={`kpi-value ${portfolio.daily_pnl < 0 ? 'neg' : 'pos'}`}>
          {fmtUSD(portfolio.daily_pnl, true)}
        </div>
        <div className="kpi-rail neg">
          <i style={{ width: (Math.max(0, Math.min(1, portfolio.circuit_breaker_used)) * 100).toFixed(0) + '%' }} />
        </div>
        <div className="kpi-sub">
          {halted
            ? <span className="neg">trading halted for today</span>
            : <>stops trading at {fmtUSD(-Math.abs(portfolio.daily_loss_limit))}</>}
        </div>
      </div>
    </section>
  );
}

// ---------- GlobePanel ----------
function GlobePanel({ cities, cityActivity, positions }) {
  const isMobile = useIsMobile();
  const wrapRef = useRef(null);
  const [hover, setHover] = useState(null);
  const [hoverPos, setHoverPos] = useState({ x: 0, y: 0 });

  useEffect(() => {
    // Mobile hides the canvas via CSS; skip the globe entirely so its
    // requestAnimationFrame loop isn't burning battery behind display:none.
    if (isMobile || !wrapRef.current || !window.StormGlobe) return;
    const g = new window.StormGlobe(wrapRef.current, {
      cities,
      cityActivity,
      onCityHover: (c, m) => {
        setHover(c);
        if (m) setHoverPos({ x: m.x, y: m.y });
      },
      onCityClick: () => {},
    });
    g.start();
    return () => g.stop();
  }, [isMobile]);

  const watched = cities.filter(c => (cityActivity[c.key] || cityActivity[c.name]));
  const hoverActivity = hover && (cityActivity[hover.key] || cityActivity[hover.name]);

  const metaFor = (act) => {
    if (act.state === 'active' && act.position) {
      return `${act.position.side} ${fmtUSD(act.position.size_usdc)}`;
    }
    if (act.state === 'signal') return 'signal';
    return (act.skip && act.skip.bucket) || 'no edge';
  };

  return (
    <section className="pane globe-pane">
      <header className="pane-head">
        <div>
          <h2>Where the bot is watching</h2>
          <p className="card-sub">
            {cities.length} airports · {positions.length} holding a trade · {watched.length} checked this cycle
          </p>
        </div>
        <div className="globe-legend">
          <span className="lg lg-active"><i />open trade</span>
          <span className="lg lg-signal"><i />signal, skipped</span>
          <span className="lg lg-scanned"><i />scanned, nothing</span>
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
        <ul className="city-list">
          {watched.map(c => {
            const act = cityActivity[c.key] || cityActivity[c.name];
            return (
              <li key={c.key} className={`city-row state-${act.state}`}>
                <span className={`state-dot dot-${act.state}`} />
                <span className="city-name">{c.name}</span>
                <span className="city-meta">{metaFor(act)}</span>
              </li>
            );
          })}
        </ul>
      </div>
    </section>
  );
}

// ---------- OpenPositions ----------
function OpenPositions({ positions, maxPositions, readOnly }) {
  const cap = maxPositions || 4;
  const [, setTick] = useState(0);
  // Two-step close: the first click arms the row, the second actually sells.
  // Selling is irreversible, so a single misclick must never fire an order.
  const [armed, setArmed] = useState(null);   // position id awaiting confirmation
  const [busyId, setBusyId] = useState(null);
  const [note, setNote] = useState(null);     // {err, msg}
  useEffect(() => {
    const i = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(i);
  }, []);

  const closePosition = async (p) => {
    setBusyId(p.id); setNote(null);
    try {
      const r = await fetch('/api/close-position', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ position_id: p.id, confirm: true }),
      });
      const d = await readJSON(r);
      // ok=false with a real message (no_fill, busy) is an outcome, not a crash —
      // show the server's wording rather than a generic failure.
      if (!r.ok && !d.message) setNote({ err: true, msg: d.error || 'Close failed' });
      else setNote({ err: !d.ok, msg: d.message });
      if (d.ok) window.dispatchEvent(new Event('stormedge-refetch'));
    } catch (e) { setNote({ err: true, msg: e.message }); }
    setBusyId(null); setArmed(null);
  };

  // Soonest resolution across open positions — drives the header sub-line.
  const nextResolve = (() => {
    const stamps = positions
      .map(p => p.resolves_at && new Date(p.resolves_at))
      .filter(d => d && !isNaN(d));
    if (!stamps.length) return null;
    return fmtCountdown(new Date(Math.min(...stamps.map(d => d.getTime()))));
  })();

  return (
    <section className="pane positions-pane">
      <header className="pane-head center">
        <div>
          <h2>Trades open now</h2>
          <p className="card-sub">
            {nextResolve
              ? <>next one settles in <span className="mono">{nextResolve}</span></>
              : 'nothing open right now'}
          </p>
        </div>
        <span className="pill">{positions.length} / {cap}</span>
      </header>
      {positions.length === 0 ? (
        <div className="empty-note">Nothing open right now.</div>
      ) : (
        <div className="pane-body">
          {positions.map(p => {
            const live = p.price_status === 'live';
            const pnl = (p.current_price - p.entry_price) / p.entry_price * p.size_usdc;
            const pnlPct = p.size_usdc > 0 ? pnl / p.size_usdc : 0;
            const tone = live ? (pnl >= 0 ? 'pos' : 'neg') : 'dim';
            return (
              <div className="tcard" key={p.id}>
                <div className="tcard-top">
                  <span className={`side-tag side-${p.side.toLowerCase()}`}>{p.side}</span>
                  <span className="tcard-city">{p.city}</span>
                  <span className={`tcard-pnl ${tone}`}>{live ? fmtUSD(pnl, true) : '—'}</span>
                </div>
                <div className="tcard-q">{p.question}</div>
                <div className="tcard-meta">
                  <span>
                    {p.entry_price.toFixed(2)} →{' '}
                    <b>
                      {p.current_price != null && p.price_status !== 'unavailable'
                        ? p.current_price.toFixed(2) : '—'}
                    </b>
                  </span>
                  <span>{fmtUSD(p.size_usdc)}</span>
                  <span className="spacer" />
                  <span className={tone}>{live ? fmtPctSigned(pnlPct) : ''}</span>
                  <span className="chip">{fmtCountdown(p.resolves_at) || '—'}</span>
                </div>
                {/* A stale or missing price must never look like a real P&L of $0. */}
                {!live && (
                  <div className="tcard-meta">
                    <span>{p.price_status === 'unavailable' ? 'Price unavailable' : 'Pending resolution'}</span>
                  </div>
                )}
                {!readOnly && (
                  <div className="tcard-foot">
                    {armed === p.id ? (
                      <>
                        <button className="btn-close-go" disabled={busyId === p.id} onClick={() => closePosition(p)}>
                          {busyId === p.id ? 'Selling…' : 'Confirm sell'}
                        </button>
                        <button className="btn-close-cancel" disabled={busyId === p.id} onClick={() => setArmed(null)}>
                          Cancel
                        </button>
                      </>
                    ) : (
                      <button
                        className="btn-close"
                        disabled={busyId != null}
                        title={live && p.current_price != null
                          ? `Sell at the current bid (~${p.current_price.toFixed(2)})`
                          : 'No live price — the sell may not fill'}
                        onClick={() => { setNote(null); setArmed(p.id); }}
                      >
                        Close
                      </button>
                    )}
                  </div>
                )}
              </div>
            );
          })}
          {note && <div className={`settings-note ${note.err ? 'err' : ''}`}>{note.msg}</div>}
        </div>
      )}
    </section>
  );
}

// ---------- PerformanceStats ----------
const PERF_PERIODS = ['30d', '6m', '1y'];

function PerformanceStats({ stats }) {
  const [period, setPeriod] = useState('30d');
  // Support both the new nested shape {30d:{…},6m:{…},1y:{…}} and the old flat shape
  const isNested = stats && typeof stats['30d'] === 'object';
  const s = isNested ? (stats[period] || stats['30d']) : (stats || {});

  const items = [
    { k: 'Hit rate',  v: fmtPct(s.win_rate),           s: `${s.total_trades} settled` },
    { k: 'Cash made', v: fmtUSD(s.realized_pnl, true), s: `in ${period}`, tone: s.realized_pnl >= 0 ? 'pos' : 'neg' },
    { k: 'Avg edge',  v: fmtPct(s.avg_edge),           s: 'floor 8.0%', color: 'var(--signal-hi)' },
    { k: 'Avg hold',  v: fmtHold(s.avg_hold_hours),    s: 'entry to exit' },
    { k: 'Best',      v: fmtUSD(s.best_trade, true),   s: 'one trade', tone: 'pos' },
    { k: 'Worst',     v: fmtUSD(s.worst_trade, true),  s: 'one trade', tone: s.worst_trade >= 0 ? 'pos' : 'neg' },
  ];

  return (
    <section className="perf-card">
      <div className="perf-head">
        <div className="perf-title">
          How it has been doing <span className="dim">· settled trades only</span>
        </div>
        <div className="seg">
          {PERF_PERIODS.map(p => (
            <button key={p} className={p === period ? 'on' : ''} onClick={() => setPeriod(p)}>{p}</button>
          ))}
        </div>
      </div>
      <div className="perf-grid">
        {items.map(it => (
          <div key={it.k} className="perf-tile">
            <div className="k">{it.k}</div>
            <div className={`v ${it.tone || ''}`} style={it.color ? { color: it.color } : undefined}>{it.v}</div>
            <div className="s">{it.s}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

// ---------- Pagination ----------
function Pagination({ page, total, onChange }) {
  if (total <= 1) return null;
  return (
    <div className="pagination">
      <button className="pg-btn" onClick={() => onChange(page - 1)} disabled={page === 0}>‹</button>
      <span className="pg-info">{page + 1} / {total}</span>
      <button className="pg-btn" onClick={() => onChange(page + 1)} disabled={page === total - 1}>›</button>
    </div>
  );
}

// ---------- RecentTrades (Archive) ----------
// The executor writes machine-readable exit reasons ("RESOLVED_WIN (Yes)",
// "Stop Loss (-50.0%)"). The archive states what happened in plain words and
// keeps the raw string on the element's title so nothing is lost.
const exitReason = (raw) => {
  const r = String(raw || '');
  if (/^RESOLVED_WIN/i.test(r))     return { label: 'Won the market', tone: 'won' };
  if (/^RESOLVED_LOSS/i.test(r))    return { label: 'Lost the market', tone: 'lost' };
  if (/^RESOLVED_UNKNOWN/i.test(r)) return { label: 'Settled, outcome unclear', tone: '' };
  if (/take profit/i.test(r))       return { label: 'Hit the target', tone: 'won' };
  if (/stop loss|sustained loss/i.test(r)) return { label: 'Cut early', tone: 'cut' };
  if (/edge decayed/i.test(r))      return { label: 'Edge faded', tone: '' };
  if (/^EXTERNAL_CLOSE/i.test(r))   return { label: 'Sold on Polymarket', tone: '' };
  if (/^MANUAL_CLOSE/i.test(r))     return { label: 'Closed by hand', tone: '' };
  if (/^EXPIRED_ON_RESTART/i.test(r)) return { label: 'Expired', tone: '' };
  return { label: r || 'Unknown', tone: '' };
};

const TRADE_FILTERS = [
  ['all', 'All'],
  ['wins', 'Wins'],
  ['losses', 'Losses'],
  ['cut', 'Cut early'],
];
const TRADES_PAGE_SIZE = 40;

function RecentTrades({ trades }) {
  const [page, setPage] = useState(0);
  const [filter, setFilter] = useState('all');
  const isMobile = useIsMobile();

  const shown = trades.filter(t =>
    filter === 'all' ? true
      : filter === 'wins' ? t.pnl > 0
      : filter === 'losses' ? t.pnl < 0
      : exitReason(t.exit_reason).tone === 'cut');
  const net = shown.reduce((a, t) => a + t.pnl, 0);

  const totalPages = Math.max(1, Math.ceil(shown.length / TRADES_PAGE_SIZE));
  const safePage = Math.min(page, totalPages - 1);
  const slice = shown.slice(safePage * TRADES_PAGE_SIZE, (safePage + 1) * TRADES_PAGE_SIZE);

  return (
    <section className="pane list-pane">
      <header className="pane-head wrap">
        <div>
          <h2>Finished trades</h2>
          <p className="card-sub">
            {shown.length} settled · newest first · net{' '}
            <span className={net >= 0 ? 'pos' : 'neg'}>{fmtUSD(net, true)}</span>
          </p>
        </div>
        <div className="head-controls">
          <div className="seg text">
            {TRADE_FILTERS.map(([id, label]) => (
              <button
                key={id}
                className={filter === id ? 'on' : ''}
                onClick={() => { setFilter(id); setPage(0); }}
              >
                {label}
              </button>
            ))}
          </div>
          {/* Live history is unbounded, so the list pages rather than growing
              without limit — the design's single scroll pane assumes 14 rows. */}
          <Pagination page={safePage} total={totalPages} onChange={setPage} />
        </div>
      </header>

      {!isMobile && (
        <div className="arch-head arch-cols">
          <div>City / market</div>
          <div>Bet</div>
          <div className="r">In → out</div>
          <div className="r">Size</div>
          <div className="r">Result</div>
          <div>Why it closed</div>
          <div className="r">Ago</div>
        </div>
      )}

      {shown.length === 0 ? (
        <div className="empty-note">No trades match this filter.</div>
      ) : (
        <div className="list-body">
          {slice.map(t => {
            const closedAt = t.closed_at instanceof Date ? t.closed_at : new Date(t.closed_at);
            const reason = exitReason(t.exit_reason);
            const tone = t.pnl >= 0 ? 'pos' : 'neg';
            return isMobile ? (
              <div className="tcard" key={t.id}>
                <div className="tcard-top">
                  <span className={`side-tag side-${t.side.toLowerCase()}`}>{t.side}</span>
                  <span className="tcard-city">{t.city}</span>
                  <span className={`tcard-pnl ${tone}`}>{fmtUSD(t.pnl, true)}</span>
                </div>
                <div className="tcard-q">{t.question}</div>
                <div className="tcard-meta">
                  <span>{t.entry_price.toFixed(2)} → {t.exit_price.toFixed(2)}</span>
                  <span>{fmtUSD(t.size_usdc)}</span>
                  <span className="spacer" />
                  <span>{fmtAgo(closedAt)} ago</span>
                </div>
                <div className="tcard-foot">
                  <span className={`reason-chip reason-${reason.tone}`} title={t.exit_reason}>{reason.label}</span>
                </div>
              </div>
            ) : (
              <div className="arch-row arch-cols" key={t.id}>
                <div className="cell-city">
                  <div className="city-line">{t.city}</div>
                  <div className="city-q">{t.question}</div>
                </div>
                <div><span className={`side-tag side-${t.side.toLowerCase()}`}>{t.side}</span></div>
                <div className="r arch-prices">{t.entry_price.toFixed(2)} → {t.exit_price.toFixed(2)}</div>
                <div className="r arch-size">{fmtUSD(t.size_usdc)}</div>
                <div className={`r arch-pnl ${tone}`}>
                  {fmtUSD(t.pnl, true)}
                  <div className="pct">
                    {t.pnl_pct != null ? (t.pnl_pct > 0 ? '+' : '') + t.pnl_pct.toFixed(1) + '%' : ''}
                  </div>
                </div>
                <div>
                  <span className={`reason-chip reason-${reason.tone}`} title={t.exit_reason}>{reason.label}</span>
                </div>
                <div className="r arch-ago">{fmtAgo(closedAt)}</div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

// ---------- ScanFeed (funnel strip) ----------
const FUNNEL_COLORS = { dim: '#4a4640', neutral: '#b4b0a6', signal: '#f5b13c', pos: '#6cbf85' };

function ScanFeed({ scanLog }) {
  const steps = [
    { label: 'Markets seen',      v: scanLog.markets_seen,  tone: 'dim' },
    { label: 'Worth a look',      v: scanLog.candidates,    tone: 'neutral' },
    { label: 'Passed the models', v: scanLog.shadow_passed, tone: 'signal' },
    { label: 'Actually bought',   v: scanLog.filled,        tone: 'pos' },
  ];
  const top = steps[0].v || 0;
  return (
    <section className="funnel-card">
      <div className="funnel-head">
        Last scan, step by step{' '}
        <span className="dim">
          · {fmtAgo(scanLog.last_scan_at)} ago · {(scanLog.duration_ms || 0).toLocaleString()}ms
        </span>
      </div>
      <div className="scan-funnel">
        {steps.map((s, i, arr) => {
          const prev = i > 0 ? arr[i - 1].v : null;
          const conv = prev && prev > 0 ? (s.v / prev) * 100 : null;
          const width = top > 0 ? Math.min(100, ((s.v || 0) / top) * 100) : 0;
          return (
            <div className="funnel-step" key={s.label}>
              <div className="k">{s.label}</div>
              <div className="v" style={{ color: FUNNEL_COLORS[s.tone] }}>{(s.v || 0).toLocaleString()}</div>
              <div className="funnel-rail">
                <i style={{ width: width.toFixed(2) + '%', background: FUNNEL_COLORS[s.tone] }} />
              </div>
              <div className="c">{conv !== null ? `${conv.toFixed(1)}% of previous` : 'start'}</div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

// ---------- RecentSignals ----------
// Fixed draw order so the breakdown bar and legend read left-to-right by how
// far a market got, rather than shuffling with whatever the data contains.
const GATE_COLOR = {
  'Taken':                    '#6cbf85',
  'Models disagreed':         '#f5b13c',
  'Model spread too wide':    '#e39c33',
  'Too close to bucket edge': '#c1913f',
  'Market spread too wide':   '#8a7a55',
  'Direction mismatch':       '#6b6455',
  'YES disabled':             '#4a5560',
  'Edge below threshold':     '#3d4550',
  'Other skip':               '#2f353d',
};
const GATE_ORDER = Object.keys(GATE_COLOR);

function SignalDetail({ s }) {
  const models = Object.entries(s.raw_models || {});
  const bucketLabel = s.bucket_low != null && s.bucket_high != null
    ? `${s.bucket_low.toFixed(1)}–${s.bucket_high.toFixed(1)}°F`
    : s.bucket_low != null ? `> ${s.bucket_low.toFixed(1)}°F`
    : s.bucket_high != null ? `< ${s.bucket_high.toFixed(1)}°F`
    : '—';
  const market = [
    ['Bucket', bucketLabel],
    ['Model probability', s.model_prob != null ? fmtPct(s.model_prob, 0) : '—'],
    ['YES price', s.yes_price != null ? '$' + s.yes_price.toFixed(3) : '—'],
    ['NO price', s.no_price != null ? '$' + s.no_price.toFixed(3) : '—'],
    ['Model spread', s.model_spread != null ? s.model_spread.toFixed(2) + '°F'
      : s.ensemble_std != null ? s.ensemble_std.toFixed(2) + '°F' : '—'],
    ['Market spread', s.market_spread_frac != null ? fmtPct(s.market_spread_frac) : '—'],
    ['Agreement', s.agreement != null ? fmtPct(s.agreement, 0) : '—'],
    ['Mean gap', s.mean_gap != null ? s.mean_gap.toFixed(1) + '°F' : '—'],
  ];
  return (
    <div className="sig-detail">
      <div className="detail-block">
        <h4>Forecast models</h4>
        {models.length === 0 && <div className="kv"><span>no data</span></div>}
        {models.map(([name, temp]) => (
          <div className="kv" key={name}>
            <span>{name}</span>
            <span>{typeof temp === 'number' ? temp.toFixed(1) + '°' : '—'}</span>
          </div>
        ))}
      </div>
      <div className="detail-block">
        <h4>Market at that moment</h4>
        {market.map(([k, v]) => (
          <div className="kv" key={k}><span>{k}</span><span>{v}</span></div>
        ))}
      </div>
      <div className="detail-block">
        <h4>{s.gate_outcome === 'Taken' ? 'Why it bought' : 'Why it passed'}</h4>
        <div className="reason-full">{s.reason}</div>
      </div>
    </div>
  );
}

function RecentSignals({ signals }) {
  const rows = signals || [];
  const [filter, setFilter] = useState(null);
  const [search, setSearch] = useState('');
  const [expanded, setExpanded] = useState(null);

  const counts = {};
  for (const r of rows) counts[r.gate_outcome] = (counts[r.gate_outcome] || 0) + 1;
  const present = GATE_ORDER.filter(k => counts[k])
    .concat(Object.keys(counts).filter(k => !GATE_COLOR[k]));   // unknown gates last

  const filtered = rows.filter(s => {
    if (filter && s.gate_outcome !== filter) return false;
    if (search) {
      const q = search.toLowerCase();
      if (!(s.city || '').toLowerCase().includes(q) && !(s.market_id || '').toLowerCase().includes(q)) return false;
    }
    return true;
  });
  const maxEdge = Math.max(...rows.map(s => Math.abs(s.edge || 0)), 0.01);
  const total = rows.length || 1;
  const toggleGate = (k) => setFilter(f => (f === k ? null : k));

  return (
    <section className="pane list-pane">
      <header className="pane-head block">
        <div className="sig-head-row">
          <div>
            <h2>Every market it looked at</h2>
            <p className="card-sub">
              {filtered.length} of {rows.length} shown · tap a row for the numbers behind it
            </p>
          </div>
          <input
            type="search"
            className="signals-search"
            placeholder="Filter by city…"
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
        </div>
        <div className="gate-bar">
          {present.map(k => (
            <div
              key={k}
              className="gate-seg"
              title={`${k}: ${counts[k]}`}
              onClick={() => toggleGate(k)}
              style={{
                width: ((counts[k] / total) * 100).toFixed(1) + '%',
                background: GATE_COLOR[k] || '#3d4550',
                opacity: !filter || filter === k ? 1 : 0.28,
              }}
            />
          ))}
        </div>
        <div className="gate-legend">
          {present.map(k => (
            <span
              key={k}
              onClick={() => toggleGate(k)}
              style={{ color: !filter || filter === k ? 'var(--text-soft)' : 'var(--dim)' }}
            >
              <i style={{ background: GATE_COLOR[k] || '#3d4550' }} />
              {k} <span className="n">{counts[k]}</span>
            </span>
          ))}
        </div>
      </header>

      {filtered.length === 0 ? (
        <div className="empty-note">No signals match.</div>
      ) : (
        <div className="list-body">
          {filtered.map((s, i) => {
            const key = s.ts + '_' + s.market_id + '_' + i;
            const open = expanded === key;
            const taken = s.gate_outcome === 'Taken';
            const barPct = Math.min(100, (Math.abs(s.edge || 0) / maxEdge) * 100);
            const edgeColor = taken ? 'var(--positive)' : 'var(--signal-deep)';
            return (
              <div className={`sig-row ${open ? 'open' : ''}`} key={key}>
                <div className="sig-top" onClick={() => setExpanded(open ? null : key)}>
                  <div>
                    <div className="sig-city">{s.city || '—'}</div>
                    <div className="sig-when">{s.target_date || '—'} · {fmtAgo(new Date(s.ts))} ago</div>
                  </div>
                  <div className="sig-edge">
                    <div className="sig-edge-track">
                      <i style={{ width: barPct.toFixed(0) + '%', background: edgeColor }} />
                    </div>
                    <span className="sig-edge-val" style={{ color: edgeColor }}>
                      {s.edge != null ? fmtPctSigned(s.edge) : '—'}
                    </span>
                  </div>
                  <div className="sig-gate">
                    <span className={`gate-pill ${taken ? 'taken' : ''}`}>{s.gate_outcome}</span>
                    <span className="sig-chev">{open ? '−' : '+'}</span>
                  </div>
                </div>
                {open && <SignalDetail s={s} />}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

// ---------- Settings ----------
// Percent-style knobs are stored as fractions but edited as whole percents —
// typing "70" is natural, 0.70 is not.
const PCT_FIELDS = { MAX_TOTAL_EXPOSURE_FRACTION: true, STOP_LOSS_PCT: true };

// Stepper — −/+ around a typed value. `dp` decimals keeps the money fields at
// cents and the whole-dollar fields clean; clamping happens on commit so an
// in-progress edit ("" or "1.") isn't fought by the input as it's typed.
function Stepper({ label, value, onChange, prefix, step = 1, dp = 2,
                   min = 0, max = Infinity, error, wide }) {
  const clamp = n => Math.min(max, Math.max(min, n));
  const bump = d => {
    const base = Number(value);
    onChange(clamp(+((isNaN(base) ? min : base) + d).toFixed(dp)).toFixed(dp));
  };
  const commit = () => {
    const n = Number(value);
    onChange((isNaN(n) ? min : clamp(n)).toFixed(dp));
  };
  return (
    <div className={`stepper ${error ? 'has-error' : ''}`}>
      <button type="button" onClick={() => bump(-step)}
              disabled={Number(value) <= min} aria-label={`Decrease ${label}`}>−</button>
      <span className={`stepper-val ${wide ? 'wide' : ''}`}>
        {prefix && <span className="stepper-affix">{prefix}</span>}
        <input type="text" inputMode="decimal" value={value} aria-label={label}
               onChange={e => onChange(e.target.value)} onBlur={commit} />
      </span>
      <button type="button" onClick={() => bump(step)}
              disabled={Number(value) >= max} aria-label={`Increase ${label}`}>+</button>
    </div>
  );
}

// Slider block — the reading sits beside the label and the fill tracks the
// thumb, so the current value is legible without dragging.
function SliderField({ label, help, value, onChange, min, max, step = 1,
                       readout, unit, disabled, error }) {
  const span = max - min;
  const fill = span ? Math.min(100, Math.max(0, ((Number(value) - min) / span) * 100)) : 0;
  return (
    <div className={disabled ? 'dependent-off' : ''}>
      <div className="slider-head">
        <div className="field-label">{label}</div>
        <div className="slider-read">
          {readout}{unit && <span className="unit"> {unit}</span>}
        </div>
      </div>
      <div className="slider-help">{help}</div>
      <input
        type="range" className="range" min={min} max={max} step={step}
        value={value} disabled={disabled} aria-label={label}
        onChange={e => onChange(e.target.value)}
        style={{ background: `linear-gradient(90deg, #e39c33 ${fill}%, rgba(255,255,255,0.08) ${fill}%)` }}
      />
      {error && <div className="field-error">{error}</div>}
    </div>
  );
}

// The allowed concurrency values, offered as buttons. A typed number here would
// hide the fact that only a handful of settings make sense.
const POS_OPTIONS = [2, 3, 4, 6, 8];

// What the bot will ACTUALLY do with a given set of values. Recomputed on every
// keystroke — this is the "show me the value as I tweak it" part.
function deriveImpact(v, ctx) {
  const equity = ctx.total_equity || 0;
  // The stake IS the per-trade size: strategy.py has no second knob that can
  // reduce it, so what the user types is what every trade costs.
  const effective = Number(v.FIXED_POSITION_SIZE) || 0;
  const exposureCap = equity * (Number(v.MAX_TOTAL_EXPOSURE_FRACTION) || 0);
  const slotsByExposure = effective > 0 ? Math.floor(exposureCap / effective) : 0;
  const slotsByCash = effective > 0 ? Math.floor((ctx.available_cash || 0) / effective) : 0;
  const maxConc = Number(v.MAX_CONCURRENT_POSITIONS) || 0;
  const slots = Math.max(0, Math.min(maxConc, slotsByExposure, slotsByCash));
  // The daily loss limit is DERIVED: a budget of N full-stake losses, so the
  // dollar figure rescales live as the stake or the budget is edited.
  const lossStakes = Number(v.DAILY_LOSS_STAKES) || 0;
  return {
    effective,
    exposureCap, slots,
    // On a binary $0/$1 market the max loss per position IS the whole stake,
    // so this is a real worst case, not a scare number.
    worstCase: slots * effective,
    dailyLossDollars: effective * lossStakes,
    lossesToHalt: Math.ceil(lossStakes),
    stopLossPerTrade: v.ENABLE_STOP_LOSS ? effective * (Number(v.STOP_LOSS_PCT) || 0) : effective,
    // Upside if a trade runs from a typical entry to the take-profit price.
    // The stake buys stake/entry shares; selling them at tp returns
    // stake * (tp/entry), so the gain is stake * (tp/entry - 1). Measured
    // against the average fill the bot has actually paid — pricing it off the
    // take-profit price itself would imply buying at ~$0.98 and report a few
    // cents of upside against a full-stake downside.
    upside: (() => {
      const tp = Number(v.TAKE_PROFIT_PRICE);
      const entry = Number(ctx.avg_entry_price) || 0.45;
      return tp > 0 && entry > 0 ? Math.max(0, effective * (tp / entry - 1)) : 0;
    })(),
  };
}

function SettingsPanel() {
  const [server, setServer] = useState(null);
  const [draft, setDraft] = useState(null);
  const [phase, setPhase] = useState('idle');   // idle|confirming|saving
  const [fieldErrors, setFieldErrors] = useState({});
  const [banner, setBanner] = useState(null);
  const [saved, setSaved] = useState(false);

  const load = async () => {
    try {
      const r = await fetch('/api/settings');
      if (r.status === 401) { window.location.href = '/'; return; }
      const d = await readJSON(r);
      // Percent-style values are edited as whole numbers.
      const shown = { ...d.values };
      Object.keys(PCT_FIELDS).forEach(k => { if (shown[k] != null) shown[k] = +(shown[k] * 100).toFixed(4); });
      setServer({ ...d, shownValues: shown });
      setDraft(shown);
    } catch (e) { setBanner({ err: true, msg: 'Could not load settings: ' + e.message }); }
  };

  useEffect(() => { load(); }, []);

  if (!server || !draft) {
    return <div className="empty-note">Loading settings…</div>;
  }

  // Values in real units (fractions, not percents) for impact + save.
  const realValues = (() => {
    const out = { ...draft };
    Object.keys(PCT_FIELDS).forEach(k => { if (out[k] != null && out[k] !== '') out[k] = Number(out[k]) / 100; });
    return out;
  })();
  const ctx = server.context;
  const impact = deriveImpact(realValues, ctx);

  // Compare numerically where both sides are numbers: the steppers normalise to
  // two decimals, so a value nudged up and back down lands on "2.00" against a
  // server "2" and a pure string compare would report a change that isn't one.
  const sameValue = (a, b) => {
    if (a === b) return true;
    // Booleans stay an identity compare — Number(true) is 1, which would make
    // the stop-loss flag look equal to a numeric 1.
    if (typeof a === 'boolean' || typeof b === 'boolean') return false;
    const na = Number(a), nb = Number(b);
    if (a !== '' && b !== '' && a != null && b != null && !isNaN(na) && !isNaN(nb)) return na === nb;
    return String(a) === String(b);
  };
  const dirtyKeys = Object.keys(server.shownValues).filter(k => !sameValue(draft[k], server.shownValues[k]));
  const dirty = dirtyKeys.length;

  const set = (key, val) => {
    setDraft(d => {
      return { ...d, [key]: val };
    });
    setFieldErrors(fe => ({ ...fe, [key]: null }));
    setSaved(false);
  };

  const save = async () => {
    setPhase('saving'); setBanner(null); setFieldErrors({});
    const payload = {};
    dirtyKeys.forEach(k => { payload[k] = realValues[k]; });
    try {
      const r = await fetch('/api/settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings: payload }),
      });
      const d = await readJSON(r);
      setPhase('idle');
      if (!r.ok) {
        if (d.field_errors) setFieldErrors(d.field_errors);
        setBanner({ err: true, msg: d.error || 'Save failed' });
        return;
      }
      // Applied live: the runtime store is already swapped, the next bot
      // decision uses the new values. Just re-sync the panel and the desk.
      await load();
      setSaved(true);
      window.dispatchEvent(new Event('stormedge-refetch'));
      setBanner({ msg: d.changed && d.changed.length
        ? 'Applied immediately: ' + d.changed.join(', ')
        : (d.message || 'No changes.') });
    } catch (e) {
      setPhase('idle');
      setBanner({ err: true, msg: 'Save failed: ' + e.message });
    }
  };


  return (
    <>
      {banner && <div className={`settings-note ${banner.err ? 'err' : ''}`}>{banner.msg}</div>}

      <div className="settings-grid">
        {/* ---- sizing ---- */}
        <section className="set-card">
          <div className="set-title">
            <h2>How much to bet</h2>
            <span className="set-kicker">PER TRADE</span>
          </div>

          <div className="set-row pad-b">
            <div className="grow">
              <div className="field-label">Stake on every trade</div>
              <div className="field-help">Polymarket won't take less than {fmtUSD(ctx.min_position_size)}</div>
              {fieldErrors.FIXED_POSITION_SIZE && <div className="field-error">{fieldErrors.FIXED_POSITION_SIZE}</div>}
            </div>
            <Stepper
              label="Stake on every trade" prefix="$" step={1} dp={2}
              min={Math.max(ctx.min_position_size || 1, server.meta.FIXED_POSITION_SIZE.min)}
              max={server.meta.FIXED_POSITION_SIZE.max}
              value={draft.FIXED_POSITION_SIZE}
              onChange={v => set('FIXED_POSITION_SIZE', v)}
              error={fieldErrors.FIXED_POSITION_SIZE}
            />
          </div>

          <div className="set-fill" />

          {/* Headline consequence of the two knobs above: what one trade costs,
              and what the rails around it work out to in dollars. */}
          <div className="means-card">
            <div className="means-label">WHAT THAT MEANS</div>
            <div className="means-hero">
              <div className="v">{fmtUSD(impact.effective)}</div>
              <div className="s">
                {ctx.available_cash ? fmtPct(impact.effective / ctx.available_cash) : '—'} of {fmtUSD(ctx.available_cash)} per trade
              </div>
            </div>
            <div className="means-rail">
              <i style={{ width: (ctx.available_cash
                ? Math.min(100, (impact.effective / ctx.available_cash) * 100) : 0).toFixed(1) + '%' }} />
            </div>
            <div className="means-grid">
              <div><span className="k">trades at once</span><span className="v">{impact.slots}</span></div>
              <div><span className="k">most at risk</span><span className="v">{fmtUSD(impact.worstCase)}</span></div>
              <div>
                <span className="k">stops at</span>
                <span className={`v ${impact.lossesToHalt <= 2 ? 'warn' : ''}`}>{fmtUSD(impact.dailyLossDollars)}</span>
              </div>
              <div>
                <span className="k">after</span>
                <span className="v">{impact.lossesToHalt} loss{impact.lossesToHalt === 1 ? '' : 'es'}</span>
              </div>
            </div>
          </div>
        </section>

        {/* ---- risk ---- */}
        <section className="set-card">
          <div className="set-title">
            <h2>When to stop</h2>
            <span className="set-kicker">SAFETY RAILS</span>
          </div>

          <div className="set-row pad-b">
            <div className="grow">
              <div className="field-label">Trades open at once</div>
              <div className="field-help">No new entries past this many</div>
              {fieldErrors.MAX_CONCURRENT_POSITIONS && (
                <div className="field-error">{fieldErrors.MAX_CONCURRENT_POSITIONS}</div>
              )}
            </div>
            <div className="pos-options">
              {POS_OPTIONS.map(n => (
                <button
                  key={n} type="button"
                  className={Number(draft.MAX_CONCURRENT_POSITIONS) === n ? 'on' : ''}
                  onClick={() => set('MAX_CONCURRENT_POSITIONS', n)}
                  aria-pressed={Number(draft.MAX_CONCURRENT_POSITIONS) === n}
                >{n}</button>
              ))}
            </div>
          </div>

          <div className="set-div" />
          <div className="pad-y-lg">
            <SliderField
              label="Give up for the day after"
              help="Counted in full-stake losses"
              min={1} max={12} step={1}
              value={draft.DAILY_LOSS_STAKES}
              onChange={v => set('DAILY_LOSS_STAKES', v)}
              readout={draft.DAILY_LOSS_STAKES}
              unit={`losses · ${fmtUSD(impact.dailyLossDollars)}`}
              error={fieldErrors.DAILY_LOSS_STAKES}
            />
          </div>

          <div className="set-div" />
          <div className="pad-y-lg">
            <SliderField
              label="Cash tied up at most"
              help="Share of the bankroll allowed in open trades"
              min={10} max={100} step={5}
              value={draft.MAX_TOTAL_EXPOSURE_FRACTION}
              onChange={v => set('MAX_TOTAL_EXPOSURE_FRACTION', v)}
              readout={`${draft.MAX_TOTAL_EXPOSURE_FRACTION}%`}
              unit={`· ${fmtUSD(impact.exposureCap)}`}
              error={fieldErrors.MAX_TOTAL_EXPOSURE_FRACTION}
            />
          </div>

          <div className="set-fill" />
          <div className="note-row">
            <span className="dot" />
            <span>These rails re-scale on their own when you change the stake.</span>
          </div>
        </section>

        {/* ---- exits ---- */}
        <section className="set-card">
          <div className="set-title">
            <h2>When to get out</h2>
            <span className="set-kicker">EARLY EXITS</span>
          </div>

          <div className="set-row" style={{ paddingBottom: 14 }}>
            <div className="grow">
              <div className="field-label">Cut losing trades early</div>
              <div className="field-help">Sell when the price falls far enough</div>
            </div>
            <button
              type="button"
              className={`switch ${draft.ENABLE_STOP_LOSS ? 'on' : ''}`}
              onClick={() => set('ENABLE_STOP_LOSS', !draft.ENABLE_STOP_LOSS)}
              aria-pressed={!!draft.ENABLE_STOP_LOSS}
              aria-label="Cut losing trades early"
            >
              <span className="switch-label">{draft.ENABLE_STOP_LOSS ? 'ON' : 'OFF'}</span>
              <span className="switch-knob" />
            </button>
          </div>

          <div className="set-div" />
          <div className="pad-y-lg">
            <SliderField
              label="Cut it when it drops"
              help={draft.ENABLE_STOP_LOSS
                ? `Instead of riding ${fmtUSD(impact.effective)} to zero`
                : 'Stop loss is off — a losing position rides to settlement'}
              min={10} max={90} step={5}
              value={draft.STOP_LOSS_PCT}
              disabled={!draft.ENABLE_STOP_LOSS}
              onChange={v => set('STOP_LOSS_PCT', v)}
              readout={`${draft.STOP_LOSS_PCT}%`}
              unit={`· loses ${fmtUSD(impact.stopLossPerTrade)}`}
              error={fieldErrors.STOP_LOSS_PCT}
            />
          </div>

          <div className="set-div" />
          <div className="set-row pad-t">
            <div className="grow">
              <div className="field-label">Take the win at</div>
              <div className="field-help">Sell as soon as a real bid hits this</div>
              {fieldErrors.TAKE_PROFIT_PRICE && <div className="field-error">{fieldErrors.TAKE_PROFIT_PRICE}</div>}
            </div>
            <Stepper
              label="Take the win at" prefix="$" step={0.01} dp={2}
              min={0.01} max={0.99} wide
              value={draft.TAKE_PROFIT_PRICE}
              onChange={v => set('TAKE_PROFIT_PRICE', v)}
              error={fieldErrors.TAKE_PROFIT_PRICE}
            />
          </div>

          <div className="set-fill" />
          {/* Both tails of one trade, side by side — the pair is the point. */}
          <div className="outcome-pair">
            <div className="outcome-cell">
              <div className="k">IF IT GOES WRONG</div>
              <div className="v down">−{fmtUSD(impact.stopLossPerTrade)}</div>
              <div className="n">{draft.ENABLE_STOP_LOSS ? `stop at ${draft.STOP_LOSS_PCT}%` : 'rides to zero'}</div>
            </div>
            <div className="outcome-cell">
              <div className="k">IF IT GOES RIGHT</div>
              <div className="v up">+{fmtUSD(impact.upside)}</div>
              <div className="n">from a {fmtUSD(Number(ctx.avg_entry_price) || 0.45)} entry</div>
            </div>
          </div>
        </section>

      </div>

      <div className="save-bar">
        <span className="dot" style={{ background: dirty ? 'var(--signal)' : '#3f3d38' }} />
        <span className="label">
          {saved && !dirty ? 'Saved'
            : dirty ? `${dirty} change${dirty === 1 ? '' : 's'} not saved`
            : 'Nothing changed'}
        </span>
        <span className="spacer" />
        <button className="btn-undo" disabled={!dirty} onClick={() => { setDraft(server.shownValues); setSaved(false); }}>
          Undo
        </button>
        <button
          className={`btn-save ${dirty ? 'armed' : ''}`}
          disabled={!dirty || phase === 'saving'}
          onClick={() => setPhase('confirming')}
        >
          {phase === 'saving' ? 'Saving…' : dirty ? 'Save' : 'Saved'}
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
              <button className="btn-undo" onClick={() => setPhase('idle')}>Cancel</button>
              <button className="btn-save armed" onClick={save}>Apply now</button>
            </div>
          </div>
        </div>
      )}
    </>
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
        const d = await readJSON(r);
        // Coerce ISO strings → Date objects so the helpers work
        d.now = new Date(d.now);
        d.positions = d.positions.map(p => ({ ...p, entry_time: new Date(p.entry_time) }));
        d.trades = d.trades.map(t => ({ ...t, closed_at: new Date(t.closed_at) }));
        d.scanLog.last_scan_at = new Date(d.scanLog.last_scan_at);
        d.scanLog.recent_skips = d.scanLog.recent_skips.map(s => ({ ...s, ts: new Date(s.ts) }));
        window.MOCK = d;
        setData(d);
        setErr(null);
      } catch (e) {
        setErr(e.message);
      }
    };
    load();
    const iv = setInterval(load, 30_000);
    // Immediate refetch when the trading mode flips — waiting up to 30s
    // for the next poll would make the switch feel broken.
    window.addEventListener('stormedge-refetch', load);
    return () => { clearInterval(iv); window.removeEventListener('stormedge-refetch', load); };
  }, []);

  if (!data) {
    return (
      <div className="loading-screen">
        <span>{err ? `⚠ ${err}` : '· loading ·'}</span>
        {err && <a href="/">← back to sign in</a>}
      </div>
    );
  }

  const M = data;
  return (
    <div className="app">
      <TopBar portfolio={M.portfolio} scanLog={M.scanLog} activeTab={activeTab} setActiveTab={setActiveTab} />

      <main className="main">

        {activeTab === 'desk' && (
          <>
            {M.portfolio.circuit_tripped && (
              <div className="banner banner-circuit">
                <span>⚠</span>
                <span>
                  Daily loss limit of {fmtUSD(Math.abs(M.portfolio.daily_loss_limit))} reached.
                  Trading halted until midnight UTC.
                </span>
              </div>
            )}
            <HeaderStrip portfolio={M.portfolio} />
            <div className="row-main">
              <GlobePanel cities={M.cities} cityActivity={M.cityActivity} positions={M.positions} />
              <OpenPositions
                positions={M.positions}
                maxPositions={M.portfolio.max_concurrent_positions}
              />
            </div>
            <PerformanceStats stats={M.stats} />
          </>
        )}

        {activeTab === 'archive' && <RecentTrades trades={M.trades} />}

        {activeTab === 'models' && (
          <>
            <ScanFeed scanLog={M.scanLog} />
            <RecentSignals signals={M.recentSignals} />
          </>
        )}

        {activeTab === 'settings' && <SettingsPanel />}
      </main>

      <footer className="page-foot">
        <span>stormedge · {M.portfolio.mode.toLowerCase()} mode · polymarket weather bot</span>
        <span className="mono">UTC {M.now.toISOString().replace('T', ' ').slice(0, 19)}</span>
      </footer>

      {/* Bottom tab bar — CSS reveals it below 860px and hides the top nav */}
      <nav className="bottom-nav">
        {TABS.map(([id, label]) => (
          <button key={id} className={activeTab === id ? 'active' : ''} onClick={() => setActiveTab(id)}>
            <span className="bar" aria-hidden="true" />
            {label}
          </button>
        ))}
      </nav>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
