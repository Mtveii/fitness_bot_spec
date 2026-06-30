import pytest
import datetime
from bot.memory.profile_calculator import _weighted_avg_time, _find_busy_hours, _find_meal_clusters


class FakeObservation:
    def __init__(self, hour, minute, confidence=1.0):
        self.observed_at = datetime.datetime(2024, 1, 1, hour, minute)
        self.confidence = confidence


class TestTimeAveraging:
    def test_single_observation(self):
        obs = [FakeObservation(7, 30)]
        result = _weighted_avg_time(obs)
        assert result == "07:30"

    def test_multiple_observations_same_weight(self):
        obs = [FakeObservation(7, 0), FakeObservation(8, 0)]
        result = _weighted_avg_time(obs)
        assert result == "07:30"

    def test_weighted_average(self):
        obs = [
            FakeObservation(7, 0, 1.0),
            FakeObservation(9, 0, 0.5),
        ]
        result = _weighted_avg_time(obs)
        assert result == "07:40" or result == "07:39"

    def test_midnight_crossover(self):
        obs = [FakeObservation(23, 0), FakeObservation(1, 0)]
        result = _weighted_avg_time(obs)
        assert result is not None


class TestBusyHours:
    def test_busy_hours_low_activity(self):
        obs = []
        for h in range(24):
            for _ in range(20 if 8 <= h <= 22 else 1):
                obs.append(FakeObservation(h, 0))
        busy = _find_busy_hours(obs)
        assert len(busy) >= 4

    def test_busy_hours_empty(self):
        assert _find_busy_hours([]) == []


class TestMealClusters:
    def test_find_meal_clusters(self):
        obs = [FakeObservation(8, 0), FakeObservation(8, 30), FakeObservation(13, 0), FakeObservation(14, 0)]
        clusters = _find_meal_clusters(obs)
        assert "08:00" in clusters
