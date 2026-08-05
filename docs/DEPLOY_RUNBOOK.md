# Staged deployment runbook

The system is live with real positions. Everything on `feat/remaining-features`
is built and tested, but **it must not go out as one deploy.** The plan's
discipline is one phase per deploy, 48 hours apart, and that cannot be
compressed — each phase changes what the bot believes or how it is scored, and
48 hours is the minimum needed to see a full cycle of flow, gate rejections and
open-position behaviour before the next change lands on top.

Deploy in the order below. **Do not reorder.** 2.1 in particular must precede
any new ensemble member or the `best_match` defect is recreated at larger scale.

---

## Before anything

```bash
# 1. Confirm CI is green on the branch.
# 2. Take a manual backup and verify it restores.
flyctl ssh console -a stormedgev2 -C "python /app/backup.py"
flyctl ssh console -a stormedgev2 -C "python /app/backup.py status"

# 3. Record the CURRENT deployed fingerprint, so the boundary is queryable.
flyctl ssh console -a stormedgev2 -C "python -c \"import config; print(config.config_fingerprint())\""
```

Set an off-box backup destination before the first deploy. Without
`BACKUP_PUT_URL_TEMPLATE` the snapshot lives on the same volume as the database
it is protecting, and `backup_status()` will keep reporting that as a problem —
correctly.

```bash
flyctl secrets set BACKUP_PUT_URL_TEMPLATE='https://…/{name}?…' -a stormedgev2
```

If you would rather pull than push, `GET /api/backup/latest` serves the newest
verified snapshot to an authenticated request; a cron on a laptop is a perfectly
good off-box destination.

---

## Deploy order and fingerprints

Fingerprints below were computed at each commit, not asserted.

| # | commit | phase | fingerprint after | pipeline | ruler |
|---|---|---|---|---|---|
| — | `a47729b` | *(currently deployed)* | `46f163bd3f1489f9` | – | – |
| 1 | `b3612a8` | Prerequisites: backup + replay backfill | `46f163bd3f1489f9` | – | – |
| 2 | `090ba7f` | 0.1 CI and guard tests | `46f163bd3f1489f9` | – | – |
| 3 | `ef22ecd` | 0.2 Correlated risk limits | `46f163bd3f1489f9` | – | – |
| 4 | `d6bd7ca` | 1.1 Settlement-window audit | `46f163bd3f1489f9` | – | – |
| 5 | `e82faf4` | 1.2 Hourly forecasts | `3e4afc27bacf2bbc` | 2 | – |
| 6 | `acdddd8` | 1.3 Intraday conditioning | `b29f091c351ce7f9` | 3 | – |
| 7 | `41402d8` | 1.4 Settlement lattice | *(see note)* | 3 | 2 |
| 8 | `42f2781` | 2.1 Model families | `4965c6a94ca32201` | 4 | – |
| 9 | `ff88a41` | 3.1 Walk-forward harness | `4965c6a94ca32201` | 4 | – |
| 10 | `5e59731` | 3.2 Quantile model (disabled) | `4965c6a94ca32201` | 4 | – |
| — | `HEAD` | ruler version added to fingerprint | `d1f0f1c6e696bd67` | 4 | 2 |

Deploys 1–4 do not change the fingerprint because they do not change what the
bot believes: they add infrastructure, tests, a portfolio-level risk ceiling and
an audited `window` field. That is the fingerprint working as intended.

**Deploys 1–4 can be batched** if you would rather move faster: none of them
touches the forecast pipeline, and 0.2's caps are set not to bind on current
flow. Deploys 5 onward must be one at a time.

---

## Per-deploy procedure

```bash
git checkout feat/remaining-features
git reset --hard <commit>          # deploy exactly one phase
flyctl deploy -a stormedgev2
date -u                            # RECORD THIS — the phase boundary
```

Then, at +2h and +24h:

```bash
# Trade flow and gate-rejection distribution
flyctl ssh console -a stormedgev2 -C "python -c \"
from db import fetch_query
print(fetch_query('''SELECT signal_type, COUNT(*) c FROM signals
  WHERE timestamp > datetime(\\\"now\\\",\\\"-24 hours\\\")
  GROUP BY signal_type ORDER BY c DESC LIMIT 15'''))\""
```

**Stop and investigate if:**

- trade flow goes to **zero** for a full day,
- gate rejections **collapse onto a single gate** (`MAX_MODEL_SPREAD_STD` already
  rejects 78% of evaluations; if it reaches ~100% the ensemble change has
  retuned the gate rather than improved the forecast),
- the `IMPOSSIBLE_BUCKET` warning fires at all — 0 of 20,988 live markets have
  one, so a firing is a parser regression, not free money,
- `CORRELATION_BLOCK` fires more than a couple of times a day at current
  concurrency, which would mean the caps are binding earlier than intended.

---

## Phase-specific watch items

**5 — 1.2 Hourly.** Numerically a no-op today (verified end-to-end against the
live API), so any change in flow is a bug, not an effect. Watch the Open-Meteo
request count halve: max and min now come from one call.

**6 — 1.3 Intraday conditioning.** The largest behavioural change here.
Probabilities move materially on same-day markets. Expect `INTRADAY |` log lines
from ~06:00 local per city, and expect the effect to be biggest on **low**
markets, where the overnight minimum is usually set before dawn (the fitted
`g(h)` is already down to 0.07 by 06:00). If you see no `INTRADAY` lines at all
within a day, the METAR feed is not being reached and it is silently falling
back — which is the designed behaviour, but not the intended state.

**7 — 1.4 Settlement lattice.** This changes the RULER, not just the belief. US
resolutions before and after differ by up to 0.9°F. Do **not** pool calibration
across this boundary; `SETTLEMENT_RULER_VERSION` is in the fingerprint so the
split is queryable. Existing `resolutions` rows for North American cities are on
the old ruler and are now known to be slightly wrong — consider re-deriving them
before any recalibration.

**8 — 2.1 Model families.** No new members yet, so the only effect is that
`model_agreement` and `model_spread_std` are now family-level. With the current
4-member blend (four distinct families) the numbers barely move. The point is to
have this in place before 2.3–2.5.

---

## Still to do (not in this branch)

**2.2 coverage matrix** is written (`coverage_matrix.py`) and run for all 31
candidate models; `reports/coverage-matrix.md` has the results. It is ground
truth for the rest. Findings that change the plan:

- **`gfs_global` works at 51/51 cities**, including Wellington, Cape Town,
  Buenos Aires and Sao Paulo. The "GFS unavailable in the Southern Hemisphere"
  belief that moved those cities onto a GFS-less `GLOBAL` blend is false. That
  is a free improvement available to those cities today.
- **`bom_access_global` serves nothing.** HTTP 200 with nulls at all 51
  coordinates — not an invalid ID, not a domain limit. The plan's Southeast Asia
  and Wellington recommendations lean on it and need a different member.
- **`gfs_graphcast025` also serves nothing**, same signature. Of the three AI
  members only `ecmwf_aifs025_single` and `ncep_aigfs025` are actually available.
- **Three of the four proposed new globals work everywhere** —
  `ukmo_global_deterministic_10km`, `meteofrance_arpege_world` and
  `cma_grapes_global` are all 51/51. UKMO was the plan's first priority and it
  is available.
- **`kma_ldps` and `kma_gdps` serve nothing** (HTTP 200, zero non-null values at
  Seoul). The plan proposes both for Seoul and Busan; neither is available.
- **`arpae_cosmo_2i` is retired** — HTTP 400, "ARPAE COSMO models are not
  available anymore". The plan proposes it for Milan.
- **`metno_seamless` is available at 51/51**, but note `families.py` advises
  against `*_seamless` members whose bias is being measured: the underlying
  model changes with lead time, so the correction becomes a mixture.
- **`jma_msm` works at 6 cities including Tokyo** — the plan's natively-hourly
  Tokyo recommendation is available.

Horizon limits are recorded and matter for per-city lists: `gfs_hrrr`,
`icon_d2`, `gem_hrdps_continental` and `meteofrance_arome_france_hd` all return
nothing at 72h.

Five models came back **INCONCLUSIVE** — the bulk run hit the rate limiter and
every city returned an HTML error page. Probed individually they all work
(`ukmo_uk_deterministic_2km` 67/96 non-null at London, both KNMI HARMONIE
variants 77/96 at Amsterdam, `dmi_harmonie_arome_europe` 75/96 at Helsinki,
`italia_meteo_arpae_icon_2i` 85/96 at Milan), but their full domain maps are
unmeasured. Fill them in one at a time before using them:

```bash
python coverage_matrix.py --models ukmo_uk_deterministic_2km
```

**2.3, 2.4, 2.5** — new globals, AI members, and per-city `extra_models` — are
NOT implemented. They depend on the matrix being complete, and shipping them is
a substantial change to every city's blend that should follow the same one-phase
discipline. When they land:

- every new member enters at **explicit 0.0 bias correction, both directions,
  log-only** — never inheriting `TIMESTEP_BIAS_PRIOR`, which was the
  `gfs_global` bug,
- AI members stay at **zero weight for 2–4 weeks minimum** before admission to
  `MODEL_BIAS_CORRECTIONS` or the spread gate,
- `MIN_MODEL_COUNT` and `MAX_MODEL_SPREAD_STD` must be **re-derived from the new
  spread distribution**, not tuned by eye, and the derivation logged. Both were
  set for a 4-member ensemble.

---

## Stake

Consider reducing `FIXED_POSITION_SIZE` from $6 for the duration of Phases
1.2–2.5. Every one of them changes what the model believes, and the stake
currently sits at 3× the value in force during the measurement week, on
constants whose confidence intervals exceed a bucket width. The setting is
dashboard-tunable and needs no deploy.

## Rollback

```bash
flyctl releases -a stormedgev2
flyctl deploy -a stormedgev2 --image <previous-image>
```

Positions persist in the DB and are reconciled on restart, so a rollback with
open positions is safe by design. The one thing a rollback does NOT undo is a
schema migration; all migrations here are additive `ALTER TABLE ... ADD COLUMN`,
so an older build ignores the new columns rather than failing on them.
