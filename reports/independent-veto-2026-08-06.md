# Independent forecast veto gate — build report

**Date:** 2026-08-06
**Branch:** `feat/remaining-features`
**Status:** built, tested, armed. `INDEPENDENT_VETO_ENABLED = true`.

A second-opinion forecast from outside the Open-Meteo ensemble, used only to
**refuse** trades. Never to adjust a probability.

Two owner decisions shaped what got built and are recorded here so they are
distinguishable from things the code discovered:

1. **DataHub only** for the 40 non-US cities. MET Norway (the plan's §2c
   fallback) was deliberately not implemented — see §2 below.
2. **Armed at deploy**, no shadow period (plan §5, owner decision 2026-08-05).

---

## 1. Provider per city — the 51-city resolution table

Probed live on 2026-08-06 for target date 2026-08-07.

**51 distinct cities** (52 `STATIONS` keys; `NYC` and `New York` are aliases of
the same LaGuardia station). 11 distinct US cities route to NWS, 40 to DataHub.

Routing is an **explicit city list**, not `STATIONS["region"]`. That field's
`"US"` bucket also contains Toronto, Mexico City and Panama, none of which
api.weather.gov serves — routing on it would have sent three cities to a
provider that 404s them. Locked by a test.

### 1a. NWS — 11 cities, all returning DATA

| City | Station | State | 2026-08-07 max |
|---|---|---|---|
| Atlanta | KATL | DATA | 87.0°F |
| Austin | KAUS | DATA | 97.0°F |
| Chicago | KORD | DATA | 84.0°F |
| Dallas | KDAL | DATA | 101.0°F |
| Denver | KBKF | DATA | 97.0°F |
| Houston | KHOU | DATA | 94.0°F |
| Los Angeles | KLAX | DATA | 77.0°F |
| Miami | KMIA | DATA | 88.0°F |
| NYC / New York | KLGA | DATA | 89.0°F |
| San Francisco | KSFO | DATA | 77.0°F |
| Seattle | KSEA | DATA | 86.0°F |

Every one resolves to a gridpoint and returns a parsed value. Verified by
`TestLiveProviders` (opt-in, `RUN_LIVE_PROVIDER_TESTS=1`).

### 1b. DataHub — 40 cities, all INCONCLUSIVE pending the key

Amsterdam, Ankara, Beijing, Berlin, Buenos Aires, Busan, Cape Town, Chengdu,
Chongqing, Guangzhou, Helsinki, Hong Kong, Istanbul, Jakarta, Jeddah, Kuala
Lumpur, Lagos, London, Lucknow, Madrid, Manila, Mexico City, Milan, Moscow,
Munich, Panama, Paris, Qingdao, Sao Paulo, Seoul, Shanghai, Shenzhen,
Singapore, Taipei, Tel Aviv, Tokyo, Toronto, Warsaw, Wellington, Wuhan.

All 40 currently return `INCONCLUSIVE` with detail
`METOFFICE_DATAHUB_KEY not configured`, and therefore **never veto**.

`INCONCLUSIVE`, deliberately, **not** `NO_DATA`. `NO_DATA` would assert that
UKMO serves nothing at those coordinates, which is false — and would become
"checked, found nothing" in the 14-day review. An unset key is a gap in our
configuration, not a fact about the world. Locked by a test.

**Consequence: 11 of 51 cities are gated at deploy.** The other 40 behave
exactly as they did before this feature existed until a key is set.

---

## 2. Why MET Norway was not built

The plan offered `api.met.no` as a fallback. It was not implemented, and the
reason is in a comment at the top of `independent.py` so nobody re-adds it
casually:

MET Norway runs MEPS for Scandinavia and **ECMWF globally**, with
observation-based post-processing only in Scandinavia and Spitsbergen. For 49 of
51 cities it is ECMWF-derived — the same model carrying the largest weight in
every regional blend. It would still catch staleness and station identity, but
it is not an independent skill opinion, and a source labelled "independent" that
agrees with the ensemble by construction is worse than no source: the gate would
look healthy while checking nothing.

---

## 3. Call budget

| Provider | Calls/day | Limit | Headroom |
|---|---|---|---|
| NWS | ≤ 66 | none published (fair-use) | n/a |
| DataHub | ≤ 120 | 360 (free plan) | 3.0× |

Cache is keyed `(city_key, target_date, is_high)` with a 6h TTL — 3 refreshes
per day. Both directions are separate cache entries, so a city with high and low
markets costs 2 calls per refresh.

- **DataHub:** 40 cities × 3 refreshes = 120 base, up to 240 if every city has
  both a high and a low market open. Still inside 360, but the margin is 1.5×
  in the worst case rather than 3×. If the book ever covers all 51 cities in
  both directions, raise `INDEPENDENT_CACHE_TTL_SECONDS` to 8h (2 refreshes/day)
  rather than dropping cities.
- **NWS:** 11 cities × 3 × 2 directions = 66, plus 11 one-time gridpoint
  lookups per process lifetime (the gridpoint cache never expires — a station's
  grid cell is a property of the coordinate, not of the forecast).

At the 10-minute scan interval an uncached gate would be roughly 7,300
calls/day, which is why the cache is not optional.

---

## 4. Projected fire rate — measured before deploy

The plan states the fire rate is unknown at deploy and that this is the risk
being accepted. It is partly measurable in advance, so it was measured.

### 4a. Gross disagreement (`> 5.0°F`)

NWS vs. the live Open-Meteo ensemble, all 11 US cities × high/low × 3 lead days,
**n = 58** real comparisons on 2026-08-06:

| statistic | value |
|---|---|
| mean \|NWS − ensemble\| | 2.17°F |
| median | 1.76°F |
| max | 10.47°F |
| **would trip 5.0°F** | **6 / 58 = 10.3%** |

Comfortably under the 25% tripwire. The measured mean of 2.17°F is close to the
project's own 2.59°F MAE, which is the sanity check that mattered — the two
pipelines agree at roughly the level two competent forecasts should.

### 4b. Bucket band (`±2.0°F`)

Measured against real market geometry: 695 unique markets from the local replay
log that pass the `forecast_margin` gate.

| margin from ensemble_mean to nearest bucket edge | |
|---|---|
| p25 | 4.7°F |
| median | 6.6°F |
| p75 | 8.8°F |

The band fires when the independent forecast sits within 2.0°F of the bucket, so
it needs `margin − 2.0` of movement toward the bucket to trip:

| independent sits this far toward the bucket | band fires on |
|---|---|
| 0.0°F (perfect agreement) | 1% |
| 1.0°F | 1% |
| 1.76°F (the measured median) | 13% |
| 3.0°F | 29% |

13% is an **upper bound** at the median: it assumes every disagreement points
toward the bucket, and direction is roughly even, so the realistic figure is
nearer 7%.

### 4c. Combined

Roughly **10–20%** on the 11 armed cities, under the 25% tripwire but not
comfortably. That is the correct place to be for a gate shipping armed: loose
enough not to stop the book, tight enough to be doing something.

**`FORECAST_MARGIN_F` (2.5) is only 0.5°F wider than `PLAUSIBLE_BAND_F` (2.0).**
A trade sitting exactly at the margin minimum has 0.5°F of headroom before the
band veto fires. It does not bind today because the median real margin is 6.6°F,
not 2.5°F — but the two constants are coupled, and lowering `FORECAST_MARGIN_F`
or raising `PLAUSIBLE_BAND_F` would couple them tightly. Neither should be moved
without re-running §4b.

---

## 5. City concentration — a finding, before any trades

**4 of the 6 gross-disagreement fires are Los Angeles and San Francisco.**

| City | Direction | Lead | NWS | Ensemble | \|diff\| |
|---|---|---|---|---|---|
| Los Angeles | high | +2d | — | — | **10.5°F** |
| Los Angeles | high | +1d | 77.0 | 86.5 | **9.5°F** |
| Austin | high | +3d | — | — | 5.6°F |
| NYC | high | +3d | — | — | 5.3°F |
| San Francisco | high | +2d | — | — | 5.2°F |
| San Francisco | high | +1d | 77.0 | 72.0 | 5.0°F |

Both are coastal marine-layer stations, and both disagreements are on the daily
**high** — the direction the marine layer controls. LAX at 9.5–10.5°F is not a
marginal difference; it is the size of gap this gate was built to surface.

**The city-concentration ERROR will almost certainly fire naming Los Angeles**
within the first 24h of real signals. When it does, the plan's instruction is
"fix the station, do not loosen the threshold" — but read the alert carefully
before acting, because there are two candidate explanations and they call for
opposite responses:

- **A station problem.** KLAX (33.9416, −118.4085) sits on the coast. If
  Polymarket's LA markets resolve on a different station, this is the Hong Kong
  bug again and the fix is the coordinate.
- **A genuine ensemble weakness.** NWS forecasters human-edit for the marine
  layer; the global models are known to run warm at coastal California stations
  in summer. If so, the veto is working exactly as designed and refusing LA
  highs is the correct outcome, not a false positive.

The 2026-08-05 settlement-window audit found every city's station text matched,
which points at the second explanation — but that audit checked the day
boundary, not the coordinate. **This is the single most valuable thing to check
after deploy**, and it is a question the counterfactual log will answer directly
once LA highs settle.

---

## 6. Tripwire, and one deliberate departure from the plan

§5b says the veto should auto-disable above "25% of gate-passing signals". As
implemented, the denominator is gate-passing signals **where the provider
returned `DATA`** — the signals the gate was actually in a position to act on.

The literal reading cannot detect the failure the tripwire exists for. With no
DataHub key, 40 of 51 cities are permanently `INCONCLUSIVE`. Under the loose
denominator, a **total** failure of the veto — firing on every single US signal
— would report roughly 11/51 = 22% and never trip. Under the implemented one it
reports 100% and trips immediately.

Both numbers are returned (`considered` and `all_gate_passing`); the tripwire
runs on the strict one. Two tests lock this.

Other tripwire properties:

- **Latching.** Once tripped it stays tripped until the process restarts, even
  if the rate falls back. An auto-re-arming gate would oscillate into the same
  storm, and each oscillation costs real refused trades.
- **`fired` counts the veto's conclusion, not its effect**, so the rate stays
  measurable after the gate has disabled itself. Counting effects would drive
  the rate to zero on disable and let it re-arm into the same storm.
- **Minimum sample of 20.** 1 veto out of 2 is 50% and means nothing.
- **A measurement failure never disables the gate.** A broken query must not
  silently remove a safety check.

---

## 7. What is stored

Nine columns on `replay_signals` (`REPLAY_SCHEMA_VERSION` 1 → 2), plus two gate
rows per signal in `replay_gates`:

`independent_source`, `independent_state`, `independent_value`,
`independent_fetched_at`, `independent_detail`, `disagreement_f`, `veto_gross`,
`veto_band`, `vetoed`.

The load-bearing distinctions:

- **`veto_gross`/`veto_band` (conclusion) vs `vetoed` (effect).** They differ
  whenever the tripwire has disarmed the gate. Keeping both is what makes "was
  the veto right?" answerable on a gate that turned itself off.
- **`independent_state` stored beside `independent_value`.** `NO_DATA` and
  `INCONCLUSIVE` stay distinguishable forever. Collapsing both to a NULL value
  would destroy the exact distinction this feature is built on.
- **NULL means "predates the veto", not "did not fire".** Schema-1 rows have no
  opinion. A replay must not read those NULLs as zeros.

The counterfactual is recorded on **every** signal, including ones an earlier
gate already refused — a counterfactual that only exists for trades nothing else
blocked is a biased sample of exactly the question the 14-day review asks.
Locked by a test.

`replay.py` reconstructs both gates from stored columns, with
`disagreement_veto_f` and `plausible_band_f` overrides, so thresholds can be
re-scored offline. Only a stored state of `DATA` can refuse in replay — the
alternative would reproduce, offline and permanently, the exact error this gate
exists to avoid.

---

## 8. Tests

`tests/test_independent.py` — 107 passing, 2 skipped (the opt-in network tests).
Full suite: **695 passed, 5 skipped**. CI boot checks (parser fixtures, config tables, env ranges, module
imports) all clean.

Every §7 item is covered. The ones that earn their place:

- **`test_no_error_path_ever_produces_a_temperature`** sweeps 8 failure shapes
  and asserts no numeric temperature, plus that the constructor refuses to hold
  one even when asked. This is the bug that produced three wrong conclusions in
  the coverage-matrix script.
- Timeout / 429 / 500 / 503 / HTML body / malformed JSON each resolve to
  `INCONCLUSIVE`; NWS 404 and DataHub 400 to `NO_DATA`; DataHub **401** to
  `INCONCLUSIVE`, because a rejected key says nothing about coverage and
  treating it as absence is how a rotated credential silently disables the gate
  while looking like a clean negative result.
- Gross fires at 5.1°F, not 4.9°F, not exactly 5.0°F.
- Band fires on overlap, not on adjacency; open-ended buckets use a half-line
  rather than a defaulted bound.
- Five assertions that the independent value never reaches `ensemble_mean`,
  `ensemble_std`, `model_spread`, `model_agreement` or `model_count` — in
  memory and on disk.
- A **baseline** test that the same market trades when the veto returns
  `NO_DATA`. Without it, a passing veto test proves nothing.

The one gap: **the DataHub parser is unverified against a real response.**
Everything about it is tested against fixtures built from the documented schema,
including the UTC→local-day conversion, but no request has ever been made. A
schema mismatch would surface as `INCONCLUSIVE` (fails open, no vetoes, warning
logged), not as a wrong number — the design makes this the safe failure — but it
is the first thing to check when a key lands.

---

## 9. What in the plan turned out to be wrong or incomplete

1. **"51 cities."** There are 51 distinct cities across 52 `STATIONS` keys —
   `NYC` and `New York` both map to KLGA. The plan's 11 US + 40 non-US split is
   correct on distinct cities.

2. **US routing cannot use `region`.** The plan implies "US cities" is a clean
   category; `STATIONS["region"] == "US"` also contains Toronto, Mexico City and
   Panama, which NWS 404s. Routing needed an explicit list.

3. **NWS needs no timezone handling; DataHub does.** The plan treats both the
   same. NWS `startTime` carries the station's local offset, so `[:10]` is the
   local day — the same trick `_aggregate_local_days` already uses. DataHub
   returns UTC and needs the IANA timezone from `metar.STATION_ICAO` (which
   covers every `STATIONS` key — checked). This is the one place in the veto
   path a day-boundary bug could hide.

4. **The §5b tripwire denominator is under-specified**, and the literal reading
   is unsafe. See §6.

5. **§5c says "record on every signal, as structured gate rows".** Gate rows
   cannot hold it — `replay_gates` is `(gate, observed REAL, threshold REAL,
   passed, detail)`, with no room for a source string, a state, or a fetch
   timestamp. The counterfactual went to columns on `replay_signals`; the two
   gate rows carry the decision. Both exist.

6. **`utils.get_session()` is unusable here.** It mounts
   `Retry(total=5, backoff_factor=1.0, status_forcelist=[429,500,...])`, which
   turns the plan's 3-second timeout budget into 30+ seconds of retry-and-backoff
   on a rate limit — on the critical path of every trade decision, for a signal
   allowed to be missing. This module uses its own session with no retries.

7. **The plan does not mention `FORECAST_MARGIN_F`.** It is 2.5 against a
   `PLAUSIBLE_BAND_F` of 2.0, leaving 0.5°F of headroom at the margin minimum.
   Not binding today, but the two are coupled. See §4c.

8. **The fire rate was not actually unknown.** §4 measured both conditions
   against real data before arming, and §5 surfaced a city concentration before
   a single trade was refused. The shadow period the owner decision skipped was
   partly recoverable from data already in hand.

---

## 10. Deployed — first-cycle results

Deployed to `stormedgev2` 2026-08-06 ~06:16 UTC. Fingerprint
`46f163bd3f1489f9` → **`783d10b3ff6d89a8`**; replay schema 1 → 2; all nine
columns migrated onto the live 252,774-row `replay_signals` table. Pre-deploy
Fly volume snapshot taken on `vol_vjyw3l6gjeo6yyxv`.

This was a single deploy of **all 11 phases**, against the runbook's staged
sequence — owner decision, taken with the trade-off stated. The app had been
running `a47729b` and nothing from the rollout had ever shipped.

**Two undefined names surfaced on the first live scan and stopped trading
completely.** Both predate this feature and neither was reachable by the test
suite or the import check:

- `is_tradeable_window(city)` in `scan_markets` — no `city` in scope, it is
  `city_key`. The per-candidate exception handler swallowed the `NameError`, so
  the scan reported `0 candidates | 0 traded | 0 skipped` rather than an error.
  Phase 1.1's settlement-window guard had therefore never run once.
- `flag_impossible_bucket`, defined in `db.py` and never imported into
  `scanner.py` — would have raised on the first off-lattice bucket.

Fixed in `098bd66`, redeployed, and a CI step now fails a push on any undefined
name. Trading resumed at **989 candidates | 1 traded | 988 skipped**.

### First-cycle veto behaviour

| | |
|---|---|
| NWS rows (`DATA`) | 125 |
| DataHub rows (`INCONCLUSIVE`) | 864 |
| Conclusion rate on `DATA` rows | **32.0%** (40/125) |
| Tripwire fire rate (`considered`) | **0/0** |
| **Trades actually refused by the veto** | **0** |

Every one of the 40 fires landed on a signal another gate had already refused.
The veto has not yet been the binding constraint on a single trade, which is
why the tripwire's denominator is still empty — and it is the counterfactual
log doing exactly its job.

### §5's prediction confirmed, precisely

| City | `DATA` rows | fired | rate |
|---|---|---|---|
| **Los Angeles** | 11 | 11 | **100%** |
| **San Francisco** | 11 | 11 | **100%** |
| every other US city | 103 | 18 | 17.5% |

LAX: NWS 77.0°F vs ensemble 86.5°F — **9.5°F**. SFO: NWS 77.0°F vs 72.0°F —
**5.0°F**. Both on the daily high, both coastal marine-layer stations, both
firing on every single evaluation.

LA + SF are 22 of 40 fires = **55%**, over the 50% concentration threshold.
Strip them out and the rate is 17.5%, comfortably under the 25% tripwire. The
whole overage is two cities.

The 32% headline is a **conclusion** rate over all `DATA` rows, not the
tripwire's rate over actionable signals — those are different denominators
(§6) and the tripwire is measuring the right one. But if an LA or SF signal
ever clears the other gates, the veto will refuse it, and on this evidence it
will refuse every one.

---

## 11. To do after deploy

- [ ] **Set `METOFFICE_DATAHUB_KEY`.** 40 of 51 cities are ungated until then.
      Verify the parser against a real response the moment it lands (§8).
- [ ] **Decide the Los Angeles / San Francisco question.** No longer a
      prediction — both fire on 100% of evaluations (§10). Do not loosen
      `DISAGREEMENT_VETO_F`: it would mask the finding without explaining it,
      and the other nine cities are at 17.5% and do not need it. The choice is
      between a station correction and accepting that the ensemble is genuinely
      weak at coastal California in summer. Settled LA/SF highs answer it.
- [ ] Record the deployed fingerprint and timestamp — it changes on this deploy
      and splits signal history. No calibration may pool across the boundary.
- [ ] **14-day review** (due 2026-08-20): fire rate overall and per city, and
      the settled outcomes of vetoed signals once `settled_value` populates.
      Loosen, tighten, or leave — on data.
