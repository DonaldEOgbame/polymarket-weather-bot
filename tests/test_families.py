"""Model families — because most "independent" ensemble members are not.

icon_global/icon_eu/icon_d2 are one model at three resolutions.
ecmwf_ifs025 and ecmwf_aifs025_single share initial conditions. gfs_graphcast025
and ncep_aigfs025 are both GFS-initialised.

Counting those separately manufactures agreement, and the two gates that decide
almost every trade read exactly the statistics fake agreement inflates: three
ICON members agreeing because they are ICON push model_agreement up and
model_spread_std down. At four members that was latent. Phase 2 triples the
count and makes it dominant, which is why 2.1 lands before 2.2-2.4.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import config as C
import families as F
import weather as W


class TestFamilyAssignment:
    def test_icon_resolutions_are_one_family(self):
        assert (F.family_of("icon_global") == F.family_of("icon_eu")
                == F.family_of("icon_d2") == "DWD")

    def test_ecmwf_ai_shares_the_ecmwf_family(self):
        """ecmwf_aifs025_single is a different forecast of the same atmosphere,
        not a second opinion about it."""
        assert F.family_of("ecmwf_aifs025_single") == F.family_of("ecmwf_ifs025")

    def test_both_ai_gfs_members_are_ncep(self):
        for m in ("gfs_graphcast025", "ncep_aigfs025", "gfs_global"):
            assert F.family_of(m) == "NCEP", m

    def test_ukmo_resolutions_are_one_family(self):
        assert (F.family_of("ukmo_global_deterministic_10km")
                == F.family_of("ukmo_uk_deterministic_2km") == "UKMO")

    def test_nbm_is_its_own_family(self):
        """The National Blend of Models is itself a station-calibrated
        multi-model consensus, so it is closer to an independent opinion than to
        another GFS run — grouping it under NCEP would cap away the one member
        with genuinely different information."""
        assert F.family_of("ncep_nbm_conus") != F.family_of("gfs_global")

    def test_an_unknown_model_is_its_own_family(self):
        """The safe default: a newly added member can never silently join an
        existing family's cap and dilute it."""
        a = F.family_of("some_new_model")
        b = F.family_of("another_new_model")
        assert a != b and a != "DWD"

    def test_every_shipped_model_has_a_family_and_a_timestep(self):
        assert F.validate_families(W.WEIGHTS, C.FAMILY_WEIGHT_CAP) == []

    def test_boot_fails_on_a_model_with_no_family(self):
        problems = F.validate_families({"X": {"mystery_model": 1.0}},
                                       C.FAMILY_WEIGHT_CAP)
        assert any("MODEL_FAMILY" in p for p in problems)
        assert any("NATIVE_TIMESTEP_HOURS" in p for p in problems)


class TestWeightCap:
    def _fam_totals(self, capped):
        out = {}
        for m, w in capped.items():
            out[F.family_of(m)] = out.get(F.family_of(m), 0.0) + w
        return out

    def test_three_icon_resolutions_are_capped_to_one_familys_worth(self):
        """The case the module exists for."""
        w = {"ecmwf_ifs025": 0.30, "gfs_global": 0.20, "icon_global": 0.15,
             "icon_eu": 0.15, "icon_d2": 0.10, "jma_gsm": 0.05,
             "bom_access_global": 0.05}
        capped = F.cap_weights_by_family(w, 0.35)
        assert self._fam_totals(capped)["DWD"] == pytest.approx(0.35)

    def test_total_weight_is_preserved(self):
        w = {"ecmwf_ifs025": 0.40, "gfs_global": 0.30, "icon_global": 0.20,
             "gem_global": 0.10}
        assert sum(F.cap_weights_by_family(w, 0.35).values()) == pytest.approx(1.0)

    def test_relative_standing_within_a_family_is_kept(self):
        """The cap says how much a family may say, not which of its members
        says it."""
        w = {"icon_global": 0.30, "icon_eu": 0.20, "icon_d2": 0.10,
             "ecmwf_ifs025": 0.40}
        capped = F.cap_weights_by_family(w, 0.35)
        assert capped["icon_global"] / capped["icon_eu"] == pytest.approx(1.5)
        assert capped["icon_eu"] / capped["icon_d2"] == pytest.approx(2.0)

    def test_an_infeasible_cap_falls_back_to_one_over_n(self):
        """A cap below 1/F is arithmetically impossible: F families cannot each
        hold at most c unless F*c >= 1. Asking for it does not fail loudly, it
        OSCILLATES — every family is over, gets scaled down, and receives the
        freed weight straight back. Observed with the 0.35 default on a
        two-family city, converging to DWD holding 0.65."""
        w = {"ecmwf_ifs025": 0.40, "icon_global": 0.20, "icon_eu": 0.20,
             "icon_d2": 0.20}
        totals = self._fam_totals(F.cap_weights_by_family(w, 0.35))
        assert totals["ECMWF"] == pytest.approx(0.5)
        assert totals["DWD"] == pytest.approx(0.5)

    def test_no_family_ever_exceeds_the_effective_cap(self):
        for w in (
            {"ecmwf_ifs025": 0.9, "gfs_global": 0.05, "icon_global": 0.05},
            {"icon_global": 0.4, "icon_eu": 0.4, "gfs_global": 0.1, "gem_global": 0.1},
            {f"m{i}": 1.0 / 9 for i in range(9)},
        ):
            capped = F.cap_weights_by_family(w, 0.35)
            total = sum(capped.values())
            n_fam = len({F.family_of(m) for m in capped})
            effective = max(0.35, 1.0 / n_fam)
            for fam, fw in self._fam_totals(capped).items():
                assert fw <= effective * total + 1e-9, (fam, fw)

    def test_a_single_family_blend_is_untouched(self):
        """Nothing to redistribute to; rescaling to the cap would drop weight on
        the floor."""
        w = {"icon_global": 0.6, "icon_eu": 0.4}
        assert F.cap_weights_by_family(w, 0.35) == w


class TestAgreementIsAcrossFamilies:
    def test_three_agreeing_icon_members_vote_once(self):
        """The fake-agreement case stated directly. Under member-level counting
        the ICON bloc carries three votes' worth of weight against ECMWF's one."""
        temps = {"ecmwf_ifs025": 90.0, "icon_global": 85.0, "icon_eu": 85.0,
                 "icon_d2": 85.0}
        weights = {"ecmwf_ifs025": 0.25, "icon_global": 0.25, "icon_eu": 0.25,
                   "icon_d2": 0.25}
        fams = F.family_means(temps, weights)
        assert set(fams) == {"ECMWF", "DWD"}
        assert fams["DWD"][0] == pytest.approx(85.0)
        assert fams["DWD"][1] == pytest.approx(0.75)   # weight, not 3 votes

    def test_family_spread_ignores_within_family_scatter(self):
        """ICON's three resolutions disagreeing slightly with each other is not
        the world's forecasting centres disagreeing, which is what the gate is
        asking about."""
        weights = {"ecmwf_ifs025": 0.5, "icon_global": 0.2, "icon_eu": 0.2,
                   "icon_d2": 0.1}
        tight = {"ecmwf_ifs025": 90.0, "icon_global": 85.0, "icon_eu": 85.0,
                 "icon_d2": 85.0}
        # 0.2*80 + 0.2*90 + 0.1*85 = 42.5 = 0.5*85, so the DWD mean is EXACTLY
        # unchanged while its internal scatter goes from zero to 10°F.
        scattered = {"ecmwf_ifs025": 90.0, "icon_global": 80.0, "icon_eu": 90.0,
                     "icon_d2": 85.0}
        assert (F.family_means(scattered, weights)["DWD"][0]
                == pytest.approx(F.family_means(tight, weights)["DWD"][0]))
        assert (F.family_spread(scattered, weights)
                == pytest.approx(F.family_spread(tight, weights)))
        # The member-level statistic, which is what the gate read before 2.1,
        # nearly doubles on an ensemble whose centres agree exactly as much.
        assert (W._weighted_pstdev(scattered, weights)
                > W._weighted_pstdev(tight, weights) * 1.5)

    def test_a_real_disagreement_still_registers(self):
        """Bought invariance is worthless if the statistic stops responding."""
        weights = {"ecmwf_ifs025": 0.5, "icon_global": 0.5}
        near = {"ecmwf_ifs025": 90.0, "icon_global": 90.2}
        far = {"ecmwf_ifs025": 90.0, "icon_global": 99.0}
        assert F.family_spread(far, weights) > F.family_spread(near, weights) + 3.0
        assert F.family_agreement(far, weights, 94.5) < 0.6


class TestTheEngineUsesFamilies:
    def test_the_engine_reports_family_composition(self):
        temps = {"ecmwf_ifs025": 90.0, "gfs_global": 91.2, "icon_global": 89.4,
                 "gem_global": 90.6}
        eng = W._build_engine_result(temps, "US", "Chicago", 36.0, True)
        assert eng["family_count"] == 4
        assert set(eng["families"]) == {"ECMWF", "NCEP", "DWD", "CCMEP"}
        assert "member_spread_std" in eng, (
            "a replay must be able to tell whether family grouping changed a "
            "decision"
        )

    def test_the_gated_spread_is_the_family_one(self):
        temps = {"ecmwf_ifs025": 90.0, "icon_global": 84.0, "icon_eu": 85.0,
                 "icon_d2": 87.0}
        saved = W.WEIGHTS["US"]
        W.WEIGHTS["US"] = {"ecmwf_ifs025": 0.5, "icon_global": 0.2,
                           "icon_eu": 0.2, "icon_d2": 0.1}
        try:
            eng = W._build_engine_result(temps, "US", "Chicago", 36.0, True)
            assert eng["model_spread_std"] != pytest.approx(eng["member_spread_std"])
            assert eng["model_spread"] == eng["model_spread_std"]
        finally:
            W.WEIGHTS["US"] = saved

    def test_the_flag_restores_member_level_gating(self, monkeypatch):
        """The current thresholds were fitted against member-level statistics.
        The escape hatch is how they get compared, not decoration."""
        monkeypatch.setattr(W, "GATE_ACROSS_FAMILIES", False)
        temps = {"ecmwf_ifs025": 90.0, "icon_global": 84.0, "icon_eu": 85.0,
                 "icon_d2": 87.0}
        saved = W.WEIGHTS["US"]
        W.WEIGHTS["US"] = {"ecmwf_ifs025": 0.5, "icon_global": 0.2,
                           "icon_eu": 0.2, "icon_d2": 0.1}
        try:
            eng = W._build_engine_result(temps, "US", "Chicago", 36.0, True)
            assert eng["model_spread_std"] == pytest.approx(eng["member_spread_std"])
        finally:
            W.WEIGHTS["US"] = saved


class TestNativeTimestep:
    def test_the_six_hourly_members_are_recorded(self):
        """The only remaining lever on the diurnal artifact: Phase 1.2 measured
        that local aggregation cannot recover a clipped peak."""
        assert F.native_timestep("jma_gsm") == 6
        assert F.native_timestep("ecmwf_aifs025_single") == 6
        assert F.native_timestep("gfs_graphcast025") == 6
        assert F.native_timestep("ncep_aigfs025") == 6

    def test_the_hourly_members_are_recorded(self):
        for m in ("jma_msm", "icon_eu", "icon_d2", "gfs_hrrr"):
            assert F.native_timestep(m) == 1, m

    def test_every_ai_member_is_six_hourly(self):
        """They inherit the WORST diurnal artifact, which is part of why they
        enter at zero weight."""
        for m in ("ecmwf_aifs025_single", "gfs_graphcast025", "ncep_aigfs025"):
            assert F.native_timestep(m) == 6, m
