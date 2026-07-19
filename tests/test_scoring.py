import pytest

from app.domain import (
    AE_DEFAULT_WEIGHTS,
    construction_result,
    ae_result,
    validate_weights,
    supplier_risk_profile,
)


def test_construction_thresholds_and_letters():
    assert construction_result([17, 17, 17, 17, 17])["outcome"] == "CONGRATULATIONS"
    assert construction_result([16, 16, 16, 16, 16])["outcome"] == "MEETS_EXPECTATIONS"
    assert construction_result([10, 10, 10, 10, 10])["outcome"] == "WARNING"
    assert construction_result([5, 5, 5, 5, 5])["outcome"] == "SUSPENSION_RECOMMENDATION"


def test_construction_optional_criteria_are_excluded_and_score_is_normalized():
    result = construction_result([15, 15, None, None, 15])
    assert result["total"] == 45
    assert result["max_applicable_points"] == 60
    assert result["percentage"] == 75.0
    assert result["ratings"] == ["Satisfactory", "Satisfactory", "Not Applicable", "Not Applicable", "Satisfactory"]


def test_ae_cperf_optional_criteria_are_excluded_and_weights_are_normalized():
    scores = {"Design": None, "Quality of Results": 16, "Management": None, "Time": 14, "Cost": None}
    weights = {name: 20 for name in scores}
    result = ae_result(scores, weights)
    assert result["applicable_weight_total"] == 40
    assert result["percentage"] == 75.0
    assert result["ratings"]["Design"] == "Not Applicable"


def test_construction_additional_suspension_rule():
    result = construction_result([5, 11, 11, 11, 11])
    assert result["total"] == 49
    assert result["outcome"] == "SUSPENSION_RECOMMENDATION"
    assert result["criterion_floor_triggered"] is True


def test_construction_rejects_out_of_range_and_wrong_count():
    with pytest.raises(ValueError):
        construction_result([20, 20])
    with pytest.raises(ValueError):
        construction_result([21, 20, 20, 20, 20])
    with pytest.raises(ValueError):
        construction_result([4.5, 20, 20, 20, 20])


def test_ae_weights_total_exactly_100():
    assert sum(AE_DEFAULT_WEIGHTS.values()) == 100
    validate_weights(AE_DEFAULT_WEIGHTS)
    with pytest.raises(ValueError):
        validate_weights({"Design": 50, "Schedule": 49})


def test_ae_weighted_score_is_normalized_and_uses_construction_threshold_logic():
    scores = {criterion: 17 for criterion in AE_DEFAULT_WEIGHTS}
    result = ae_result(scores, AE_DEFAULT_WEIGHTS)
    assert result["percentage"] == 85.0
    assert result["outcome"] == "CONGRATULATIONS"


def test_risk_profile_flags_repeated_low_and_declining_performance():
    evaluations = [
        {"score": 75, "date": "2025-01-01", "issues": []},
        {"score": 48, "date": "2025-06-01", "issues": ["schedule"]},
        {"score": 42, "date": "2026-01-01", "issues": ["schedule", "safety"]},
    ]
    risk = supplier_risk_profile(evaluations)
    assert risk["average_score"] == 55.0
    assert risk["trend_score"] == -33.0
    assert risk["risk_rating"] == "HIGH"
    assert "REPEATED_LOW_EVALUATIONS_WITHIN_TWO_YEARS" in risk["flags"]
    assert "REPEATED_SCHEDULE_ISSUES" in risk["flags"]
    assert "SAFETY_DEFICIENCY" in risk["flags"]
    assert "DECLINING_TREND" in risk["flags"]
