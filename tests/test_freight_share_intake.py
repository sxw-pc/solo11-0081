import json
from pathlib import Path
from synthlab.freight_share_intake import solo_quotes
from synthlab.engine import generate_dataset
from synthlab.spec.loader import load_dataset


def test_shipments_reference_generated_skus():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/freight_share/intake.json").read_text())
    rows=generate_dataset(load_dataset(root/"fixtures/demo/dataset.yaml")).tables["products"].rows
    assert {m["sku_id"] for m in data["merchants"]}<={r[0] for r in rows}


def test_solo_quotes_miss_shared_vehicle_savings():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/freight_share/intake.json").read_text())
    assert solo_quotes(data)=={"m-A":20,"m-B":18,"m-C":16}
    assert sum(solo_quotes(data).values())==54
    dist=data["distance"]
    ab=dist["D"]["A"]+dist["A"]["B"]+dist["B"]["D"]
    c=dist["D"]["C"]+dist["C"]["D"]
    assert ab+c==37
