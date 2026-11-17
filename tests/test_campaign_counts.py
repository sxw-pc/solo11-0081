import json
from pathlib import Path

def test_compact_trial_counts_match_events():
    root = Path(__file__).parents[1] / "fixtures/campaign"
    events = json.loads((root/"trial.json").read_text())["events"]
    summary = json.loads((root/"trial_counts.json").read_text())
    for assignment, key in ((0, "control"), (1, "offer")):
        rows = [r for r in events if r["assigned"] == assignment]
        expected = summary["assignment"][key]
        assert len(rows) == expected["total"]
        assert sum(r["exposed"] for r in rows) == expected["exposed"]
        assert sum(r["outcome_observed"] for r in rows) == expected["outcome_observed"]
        assert sum(int(r["converted"] or 0) for r in rows) == expected["converted_observed"]
