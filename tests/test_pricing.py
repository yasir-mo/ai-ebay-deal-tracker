import pytest

from tracker.models import ConditionBucket, Profile
from tracker.pricing import MIN_SAMPLES, compute_baseline

PROFILE = Profile(
    id="a7iii",
    name="Sony A7 III",
    query="sony a7 iii",
    ceiling_pence=150_000,
    target_pence=100_000,
)

NO_TARGET = Profile(
    id="rtx", name="RTX 4090", query="rtx 4090", ceiling_pence=200_000
)


class TestColdStart:
    def test_falls_back_to_manual_target_when_data_is_thin(self):
        b = compute_baseline(PROFILE, ConditionBucket.USED, [80_000] * 3)
        assert b.median_pence == 100_000
        assert b.provisional is True
        assert b.sample_n == 3

    def test_no_data_and_no_target_means_no_baseline(self):
        """Better to decline to judge than to invent a number."""
        assert compute_baseline(NO_TARGET, ConditionBucket.USED, []) is None

    def test_thin_data_and_no_target_means_no_baseline(self):
        assert compute_baseline(NO_TARGET, ConditionBucket.USED, [1, 2, 3]) is None


class TestRealBaseline:
    def test_uses_observations_once_sample_is_big_enough(self):
        sample = [100_000] * MIN_SAMPLES
        b = compute_baseline(PROFILE, ConditionBucket.USED, sample)
        assert b.provisional is False
        assert b.median_pence == 100_000
        assert b.sample_n == MIN_SAMPLES

    def test_real_data_overrides_the_manual_target(self):
        sample = [50_000] * MIN_SAMPLES
        b = compute_baseline(PROFILE, ConditionBucket.USED, sample)
        assert b.median_pence == 50_000

    def test_median_not_mean(self):
        sample = [10_000] * 10 + [11_000] * 10 + [900_000]
        b = compute_baseline(PROFILE, ConditionBucket.USED, sample)
        assert b.median_pence < 12_000

    def test_trimming_discards_both_tails(self):
        """One 99p scam and one delusional price must not move the median."""
        clean = list(range(90_000, 110_000, 1_000)) * 2
        polluted = clean + [99, 5_000_000]
        assert (
            compute_baseline(PROFILE, ConditionBucket.USED, polluted).median_pence
            == pytest.approx(
                compute_baseline(PROFILE, ConditionBucket.USED, clean).median_pence,
                rel=0.05,
            )
        )

    def test_p25_is_below_median(self):
        sample = list(range(50_000, 150_000, 2_000))
        b = compute_baseline(PROFILE, ConditionBucket.USED, sample)
        assert b.p25_pence < b.median_pence

    def test_bucket_is_carried_through(self):
        b = compute_baseline(PROFILE, ConditionBucket.REFURB, [100_000] * MIN_SAMPLES)
        assert b.bucket is ConditionBucket.REFURB
        assert b.profile_id == "a7iii"
