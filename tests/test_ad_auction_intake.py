import json
from pathlib import Path
from synthlab.ad_auction_intake import atomic_preview
from synthlab.engine import generate_dataset
from synthlab.spec.loader import load_dataset


def test_auction_product_is_generated_catalog_sku():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/ad_auction/intake.json").read_text())
    products=generate_dataset(load_dataset(root/"fixtures/demo/dataset.yaml")).tables["products"].rows
    assert data["sku_id"] in {row[0] for row in products}


def test_atomic_preview_excludes_high_value_bundle():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/ad_auction/intake.json").read_text())
    assert atomic_preview(data)==["b6","b4","b3"]
    by_id={b["bid_id"]:b for b in data["bids"]}
    assert sum(by_id[i]["value"] for i in atomic_preview(data))==24
    assert by_id["b1"]["value"]+by_id["b4"]["value"]==25
