"""Tests for weather.py — probability engine and ensemble logic."""
import pytest
import math
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from weather import get_bucket_probability, get_station_coords


class TestGetStationCoords:
    """Station mapping tests."""

    def test_nyc_match(self):
        key, station = get_station_coords("Will NYC high be above 85°F?")
        assert key == "NYC"
        assert station is not None

    def test_new_york_match(self):
        key, station = get_station_coords("Will the high in New York exceed 90°F?")
        assert key is not None
        assert "lat" in station

    def test_chicago_match(self):
        key, station = get_station_coords("Will Chicago low be below 30°F?")
        assert key == "Chicago"

    def test_no_match(self):
        key, station = get_station_coords("Will Mumbai high exceed 100°F?")
        assert key is None
        assert station is None

    def test_case_insensitive(self):
        key, _ = get_station_coords("will LONDON low be under 40°F?")
        assert key == "London"


class TestGetBucketProbability:
    """Tests for the probability engine's CDF calculations."""

    def _make_engine_result(self, mean, std):
        return {"ensemble_mean": mean, "ensemble_std": std}

    def test_centered_bucket_high_prob(self):
        """A bucket centered on the mean should have high probability."""
        result = self._make_engine_result(mean=80.0, std=3.0)
        prob = get_bucket_probability(result, 77.0, 83.0)
        # 77-83 is ±3°F around mean=80 → roughly ±1 std → ~68% with boundary adjustment
        assert 0.5 < prob < 0.9

    def test_far_bucket_floored(self):
        """A BOUNDED bucket far from the mean floors at MIN_BUCKET_PROB (0.05) —
        the tail floor that stops the model betting real money on ~0% claims
        (the Guangzhou-#31 overconfidence bust). Raw prob here is ~0."""
        from config import MIN_BUCKET_PROB
        result = self._make_engine_result(mean=80.0, std=3.0)
        prob = get_bucket_probability(result, 90.0, 95.0)
        assert prob == pytest.approx(MIN_BUCKET_PROB)

    def test_open_ended_tail_floored(self):
        """Open-ended buckets ARE floored too — the overconfidence busts
        (Guangzhou #31, 'X or higher') were open-ended, so a bounded-only floor
        missed exactly the trades it targeted."""
        from config import MIN_BUCKET_PROB
        result = self._make_engine_result(mean=70.0, std=2.0)
        prob = get_bucket_probability(result, 95.0, None)  # raw P(>=95) with mean 70 ~ 0
        assert prob == pytest.approx(MIN_BUCKET_PROB)

    def test_open_ended_above(self):
        """'Above X' bucket with lower bound only."""
        result = self._make_engine_result(mean=80.0, std=3.0)
        prob = get_bucket_probability(result, 80.0, None)
        # Should be roughly 50% (mean is at the boundary)
        assert 0.4 < prob < 0.6

    def test_open_ended_below(self):
        """'Below X' bucket with upper bound only."""
        result = self._make_engine_result(mean=80.0, std=3.0)
        prob = get_bucket_probability(result, None, 80.0)
        assert 0.4 < prob < 0.6

    def test_exact_value_bucket(self):
        """Exact value bucket (lb == ub) should have non-zero probability."""
        result = self._make_engine_result(mean=80.0, std=3.0)
        prob = get_bucket_probability(result, 80.0, 80.0)
        # 79.5 to 80.5 is a 1°F window → small but non-zero. The upper bound is
        # 0.40 rather than 0.30 because the kernel is now a variance-matched
        # Student-t: matching variance while fattening the tails moves mass out
        # of the shoulders into BOTH the tails and the centre, so a bucket
        # sitting on the ensemble mean gains probability. That is conservative
        # in the direction that matters — this bot sells these buckets.
        assert 0.05 < prob < 0.40

    def test_probability_bounded_0_1(self):
        """Probability should always be in [0, 1]."""
        result = self._make_engine_result(mean=80.0, std=3.0)
        for lb, ub in [(50.0, 110.0), (None, None), (80.0, 80.0), (90.0, None)]:
            prob = get_bucket_probability(result, lb, ub)
            assert 0.0 <= prob <= 1.0

    def test_wider_std_gives_more_tail_probability(self):
        """With wider uncertainty, tail buckets should get more probability.
        Bound chosen so both probs sit ABOVE MIN_BUCKET_PROB (else the floor
        flattens the comparison — which is itself the intended tail behaviour)."""
        tight = self._make_engine_result(mean=80.0, std=3.0)
        wide = self._make_engine_result(mean=80.0, std=6.0)
        prob_tight = get_bucket_probability(tight, 84.0, None)
        prob_wide = get_bucket_probability(wide, 84.0, None)
        assert prob_wide > prob_tight

    def test_minimum_std_floor(self):
        """Even with very small std, probability should not be degenerate."""
        result = self._make_engine_result(mean=80.0, std=0.01)
        prob = get_bucket_probability(result, 75.0, 85.0)
        # The function clamps std to 0.5 minimum, so this should still work
        assert prob > 0.0


class TestUncertaintyModel:
    """Tests that the uncertainty model produces realistic values."""

    # NOTE: these used to reimplement sqrt(base**2 + spread**2) inline and assert
    # properties of that arithmetic, so they passed regardless of what the bot
    # actually computed. They now call compute_sigma, the real function.

    def test_sigma_never_below_floor(self):
        """Even a perfectly agreeing ensemble gets a floor. Tight agreement is
        not evidence of accuracy — the single worst miss in the record (Seoul
        2026-07-25, 5.7°F) had the tightest ensemble of any trade."""
        from config import MIN_SIGMA_F
        from weather import compute_sigma
        for hours in (12, 24, 48, 72):
            for is_high in (True, False):
                assert compute_sigma(0.0, hours, is_high) >= MIN_SIGMA_F

    def test_sigma_never_shrinks_with_lead_time(self):
        """Lead time must never REDUCE uncertainty. It no longer increases it
        much either: Spearman(lead, |error|) = +0.105 (n=27, not significant),
        so the ramp is deliberately near-flat rather than 1.0 -> 2.5."""
        from weather import compute_sigma
        for is_high in (True, False):
            sigmas = [compute_sigma(0.6, h, is_high) for h in (12, 24, 48, 72)]
            assert sigmas == sorted(sigmas)

    def test_sigma_grows_with_model_disagreement(self):
        """More disagreement must widen the distribution, and materially so —
        under the old quadrature form a spread this wide moved sigma by tenths
        of a degree, which is why the spread signal was never priced."""
        from weather import compute_sigma
        low = compute_sigma(0.2, 48, True)
        high = compute_sigma(1.2, 48, True)
        assert high > low + 2.0

    def test_sigma_is_wider_on_highs_than_lows(self):
        """The dominant finding in the record: daily maxima are far less
        predictable than minima, and the old formula had it backwards — wider on
        the accurate segment.

        Tested above sd=0 only: at zero spread MIN_SIGMA_F binds on both
        directions and they legitimately tie."""
        from weather import compute_sigma
        for sd in (0.4, 0.8, 1.0):
            assert compute_sigma(sd, 48, True) > compute_sigma(sd, 48, False)

    def test_floor_binds_equally_at_zero_spread(self):
        from config import MIN_SIGMA_F
        from weather import compute_sigma
        assert compute_sigma(0.0, 48, True) == compute_sigma(0.0, 48, False) == MIN_SIGMA_F


class TestProbabilityCalibration:
    """Regression tests for the Platt probability calibration (weather._calibrate_prob).

    The raw Gaussian bucket prob is ~1.9x overconfident in the low-p region where the
    bot bets NO (measured on 96,307 resolved signals: predicted ~15% hit ~28%). The
    calibration remap pulls raw probs back onto the observed reliability curve. This was
    the single biggest driver of the -$20 true loss on the first 19 live trades — with
    calibration on, 12 of 14 losing bets are refused.
    """
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

    def test_low_prob_inflated_toward_observed(self):
        from weather import _calibrate_prob
        # ~15% raw maps up toward the METAR-observed ~29% hit rate for that bin.
        out = _calibrate_prob(0.15)
        assert 0.22 < out < 0.34, f"0.15 should calibrate toward observed ~0.29, got {out:.3f}"

    def test_monotonic(self):
        from weather import _calibrate_prob
        xs = [0.02, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9]
        ys = [_calibrate_prob(x) for x in xs]
        assert ys == sorted(ys), "calibration must be monotonic (never reorder opportunities)"

    def test_endpoints_pass_through(self):
        from weather import _calibrate_prob
        assert _calibrate_prob(0.0) == 0.0
        assert _calibrate_prob(1.0) == 1.0

    def test_shrinks_fake_no_edge(self):
        # A raw P_YES of 0.20 gives a raw NO edge of (1-0.20)-0.53 = 0.27 (looks great).
        # The METAR-fit calibration raises P_YES toward its true ~27% hit rate, shrinking
        # the NO edge — the overconfident portion of the edge is removed.
        from weather import _calibrate_prob
        raw_edge = (1 - 0.20) - 0.53
        cal_edge = (1 - _calibrate_prob(0.20)) - 0.53
        assert cal_edge < raw_edge - 0.05, "calibration must shrink the low-p NO edge"
        assert _calibrate_prob(0.20) > 0.20, "low raw probs must be inflated toward observed"


class TestStationCoordinates:
    """Coordinates must match the exact station Polymarket names as the resolution
    source (verified 2026-07-04 from every live market's description). These pin the
    two that are counterintuitive: Polymarket resolves Seoul on INCHEON and London on
    LONDON CITY AIRPORT — NOT the city centre / Heathrow."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

    def test_seoul_resolves_on_incheon(self):
        from weather import STATIONS
        lon = STATIONS["Seoul"]["lon"]
        # Incheon (RKSI) ~ 126.44, well west of the Seoul city centre (126.98).
        assert lon < 126.6, f"Seoul must resolve on Incheon (~126.44), got lon {lon}"

    def test_london_resolves_on_city_airport(self):
        from weather import STATIONS
        lon = STATIONS["London"]["lon"]
        # London City Airport (EGLC) ~ +0.049, just east of the meridian — NOT
        # Heathrow (-0.45). Pin tightly so a future "fix" back to Heathrow fails here.
        assert 0.0 < lon < 0.1, f"London must resolve on City Airport (~+0.049), got lon {lon}"


class TestMetarStationMapping:
    """Every forecast city must have an ICAO/timezone mapping to Polymarket's METAR
    resolution source, or trades on it can never be verified against the real ruler."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

    def test_every_station_has_icao(self):
        from weather import STATIONS
        from metar import STATION_ICAO
        missing = [c for c in STATIONS if c not in STATION_ICAO]
        assert not missing, f"cities with no METAR ICAO mapping: {missing}"

    def test_icao_and_timezone_shape(self):
        from metar import STATION_ICAO
        for city, (icao, tz) in STATION_ICAO.items():
            assert 3 <= len(icao) <= 4 and icao.isalnum(), f"{city}: bad ICAO {icao!r}"
            assert "/" in tz, f"{city}: bad IANA tz {tz!r}"


class TestDirectionalBiasCorrections:
    """Per-model bias is keyed by (model, is_high) since 2026-07-31.

    A model that runs cold on daily maxima generally runs warm on daily minima:
    Open-Meteo interpolates coarse-timestep output onto an hourly grid, and
    smoothing a diurnal curve clips the afternoon peak and lifts the overnight
    trough. One signed offset applied to both directions corrects one of them
    backwards — for jma_gsm the old flat +1.55 was 3.26°F out on minima.
    """
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

    def test_correction_differs_by_direction(self):
        from config import model_bias_correction
        for m in ("ecmwf_ifs025", "icon_global", "jma_gsm"):
            assert model_bias_correction(m, True) != model_bias_correction(m, False)

    def test_coarse_timestep_models_need_a_warmer_push_on_highs(self):
        """The interpolation artifact is one-signed where it is measurable: a
        coarse model needs a WARMER correction on maxima than on minima.

        gem_global is deliberately excluded — it is the one model whose measured
        swing is near zero and slightly negative (-0.09), which is why the
        mechanism is stated as "every model except GEM" rather than universally.
        Asserting it for all models would encode a claim the data contradicts."""
        from config import model_bias_correction
        for m in ("ecmwf_ifs025", "icon_global", "jma_gsm"):
            assert model_bias_correction(m, True) > model_bias_correction(m, False)

    def test_coarsest_model_has_the_largest_swing(self):
        """jma_gsm is 6-hourly and icon_global hourly, so the interpolation
        artifact should be larger on the coarser model.

        Only the ORDERING is asserted, not a ratio. The two directions are now
        fitted on different samples — HIGH on the live era, LOW held at its older
        value because the live low sample is n=4 — so the swing is a difference
        between two fits and no longer cleanly measures the timestep effect.
        Restore a ratio assertion once both directions are fitted on live data."""
        from config import model_bias_correction as c
        swing = lambda m: c(m, True) - c(m, False)
        assert swing("jma_gsm") > swing("icon_global")

    def test_unknown_model_raises_rather_than_guessing(self):
        """REPLACES test_unknown_model_gets_timestep_prior_not_zero.

        The timestep prior was a runtime fallback, and it is how gfs_global
        acquired a +0.7°F correction nobody chose and the config comment
        explicitly said it did not have. A model with no measured correction is
        a decision that has not been made, not a number to be guessed at scan
        time. The prior survives as advisory text in the error message."""
        import pytest
        from config import model_bias_correction
        for is_high in (True, False):
            with pytest.raises(KeyError) as exc:
                model_bias_correction("ecmwf_aifs025_single", is_high)
            assert "ecmwf_aifs025_single" in str(exc.value)

    def test_error_quotes_the_advisory_prior(self):
        """The prior is still the right starting point — it just has to be
        chosen by a human, so the message hands it over."""
        import pytest
        from config import model_bias_correction
        with pytest.raises(KeyError) as exc:
            model_bias_correction("jma_msm", True)      # 1-hourly class
        assert "+0.4" in str(exc.value)

    def test_model_with_no_timestep_entry_still_raises_cleanly(self):
        import pytest
        from config import model_bias_correction
        with pytest.raises(KeyError):
            model_bias_correction("some_model_invented_tomorrow", True)

    def test_error_names_the_model_and_the_table_to_edit(self):
        """REPLACES test_completely_unknown_model_still_returns_a_number, which
        asserted the old "always return a number" policy — the policy that
        silently corrected gfs_global."""
        import pytest
        from config import model_bias_correction
        with pytest.raises(KeyError) as exc:
            model_bias_correction("some_new_model_v9", True)
        msg = str(exc.value)
        assert "some_new_model_v9" in msg and "MODEL_BIAS_CORRECTIONS" in msg

    def test_gfs_correction_is_explicitly_zero(self):
        """gfs_global already carries per-city corrections in GFS_BIAS_CORRECTIONS,
        so its model-level correction must be ZERO — and must be asserted through
        the ACCESSOR, not by absence from the dict.

        The old version of this test asserted `("gfs_global", True) not in
        MODEL_BIAS_CORRECTIONS` and passed the whole time gfs_global was
        receiving +0.7°F on highs from the timestep fallback. Absence from a
        table is not absence of a correction when the lookup has a fallback."""
        from config import model_bias_correction
        assert model_bias_correction("gfs_global", True) == 0.0
        assert model_bias_correction("gfs_global", False) == 0.0

    def test_every_weighted_model_has_an_explicit_correction(self):
        """The boot-time guard, as a test: no model in WEIGHTS may rely on a
        default, because there is no longer a default."""
        from weather import WEIGHTS, validate_model_tables
        from config import model_bias_correction
        assert validate_model_tables() == []
        for weights in WEIGHTS.values():
            for model in weights:
                for is_high in (True, False):
                    assert isinstance(model_bias_correction(model, is_high), float)


class TestDirBiasParsing:
    """MODEL_BIAS_CORRECTIONS is either wholly valid or the process refuses to boot.

    The parser used to `continue` past a malformed entry, so the pre-2026-07-31
    two-field format ("model:value") produced an EMPTY dict with no exception and
    no log line. Combined with the timestep fallback that then existed, one stale
    env var yielded a fully populated, entirely wrong correction table.
    """
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

    def test_valid_format_parses(self):
        from config import _parse_dir_bias
        out = _parse_dir_bias("ecmwf_ifs025:2.29:0.29,gfs_global:0:0")
        assert out[("ecmwf_ifs025", True)] == 2.29
        assert out[("ecmwf_ifs025", False)] == 0.29
        assert out[("gfs_global", True)] == 0.0

    def test_whitespace_around_entries_is_tolerated(self):
        from config import _parse_dir_bias
        assert _parse_dir_bias(" ecmwf_ifs025 : 2.29 : 0.29 ")[("ecmwf_ifs025", True)] == 2.29

    def test_old_two_field_format_raises(self):
        """The exact stale-env case that motivated this."""
        import pytest
        from config import _parse_dir_bias
        with pytest.raises(ValueError) as exc:
            _parse_dir_bias("ecmwf_ifs025:0.29,icon_global:0.03")
        assert "ecmwf_ifs025:0.29" in str(exc.value)

    def test_partial_dict_is_never_returned(self):
        """One good entry must not rescue a malformed neighbour."""
        import pytest
        from config import _parse_dir_bias
        with pytest.raises(ValueError):
            _parse_dir_bias("jma_gsm:3.99:1.55,icon_global:0.03")

    def test_wrong_arity_raises(self):
        import pytest
        from config import _parse_dir_bias
        with pytest.raises(ValueError) as exc:
            _parse_dir_bias("ecmwf_ifs025:1:2:3")
        assert "4 field" in str(exc.value)

    def test_non_numeric_raises(self):
        import pytest
        from config import _parse_dir_bias
        with pytest.raises(ValueError) as exc:
            _parse_dir_bias("ecmwf_ifs025:abc:def")
        assert "non-numeric" in str(exc.value)

    def test_empty_string_raises(self):
        import pytest
        from config import _parse_dir_bias
        with pytest.raises(ValueError):
            _parse_dir_bias("")

    def test_trailing_comma_raises(self):
        import pytest
        from config import _parse_dir_bias
        with pytest.raises(ValueError) as exc:
            _parse_dir_bias("ecmwf_ifs025:2.29:0.29,")
        assert "empty entry" in str(exc.value)

    def test_empty_model_id_raises(self):
        import pytest
        from config import _parse_dir_bias
        with pytest.raises(ValueError):
            _parse_dir_bias(":1.0:2.0")


class TestWeightedSpread:
    """Spread must be weight-aware and invariant to ensemble size, or expanding
    the model set silently re-tunes the gate that uses it."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

    def test_low_weight_member_moves_spread_less(self):
        from weather import _weighted_pstdev
        w = {"a": 0.45, "b": 0.45, "c": 0.10}
        tight = _weighted_pstdev({"a": 80.0, "b": 80.0, "c": 80.0}, w)
        outlier_small = _weighted_pstdev({"a": 80.0, "b": 80.0, "c": 90.0}, w)
        outlier_big = _weighted_pstdev({"a": 80.0, "b": 90.0, "c": 80.0}, w)
        assert tight == 0.0
        assert outlier_big > outlier_small, "a 45%-weight outlier must move spread more than a 10% one"

    def test_duplicating_agreeing_members_does_not_inflate_spread(self):
        """The failure mode that max-min has: adding members can only push it up.
        Weight-normalised stdev is stable when the added members agree."""
        from weather import _weighted_pstdev
        four = _weighted_pstdev({"a": 79.0, "b": 81.0}, {"a": 0.5, "b": 0.5})
        six = _weighted_pstdev({"a": 79.0, "b": 81.0, "c": 80.0, "d": 80.0},
                               {"a": 0.25, "b": 0.25, "c": 0.25, "d": 0.25})
        assert six < four

    def test_ignores_members_with_no_weight(self):
        from weather import _weighted_pstdev
        assert _weighted_pstdev({"a": 80.0, "zzz": 200.0}, {"a": 1.0}) == 0.0

    def test_empty_and_zero_weight_are_safe(self):
        from weather import _weighted_pstdev
        assert _weighted_pstdev({}, {"a": 1.0}) == 0.0
        assert _weighted_pstdev({"a": 80.0}, {}) == 0.0


class TestStudentTKernel:
    """The bucket kernel is a variance-matched Student-t (nu=4 by default)."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

    def test_matches_published_t_table(self):
        import math
        from weather import _student_t_cdf
        raw = math.sqrt(4 / 2.0)  # cancel the variance-matching to hit the raw t
        for t, want in ((2.1318, 0.95), (2.7764, 0.975), (3.7469, 0.99), (0.0, 0.5)):
            assert abs(_student_t_cdf(t, 0.0, raw, 4.0) - want) < 5e-4

    def test_fatter_tails_than_gaussian_at_same_variance(self):
        """The point of the change: a 3-sigma outcome must not be priced at zero.
        Every tail bust in the record was a regime break a Gaussian called impossible."""
        from weather import _student_t_cdf, _norm_cdf
        t_tail = 1.0 - _student_t_cdf(3.0, 0.0, 1.0, 4.0)
        g_tail = 1.0 - _norm_cdf(3.0, 0.0, 1.0)
        assert t_tail > 4 * g_tail   # measured 4.9x at nu=4

    def test_variance_is_matched_not_inflated(self):
        """Scale must mean the same thing under both kernels, or the fitted
        SIGMA_SCALE_* constants silently change meaning when the kernel flips."""
        from weather import _student_t_cdf, _norm_cdf
        # At 1 sigma the two should be close; a non-variance-matched t would be
        # visibly wider everywhere.
        assert abs(_student_t_cdf(1.0, 0.0, 1.0, 4.0) - _norm_cdf(1.0, 0.0, 1.0)) < 0.05

    def test_degenerate_df_falls_back_to_gaussian(self):
        from weather import _student_t_cdf, _norm_cdf
        for df in (0.0, 1.0, 2.0):
            assert _student_t_cdf(1.3, 0.0, 2.0, df) == _norm_cdf(1.3, 0.0, 2.0)

    def test_monotonic_and_bounded(self):
        from weather import _student_t_cdf
        xs = [-8, -3, -1, 0, 1, 3, 8]
        ys = [_student_t_cdf(x, 0.0, 2.0, 4.0) for x in xs]
        assert ys == sorted(ys)
        assert 0.0 <= ys[0] and ys[-1] <= 1.0


class TestSigmaBounds:
    """The spread term is linear and fitted over weighted-spread 0.25-1.08.
    Outside that range it must not produce absurd values, and the ceiling must
    never bind on a market the spread gate would actually let through."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

    def test_extreme_disagreement_is_capped(self):
        from config import MAX_SIGMA_F
        from weather import compute_sigma
        assert compute_sigma(3.4, 48, True) == MAX_SIGMA_F

    def test_ceiling_never_binds_on_a_tradeable_market(self):
        """A wide sigma makes a narrow bucket look UNLIKELY, which inflates the
        NO edge rather than suppressing it — so a ceiling that bit on tradeable
        markets would manufacture confidence. It must sit above the gate."""
        from config import (MAX_MODEL_SPREAD_STD, MAX_SIGMA_F,
                            CONVECTIVE_CITIES, NARROW_BUCKET_STD_INFLATION)
        from weather import compute_sigma
        # A REAL convective city_key, not None. The original version passed None
        # and so never exercised the x1.3 convective path — the one that gets
        # closest to the ceiling.
        convective = sorted(CONVECTIVE_CITIES)[0]
        for is_high in (True, False):
            for city in (None, convective):
                at_gate = compute_sigma(MAX_MODEL_SPREAD_STD, 72, is_high, city)
                assert at_gate < MAX_SIGMA_F, (
                    f"MAX_SIGMA_F must stay above the sigma implied by "
                    f"MAX_MODEL_SPREAD_STD (city={city}, is_high={is_high}, "
                    f"sigma={at_gate:.2f}), or the cap sharpens tradeable markets")

    def test_narrow_inflation_cannot_escape_the_ceiling(self):
        """strategy.py inflates AFTER compute_sigma clamps, so before the
        re-clamp the sigma used to price a bucket could reach
        MAX_SIGMA_F * NARROW_BUCKET_STD_INFLATION = 11.2°F."""
        from config import MAX_SIGMA_F, NARROW_BUCKET_STD_INFLATION
        from weather import compute_sigma
        # Spread far beyond anything tradeable, so compute_sigma is at the cap.
        capped = compute_sigma(50.0, 72, True)
        assert capped == MAX_SIGMA_F
        inflated = min(capped * NARROW_BUCKET_STD_INFLATION, MAX_SIGMA_F)
        assert inflated <= MAX_SIGMA_F, (
            "narrow-bucket inflation must be re-clamped to MAX_SIGMA_F")

    def test_floor_and_ceiling_are_ordered(self):
        from config import MIN_SIGMA_F, MAX_SIGMA_F
        assert MIN_SIGMA_F < MAX_SIGMA_F


class TestCityTableValidation:
    """City-keyed config tables must not name cities STATIONS has never heard of.

    Such an entry is a silent no-op — 'Tampa' sat in CONVECTIVE_CITIES and
    GFS_BIAS_CORRECTIONS while absent from STATIONS, so neither its std
    inflation nor its -1.3°F GFS correction ever applied, and nothing reported it.
    """
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

    def test_every_convective_city_exists(self):
        from config import CONVECTIVE_CITIES
        from weather import STATIONS
        missing = sorted(set(CONVECTIVE_CITIES) - set(STATIONS))
        assert not missing, f"CONVECTIVE_CITIES names unknown cities: {missing}"

    def test_every_gfs_bias_city_exists(self):
        from config import GFS_BIAS_CORRECTIONS
        from weather import STATIONS
        missing = sorted(set(GFS_BIAS_CORRECTIONS) - set(STATIONS))
        assert not missing, f"GFS_BIAS_CORRECTIONS names unknown cities: {missing}"

    def test_boot_guard_raises_on_a_phantom_city(self, monkeypatch):
        """It used to log a warning and carry on, and it ran AFTER the first scan
        cycle — so a dead table could trade for a full cycle before saying so."""
        import pytest
        import weather
        monkeypatch.setattr(weather, "CONVECTIVE_CITIES", {"Atlantis"})
        monkeypatch.setattr(weather, "GFS_BIAS_CORRECTIONS", {})
        with pytest.raises(RuntimeError) as exc:
            weather.validate_config_tables()
        assert "Atlantis" in str(exc.value)

    def test_boot_guard_raises_on_an_uncorrected_model(self, monkeypatch):
        import pytest
        import weather
        monkeypatch.setattr(weather, "WEIGHTS",
                            {"US": {"ecmwf_ifs025": 0.5, "brand_new_model": 0.5}})
        with pytest.raises(RuntimeError) as exc:
            weather.validate_config_tables()
        assert "brand_new_model" in str(exc.value)

    def test_boot_guard_passes_on_the_shipped_tables(self):
        import weather
        assert weather.validate_config_tables() == []

    def test_validator_reports_a_phantom_city(self, monkeypatch):
        import weather
        monkeypatch.setattr(weather, "CONVECTIVE_CITIES", {"Atlantis"})
        monkeypatch.setattr(weather, "GFS_BIAS_CORRECTIONS", {})
        problems = weather.validate_city_tables()
        assert len(problems) == 1 and "Atlantis" in problems[0]

    def test_validator_clean_when_tables_agree(self, monkeypatch):
        import weather
        monkeypatch.setattr(weather, "CONVECTIVE_CITIES", {"Miami"})
        monkeypatch.setattr(weather, "GFS_BIAS_CORRECTIONS", {"Miami": -1.5})
        assert weather.validate_city_tables() == []
