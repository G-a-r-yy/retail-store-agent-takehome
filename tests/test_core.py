from __future__ import annotations

import pytest

from db import current_qty, init_db
from tools import AmbiguousOrderError, AmbiguousProductError, InsufficientStockError, InvalidReturnError, RetailTools


@pytest.fixture()
def tools(tmp_path):
    conn = init_db(tmp_path / "store.db")
    return RetailTools(conn)


def test_default_db_is_fresh_in_memory_per_session():
    first = RetailTools(init_db())
    first.ring_up_sale(line_items=[{"product_descriptor": "Canvas Tote", "qty": 1}], customer_descriptor="walk-in")
    assert current_qty(first.conn, "TOTE") == 3

    second = RetailTools(init_db())
    assert current_qty(second.conn, "TOTE") == 4


def test_ring_up_tees_and_tote(tools):
    result = tools.ring_up_sale(
        line_items=[
            {"product_descriptor": "Classic Tee Blue Medium", "qty": 2},
            {"product_descriptor": "Canvas Tote", "qty": 1},
        ],
        customer_descriptor="walk-in",
        payment_method="cash",
        date="2026-06-19",
    )
    assert result["data"]["total"] == "68.00"
    assert current_qty(tools.conn, "TEE-BLU-M") == 20
    assert current_qty(tools.conn, "TOTE") == 3


def test_ring_up_sale_order_discount_is_separate_from_line_price(tools):
    result = tools.ring_up_sale(
        line_items=[{"product_descriptor": "Canvas Tote", "qty": 1}],
        customer_descriptor="walk-in",
        date="2026-06-19",
        order_discount_pct=10,
    )
    assert result["data"]["subtotal"] == "18.00"
    assert result["data"]["total"] == "16.20"
    assert result["data"]["lines"][0]["unit_price"] == "18.00"
    assert result["data"]["lines"][0]["paid_unit_price"] == "16.20"


def test_reject_insufficient_stock(tools):
    with pytest.raises(InsufficientStockError):
        tools.ring_up_sale(line_items=[{"product_descriptor": "Canvas Tote", "qty": 10}], customer_descriptor="walk-in")


def test_medium_hoodie_is_ambiguous_for_sarah(tools):
    with pytest.raises(AmbiguousProductError):
        tools.ring_up_sale(line_items=[{"product_descriptor": "hoodie medium", "qty": 1}], customer_descriptor="Sarah Chen")


def test_lookup_medium_hoodie_surfaces_both_in_stock_variants(tools):
    with pytest.raises(AmbiguousProductError) as exc:
        tools.lookup_product(query="a hoodie in medium")
    assert {item["sku"] for item in exc.value.candidates} == {"HOOD-GRY-M", "HOOD-NVY-M"}


def test_hoodie_without_size_or_color_surfaces_all_four_variants(tools):
    with pytest.raises(AmbiguousProductError) as exc:
        tools.lookup_product(query="hoodie")
    assert {item["sku"] for item in exc.value.candidates} == {"HOOD-GRY-M", "HOOD-GRY-L", "HOOD-NVY-M", "HOOD-NVY-L"}


def test_wool_socks_with_order_level_discount(tools):
    result = tools.ring_up_sale(
        line_items=[{"product_descriptor": "Wool Socks", "qty": 3}],
        customer_descriptor="Priya Patel",
        date="2026-06-19",
        order_discount_pct=15,
    )
    assert result["data"]["subtotal"] == "27.00"
    assert result["data"]["total"] == "22.95"
    assert result["data"]["lines"][0]["unit_price"] == "9.00"
    assert result["data"]["lines"][0]["paid_unit_price"] == "7.65"


def test_follow_up_quantity_correction_adjusts_last_single_line_sale(tools):
    sale = tools.ring_up_sale(
        line_items=[{"product_descriptor": "Wool Socks", "qty": 3}],
        customer_descriptor="Priya Patel",
        date="2026-06-19",
        order_discount_pct=15,
    )
    adjusted = tools.adjust_sale_quantity(order_id=sale["data"]["order_id"], qty=4)
    assert adjusted["data"]["quantity"] == 4
    assert adjusted["data"]["line_total"] == "30.60"
    assert current_qty(tools.conn, "SOCK") == 36


def test_quantity_correction_preserves_promo_price_and_order_discount(tools):
    tools.create_promotion("product", "hoodies", 20, "2026-06-20", "2026-06-22")
    sale = tools.ring_up_sale(
        line_items=[{"product_descriptor": "Gray Medium hoodie", "qty": 1}],
        date="2026-06-21",
        order_discount_pct=10,
    )
    assert sale["data"]["lines"][0]["unit_price"] == "48.00"
    assert sale["data"]["lines"][0]["paid_unit_price"] == "43.20"
    adjusted = tools.adjust_sale_quantity(order_id=sale["data"]["order_id"], qty=2)
    assert adjusted["data"]["line_total"] == "86.40"


def test_find_marcus_mug_return_without_order_but_condition_required(tools):
    result = tools.find_returnable_order_lines(customer_descriptor="Marcus Reed", product_descriptor="mug")
    assert result["data"]["condition_required"] is True
    assert result["data"]["candidates"] == [{"order_id": "O-1009", "line_no": 1, "sku": "MUG", "returnable_qty": 3, "refund_per_unit": "12.00"}]


def test_reorder_low_stock_best_supplier(tools):
    result = tools.reorder_low_stock(date="2026-06-19")
    assert result["data"]["items"] == [{"po_id": "PO-1", "sku": "TOTE", "qty": 50, "supplier_id": "SUP-NW", "unit_cost": "7.00"}]


def test_receive_synthesized_po(tools):
    before = current_qty(tools.conn, "TOTE")
    result = tools.receive_purchase_order(
        supplier_descriptor="Northwind",
        product_descriptor="Canvas Tote",
        qty_ordered=50,
        qty_received=40,
        date="2026-06-19",
    )
    assert result["data"]["synthesized"] is True
    assert result["data"]["status"] == "partial"
    assert current_qty(tools.conn, "TOTE") == before + 40


def test_receive_complete_open_tote_po_from_northwind(tools):
    tools.reorder_low_stock(date="2026-06-19")
    before = current_qty(tools.conn, "TOTE")
    result = tools.receive_purchase_order(
        supplier_descriptor="Northwind",
        product_descriptor="Canvas Tote",
        receive_all=True,
        date="2026-06-19",
    )
    assert result["data"]["qty_received"] == 50
    assert result["data"]["status"] == "received/closed"
    assert current_qty(tools.conn, "TOTE") == before + 50


def test_receive_complete_fails_when_multiple_open_pos_match(tools):
    tools.reorder_low_stock(date="2026-06-19")
    tools.receive_purchase_order(
        supplier_descriptor="Northwind",
        product_descriptor="Canvas Tote",
        qty_ordered=50,
        qty_received=1,
        date="2026-06-19",
    )
    tools.reorder_low_stock(date="2026-06-19")
    with pytest.raises(AmbiguousOrderError):
        tools.receive_purchase_order(
            supplier_descriptor="Northwind",
            product_descriptor="Canvas Tote",
            receive_all=True,
            date="2026-06-19",
        )


def test_good_return_restocks(tools):
    before = current_qty(tools.conn, "HOOD-NVY-L")
    result = tools.process_return(order_id="O-1006", product_descriptor="Navy Large hoodie", qty=1, condition="good")
    assert result["data"]["refund_amount"] == "54.00"
    assert result["data"]["restocked"] is True
    assert current_qty(tools.conn, "HOOD-NVY-L") == before + 1


def test_o1006_navy_large_hoodie_only_one_remains_returnable(tools):
    tools.process_return(order_id="O-1006", product_descriptor="Navy Large hoodie", qty=1, condition="good")
    with pytest.raises(InvalidReturnError):
        tools.process_return(order_id="O-1006", product_descriptor="Navy Large hoodie", qty=1, condition="good")


def test_damaged_return_does_not_restock(tools):
    before = current_qty(tools.conn, "TOTE")
    result = tools.process_return(order_id="O-1006", product_descriptor="Canvas Tote", qty=1, condition="damaged")
    assert result["data"]["refund_amount"] == "16.20"
    assert result["data"]["restocked"] is False
    assert current_qty(tools.conn, "TOTE") == before


def test_hoodie_promotion_window(tools):
    tools.create_promotion("product", "Gray Medium hoodie", 20, "2026-06-20", "2026-06-22")
    inside = tools.ring_up_sale(line_items=[{"product_descriptor": "Gray Medium hoodie", "qty": 1}], date="2026-06-21")
    outside = tools.ring_up_sale(line_items=[{"product_descriptor": "Gray Medium hoodie", "qty": 1}], date="2026-06-23")
    assert inside["data"]["lines"][0]["unit_price"] == "48.00"
    assert outside["data"]["lines"][0]["unit_price"] == "60.00"


def test_hoodie_promotion_maps_to_product_not_apparel_category(tools):
    promo = tools.create_promotion("product", "hoodies", 20, "2026-06-20", "2026-06-22")
    assert promo["data"]["scope_ref"] == "P-HOOD"
    hoodie = tools.ring_up_sale(line_items=[{"product_descriptor": "Gray Medium hoodie", "qty": 1}], date="2026-06-21")
    tee = tools.ring_up_sale(line_items=[{"product_descriptor": "Classic Tee Blue Medium", "qty": 1}], date="2026-06-21")
    assert hoodie["data"]["lines"][0]["unit_price"] == "48.00"
    assert tee["data"]["lines"][0]["unit_price"] == "25.00"


def test_goods_category_promotion_starting_tomorrow_for_a_week(tools):
    promo = tools.create_promotion("category", "goods", 10, "2026-06-20", "2026-06-27")
    assert promo["data"]["scope_type"] == "category"
    assert promo["data"]["scope_ref"] == "goods"
    tote = tools.ring_up_sale(line_items=[{"product_descriptor": "Canvas Tote", "qty": 1}], date="2026-06-20")
    tee = tools.ring_up_sale(line_items=[{"product_descriptor": "Classic Tee Blue Medium", "qty": 1}], date="2026-06-20")
    assert tote["data"]["lines"][0]["unit_price"] == "16.20"
    assert tee["data"]["lines"][0]["unit_price"] == "25.00"


def test_net_revenue_in_may_after_refunds(tools):
    result = tools.get_net_revenue("May 2026")
    assert result["data"]["gross_revenue"] == "1786.20"
    assert result["data"]["refunds"] == "54.00"
    assert result["data"]["net_revenue"] == "1732.20"
    assert tools.get_top_products_by_margin("May 2026", 5)["data"]["items"][0]["margin"] == "420.00"


def test_backdated_tee_sale_uses_spring_promotion(tools):
    result = tools.ring_up_sale(line_items=[{"product_descriptor": "Classic Tee Blue Small", "qty": 2}], date="May 3rd, 2026")
    assert result["data"]["lines"][0]["unit_price"] == "20.00"
    assert result["data"]["total"] == "40.00"


def test_top_five_products_by_margin(tools):
    result = tools.get_top_products_by_margin("last month", 5)
    items = result["data"]["items"]
    assert [(item["product_name"], item["margin"]) for item in items] == [
        ("Classic Tee", "420.00"),
        ("Pullover Hoodie", "282.00"),
        ("Wool Socks", "120.00"),
        ("Canvas Tote", "108.20"),
        ("Ceramic Mug", "70.00"),
    ]


def test_stockout_risk_is_distinct_from_reorder(tools):
    result = tools.get_stockout_risk()
    tote = next(item for item in result["data"]["items"] if item["product_id"] == "P-TOTE")
    assert tote["on_hand"] == 4
    assert tote["monthly_units"] == 10
    assert tote["days_of_cover"] == "12.0"
    assert tote["reorder_trigger_skus"] == ["TOTE"]
    assert tote["reorder_risk"] is True


def test_gray_hoodie_inventory_summary_and_product_risk(tools):
    result = tools.check_inventory_summary("Gray Hoodies")
    hoodie_risk = tools._product_stockout_metrics("P-HOOD", 14)
    assert set(result["data"]["matched_skus"]) == {"HOOD-GRY-M", "HOOD-GRY-L"}
    assert result["data"]["current_qty"] == 16
    assert result["data"]["product_on_hand"] == 30
    assert result["data"]["monthly_units"] == 10
    assert result["data"]["days_of_cover"] == "90.0"
    assert result["data"]["at_risk"] is False
    assert result["data"]["product_on_hand"] == hoodie_risk["product_on_hand"]
    assert result["data"]["monthly_units"] == hoodie_risk["monthly_units"]
    assert result["data"]["days_of_cover"] == hoodie_risk["days_of_cover"]
    assert result["data"]["at_risk"] == hoodie_risk["at_risk"]
