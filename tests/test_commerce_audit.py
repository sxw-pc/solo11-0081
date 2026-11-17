from synthlab.commerce_audit import audit_commerce_rows, audit_generated_commerce


def test_current_demo_has_cross_table_economic_mismatches(demo_data):
    result = audit_generated_commerce(demo_data)
    assert result["status"] == "inconsistent"
    assert result["counts"]["line_amount"] == 360
    assert result["counts"]["product_price"] == 360
    assert result["counts"]["order_total"] == 160
    assert result["counts"]["stock_overuse"] > 0


def test_small_coherent_case_passes():
    result = audit_commerce_rows(
        [{"product_id": "P1", "unit_price": "10.00", "stock_quantity": 2}],
        [{"order_id": "O1", "status": "paid", "total_amount": "10.00", "item_count": 1}],
        [{"item_id": "I1", "order_id": "O1", "product_id": "P1", "quantity": 1,
          "unit_price": "10.00", "discount_pct": "0", "line_total": "10.00"}],
    )
    assert result["status"] == "coherent"
    assert all(count == 0 for count in result["counts"].values())
