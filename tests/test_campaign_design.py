import json
from pathlib import Path
from synthlab.campaign_design import potential_exposure, potential_conversion

def test_synthetic_trial_has_monotone_exposure_and_exclusion():
    fixture = json.loads((Path(__file__).parents[1]/"fixtures/campaign/trial.json").read_text())
    for row in fixture["events"]:
        i = int(row["customer_id"].split("-")[1])
        assert potential_exposure(i, 1) >= potential_exposure(i, 0)
        assert row["exposed"] == potential_exposure(i, row["assigned"])
        if row["outcome_observed"]:
            assert row["converted"] == potential_conversion(i, row["exposed"])
