import json
from pathlib import Path
from synthlab.tax_intake import preview_tax

def test_fictional_tax_preview():
    data=json.loads((Path(__file__).parents[1]/"fixtures/tax/intake.json").read_text())
    result=preview_tax(data["orders"],data["items"],data["rates"])
    assert [row["tax"] for row in result] == ["4.00","3.50","0.40"]
