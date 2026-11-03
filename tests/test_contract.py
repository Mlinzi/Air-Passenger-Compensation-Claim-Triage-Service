from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from triage.common.contract import ClaimRequest, Disruption


def valid_claim(**overrides):
    claim = {
        "pnr": "ABC123",
        "passenger_name": "Asha Rao",
        "carrier_code": "AI",
        "flight_number": "AI 101",
        "origin": "DEL",
        "destination": "BOM",
        "scheduled_departure": datetime(2025, 1, 1, 10, tzinfo=timezone.utc),
        "description": "My flight was cancelled and I need help with my claim.",
    }
    return ClaimRequest.model_validate(claim | overrides)


def test_valid_claim_strips_strings_and_applies_defaults():
    claim = valid_claim(passenger_name="  Asha Rao  ")

    assert claim.passenger_name == "Asha Rao"
    assert claim.international is False
    assert claim.passenger_disputes_cause is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"pnr": "bad"},
        {"destination": "DEL"},
        {"alternate_flight_taken": True, "alternate_flight_offered": False},
        {"actual_departure": datetime(2025, 1, 1, 9, tzinfo=timezone.utc)},
        {"disruption_type": Disruption.delay},
    ],
)
def test_invalid_claims_fail_validation(overrides):
    with pytest.raises(ValidationError):
        valid_claim(**overrides)


def test_downgrade_requires_lower_flown_cabin():
    with pytest.raises(ValidationError, match="flown_class"):
        valid_claim(disruption_type=Disruption.downgrading, booked_class="business", flown_class="business")


def test_alternate_departure_must_be_after_schedule():
    with pytest.raises(ValidationError, match="alternate_flight_departure"):
        valid_claim(alternate_flight_departure=datetime(2025, 1, 1, 10, tzinfo=timezone.utc))

