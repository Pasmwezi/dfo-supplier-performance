from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Iterable, Mapping

CONSTRUCTION_CRITERIA = [
    "Quality of Workmanship",
    "Time",
    "Project Management",
    "Contract Management",
    "Health and Safety",
]
CONSTRUCTION_OPTIONAL_CRITERIA = {"Project Management", "Contract Management"}

AE_DEFAULT_WEIGHTS = {
    "Quality of Design": 15,
    "Quality of Deliverables": 15,
    "Technical Competence": 15,
    "Contract Administration": 15,
    "Project Management": 15,
    "Schedule Compliance": 10,
    "Cost Control": 10,
    "Client Service": 5,
}

AE_CPERF_WEIGHTS = {
    "Design": 20,
    "Quality of Results": 20,
    "Management": 20,
    "Time": 20,
    "Cost": 20,
}
AE_CPERF_OPTIONAL_CRITERIA = {"Design", "Management", "Cost"}

RATING_BANDS = [
    (0, 5, "Unacceptable"),
    (6, 10, "Not Satisfactory"),
    (11, 16, "Satisfactory"),
    (17, 20, "Superior"),
]


def rating_label(score: float) -> str:
    if not 0 <= score <= 20:
        raise ValueError("Each criterion score must be between 0 and 20.")
    if float(score) != int(score):
        raise ValueError("CPERF criterion scores must be whole numbers.")
    for low, high, label in RATING_BANDS:
        if low <= score <= high:
            return label
    raise ValueError("Scores must use whole-number CPERF scale values.")


def outcome_for_percentage(percentage: float, criterion_scores: Iterable[float]) -> tuple[str, bool]:
    floor_triggered = 30 <= percentage <= 50 and any(score <= 5 for score in criterion_scores)
    if percentage >= 85:
        return "CONGRATULATIONS", False
    if percentage >= 51:
        return "MEETS_EXPECTATIONS", False
    if percentage >= 30 and not floor_triggered:
        return "WARNING", False
    return "SUSPENSION_RECOMMENDATION", floor_triggered


def construction_result(scores: list[float | None]) -> dict:
    if len(scores) != 5:
        raise ValueError("Construction evaluations require exactly five criteria.")
    applicable_scores = [score for score in scores if score is not None]
    if not applicable_scores:
        raise ValueError("At least one construction criterion must be applicable.")
    for score in applicable_scores:
        rating_label(score)
    total = round(sum(applicable_scores), 2)
    max_applicable_points = len(applicable_scores) * 20
    percentage = round(total / max_applicable_points * 100, 2)
    outcome, floor = outcome_for_percentage(percentage, applicable_scores)
    return {
        "total": total,
        "max_applicable_points": max_applicable_points,
        "percentage": percentage,
        "outcome": outcome,
        "criterion_floor_triggered": floor,
        "ratings": [rating_label(score) if score is not None else "Not Applicable" for score in scores],
    }


def validate_weights(weights: Mapping[str, float]) -> None:
    if not weights:
        raise ValueError("At least one A&E criterion is required.")
    if any(weight <= 0 for weight in weights.values()):
        raise ValueError("Every criterion weighting must be greater than zero.")
    if round(sum(weights.values()), 4) != 100:
        raise ValueError("A&E criterion weightings must total exactly 100%.")


def ae_result(scores: dict[str, float | None], weights: Mapping[str, float]) -> dict:
    validate_weights(weights)
    if set(scores) != set(weights):
        raise ValueError("A score is required for every weighted A&E criterion, with no extra criteria.")
    applicable_names: list[str] = []
    applicable_scores: list[float] = []
    for name, score in scores.items():
        if score is None:
            continue
        rating_label(score)
        applicable_names.append(name)
        applicable_scores.append(score)
    if not applicable_names:
        raise ValueError("At least one A&E criterion must be applicable.")
    applicable_weight_total = sum(weights[name] for name in applicable_names)
    weighted_points = sum((score / 20) * weights[name] for name, score in zip(applicable_names, applicable_scores))
    percentage = round(weighted_points / applicable_weight_total * 100, 2)
    outcome, floor = outcome_for_percentage(percentage, applicable_scores)
    return {
        "total": percentage,
        "percentage": percentage,
        "applicable_weight_total": applicable_weight_total,
        "outcome": outcome,
        "criterion_floor_triggered": floor,
        "ratings": {name: rating_label(score) if score is not None else "Not Applicable" for name, score in scores.items()},
    }


def supplier_risk_profile(evaluations: list[dict]) -> dict:
    if not evaluations:
        return {
            "average_score": None,
            "trend_score": None,
            "risk_rating": "NOT_RATED",
            "warning_status": False,
            "suspension_recommendation_status": False,
            "eligibility_status": "ELIGIBLE - NO PERFORMANCE HISTORY",
            "flags": [],
        }
    ordered = sorted(evaluations, key=lambda item: item.get("date") or date.min.isoformat())
    scores = [float(item["score"]) for item in ordered]
    issues = Counter(issue.lower() for item in ordered for issue in item.get("issues", []))
    outcomes = [item.get("outcome") for item in ordered]
    flags: list[str] = []
    low_dates = [date.fromisoformat(str(item.get("date"))[:10]) for item in ordered if float(item["score"]) <= 50 and item.get("date")]
    repeated_low_within_two_years = any((later - earlier).days <= 730 for i, earlier in enumerate(low_dates) for later in low_dates[i + 1:])
    if repeated_low_within_two_years:
        flags.append("REPEATED_LOW_EVALUATIONS_WITHIN_TWO_YEARS")
    issue_flags = {
        "schedule": "REPEATED_SCHEDULE_ISSUES",
        "cost control": "REPEATED_COST_CONTROL_ISSUES",
        "contract administration": "REPEATED_CONTRACT_ADMINISTRATION_DEFICIENCIES",
    }
    for issue, flag in issue_flags.items():
        if issues[issue] >= 2:
            flags.append(flag)
    if issues["safety"] >= 1:
        flags.append("SAFETY_DEFICIENCY")
    trend = round(scores[-1] - scores[0], 1) if len(scores) > 1 else 0.0
    if trend <= -10:
        flags.append("DECLINING_TREND")
    average = round(sum(scores) / len(scores), 1)
    suspension = "SUSPENSION_RECOMMENDATION" in outcomes
    warning = suspension or "WARNING" in outcomes
    if suspension or "REPEATED_LOW_EVALUATIONS_WITHIN_TWO_YEARS" in flags or average < 51:
        risk = "HIGH"
    elif warning or trend <= -10 or average < 70:
        risk = "MEDIUM"
    else:
        risk = "LOW"
    return {
        "average_score": average,
        "trend_score": trend,
        "risk_rating": risk,
        "warning_status": warning,
        "suspension_recommendation_status": suspension,
        "eligibility_status": "REQUIRES AUTHORIZED REVIEW" if suspension else ("ELIGIBLE WITH CAUTION" if warning else "ELIGIBLE"),
        "flags": flags,
    }
