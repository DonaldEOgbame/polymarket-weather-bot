"""Model families — because most "independent" ensemble members are not.

`icon_global`, `icon_eu` and `icon_d2` are one model at three resolutions.
`ukmo_global_deterministic_10km` and `ukmo_uk_deterministic_2km` are one system.
`ecmwf_ifs025` and `ecmwf_aifs025_single` share initial conditions.
`gfs_graphcast025` and `ncep_aigfs025` are both GFS-initialised.

Counting those as independent manufactures agreement. Three ICON members that
agree because they are ICON push `model_agreement` up and `model_spread_std`
down, and both gates read exactly those numbers — so adding resolutions of a
model the bot already has would look like rising confidence while adding no
information at all. At four members that was a latent problem. Phase 2 roughly
triples the count and makes it the dominant one.

Two mechanisms:

  WEIGHT CAP. No family may carry more than FAMILY_WEIGHT_CAP of the blend,
      however many of its members are present.
  AGREEMENT ACROSS FAMILIES. A family votes ONCE, at its own internal weighted
      mean. Three ICON members that agree with each other contribute one
      opinion, not three.

This is also where the diurnal artifact is actually addressable. Local
aggregation cannot remove it (Phase 1.2 measured daily == max(hourly) to
0.000°F), so the only remaining lever is preferring members with a finer native
timestep where skill is comparable — hence NATIVE_TIMESTEP_HOURS.
"""
import logging

# model id -> family. A model absent from this table is treated as its own
# family, which is the safe default for a newly added member: it can never
# silently join an existing family's weight cap and dilute it.
MODEL_FAMILY = {
    # ECMWF — ifs025 and aifs025_single share initial conditions, so the AI
    # member is a different forecast of the same atmosphere, not a second
    # opinion about it.
    "ecmwf_ifs025": "ECMWF",
    "ecmwf_aifs025_single": "ECMWF",
    # NCEP — graphcast and aigfs are both GFS-initialised.
    "gfs_global": "NCEP",
    "gfs_graphcast025": "NCEP",
    "ncep_aigfs025": "NCEP",
    "gfs_hrrr": "NCEP",
    "ncep_nam_conus": "NCEP",
    # ncep_nbm_conus is deliberately NOT in NCEP: the National Blend of Models
    # is itself a station-calibrated multi-model consensus, so it is closer to
    # an independent opinion than to another GFS run. Its own family.
    "ncep_nbm_conus": "NBM",
    # DWD — one model at three resolutions.
    "icon_global": "DWD",
    "icon_eu": "DWD",
    "icon_d2": "DWD",
    # CCMEP (Environment Canada)
    "gem_global": "CCMEP",
    "gem_regional": "CCMEP",
    "gem_hrdps_continental": "CCMEP",
    # UKMO — one system at two resolutions.
    "ukmo_global_deterministic_10km": "UKMO",
    "ukmo_uk_deterministic_2km": "UKMO",
    # JMA
    "jma_gsm": "JMA",
    "jma_msm": "JMA",
    # KMA
    "kma_gdps": "KMA",
    "kma_ldps": "KMA",
    # CMA
    "cma_grapes_global": "CMA",
    # BOM
    "bom_access_global": "BOM",
    # Météo-France — ARPEGE and AROME share the same operational suite.
    "meteofrance_arpege_world": "METEOFRANCE",
    "meteofrance_arpege_europe": "METEOFRANCE",
    "meteofrance_arome_france_hd": "METEOFRANCE",
    # HARMONIE-AROME is run by several services from a shared consortium model.
    # Grouped together for the same reason ICON's resolutions are: they are one
    # model, not one per country.
    "knmi_harmonie_arome_europe": "HARMONIE",
    "knmi_harmonie_arome_netherlands": "HARMONIE",
    "dmi_harmonie_arome_europe": "HARMONIE",
    "metno_seamless": "HARMONIE",
    # ARPAE / Italia Meteo — both ICON-derived limited-area runs, but run and
    # assimilated independently of DWD's, so a separate family.
    "italia_meteo_arpae_icon_2i": "ARPAE",
    "arpae_cosmo_2i": "ARPAE",
}

# Native timestep in hours. The ONLY remaining lever on the diurnal artifact:
# Phase 1.2 measured that local aggregation cannot recover a clipped peak
# because Open-Meteo interpolates upstream of both endpoints. Measured diurnal
# swing scales with timestep (ICON hourly 0.79, JMA 6-hourly 3.03), so prefer a
# finer member where skill is comparable.
NATIVE_TIMESTEP_HOURS = {
    "ecmwf_ifs025": 3, "ecmwf_aifs025_single": 6,
    "gfs_global": 3, "gfs_graphcast025": 6, "ncep_aigfs025": 6,
    "gfs_hrrr": 1, "ncep_nam_conus": 1, "ncep_nbm_conus": 1,
    "icon_global": 3, "icon_eu": 1, "icon_d2": 1,
    "gem_global": 3, "gem_regional": 1, "gem_hrdps_continental": 1,
    "ukmo_global_deterministic_10km": 1, "ukmo_uk_deterministic_2km": 1,
    "jma_gsm": 6, "jma_msm": 1,
    "kma_gdps": 3, "kma_ldps": 1,
    "cma_grapes_global": 3,
    "bom_access_global": 3,
    "meteofrance_arpege_world": 3, "meteofrance_arpege_europe": 1,
    "meteofrance_arome_france_hd": 1,
    "knmi_harmonie_arome_europe": 1, "knmi_harmonie_arome_netherlands": 1,
    "dmi_harmonie_arome_europe": 1, "metno_seamless": 1,
    "italia_meteo_arpae_icon_2i": 1, "arpae_cosmo_2i": 1,
}


def family_of(model):
    """The model's family. An unknown model is its own family — the safe
    default, since it cannot then dilute an existing family's cap."""
    return MODEL_FAMILY.get(model, f"_SOLO_{model}")


def native_timestep(model):
    """Native output timestep in hours, or None if unrecorded."""
    return NATIVE_TIMESTEP_HOURS.get(model)


def cap_weights_by_family(weights, cap):
    """Renormalise `weights` so no family exceeds `cap` of the total.

    Within an over-weight family the members keep their RELATIVE proportions —
    the cap is a statement about how much the family may say, not about which
    of its members says it. Freed weight is redistributed across the other
    families in proportion to what they already hold, so a cap never changes the
    relative standing of the families it did not bind on.

    Iterates because redistribution can push a second family over the cap.
    Converges in a handful of passes; the bound is defensive.

    Returns a new dict summing to the same total as the input (1.0 for a
    normalised blend). A cap at or above 1.0, or a single-family blend, is a
    no-op — with one family there is nothing to redistribute to, and silently
    rescaling to `cap` would drop total weight on the floor."""
    if not weights or cap >= 1.0:
        return dict(weights)

    out = dict(weights)
    total = sum(out.values())
    if total <= 0:
        return out

    families = {}
    for m in out:
        families.setdefault(family_of(m), []).append(m)
    if len(families) <= 1:
        return out

    # A cap below 1/F is arithmetically impossible: F families cannot each hold
    # at most c of the total unless F*c >= 1. Asking for it does not fail
    # loudly, it OSCILLATES — every family is over the cap, gets scaled down,
    # and receives the freed weight straight back. Observed with the 0.35
    # default on a two-family city, where it converged to nothing and left DWD
    # holding 0.65. Two families with a 0.35 cap therefore means 0.50 each,
    # which is the tightest the constraint can actually be.
    effective = max(cap, 1.0 / len(families))

    for _ in range(50):
        fam_weight = {f: sum(out[m] for m in ms) for f, ms in families.items()}
        over = {f: w for f, w in fam_weight.items() if w > effective * total + 1e-12}
        if not over:
            break
        freed = 0.0
        for f, w in over.items():
            target = effective * total
            scale = target / w
            for m in families[f]:
                out[m] *= scale          # relative standing within the family kept
            freed += w - target
        under = [f for f in families if f not in over]
        under_total = sum(fam_weight[f] for f in under)
        if under_total <= 0:
            break
        for f in under:
            share = freed * (fam_weight[f] / under_total)
            for m in families[f]:
                out[m] += share * (out[m] / fam_weight[f])
    return out


def family_means(model_temps, weights):
    """{family: (weighted mean of its members, total weight of its members)}.

    A family's contribution is its own internal weighted mean — the unit of
    opinion for the agreement gate."""
    groups = {}
    for m, t in model_temps.items():
        w = weights.get(m, 0.0)
        if w <= 0:
            continue
        f = family_of(m)
        acc = groups.setdefault(f, [0.0, 0.0])
        acc[0] += t * w
        acc[1] += w
    return {f: (s / w, w) for f, (s, w) in groups.items() if w > 0}


def family_agreement(model_temps, weights, consensus, tolerance=2.0):
    """Weighted fraction of FAMILIES agreeing with `consensus`.

    Computed across families rather than across members, so three ICON
    resolutions that agree because they are ICON contribute one vote at their
    combined weight — not three votes that manufacture confidence.

    n-invariant in the sense tests/test_gate_invariance.py pins: splitting a
    member into two members of the same family at the same value leaves both the
    family mean and its weight unchanged, so this cannot move."""
    fams = family_means(model_temps, weights)
    total = sum(w for _, w in fams.values())
    if total <= 0:
        return 0.0
    agree = sum(w for mean, w in fams.values() if abs(mean - consensus) < tolerance)
    return agree / total


def family_spread(model_temps, weights):
    """Weighted standard deviation ACROSS FAMILY MEANS, in °F.

    The statistic MAX_MODEL_SPREAD_STD should gate on once the ensemble contains
    several members per family. A member-level spread counts ICON's three
    resolutions disagreeing slightly with each other as ensemble disagreement,
    when the question the gate asks is whether the world's forecasting centres
    disagree."""
    import math
    fams = family_means(model_temps, weights)
    total = sum(w for _, w in fams.values())
    if total <= 0:
        return 0.0
    mean = sum(m * w for m, w in fams.values()) / total
    var = sum(w / total * (m - mean) ** 2 for m, w in fams.values())
    return math.sqrt(max(var, 0.0))


def describe(model_temps, weights):
    """Family composition, for the replay log and the startup summary."""
    fams = family_means(model_temps, weights)
    total = sum(w for _, w in fams.values()) or 1.0
    return {f: {"mean": round(m, 2), "weight": round(w / total, 4),
                "members": sorted(x for x in model_temps if family_of(x) == f)}
            for f, (m, w) in sorted(fams.items())}


def validate_families(weights_by_region, cap):
    """Problems with the family tables; empty means clean.

    Every model in a blend must have a recorded family and timestep. An
    unrecorded family silently becomes a solo family and escapes the cap, which
    is the failure this whole module exists to prevent — so it is a boot error,
    not a default."""
    problems = []
    for region, weights in weights_by_region.items():
        for model in sorted(weights):
            if model not in MODEL_FAMILY:
                problems.append(
                    f"WEIGHTS[{region!r}]: '{model}' has no MODEL_FAMILY entry — it "
                    f"would count as an independent opinion and escape the "
                    f"{cap:.0%} family cap")
            if model not in NATIVE_TIMESTEP_HOURS:
                problems.append(
                    f"WEIGHTS[{region!r}]: '{model}' has no NATIVE_TIMESTEP_HOURS "
                    f"entry — the diurnal artifact cannot be reasoned about")
    return problems
