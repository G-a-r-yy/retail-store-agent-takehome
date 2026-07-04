from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping, Optional


CENT = Decimal("0.01")
TODAY = date(2026, 6, 19)


def money(value: object) -> Decimal:
    return Decimal(str(value)).quantize(CENT)


def discounted_unit_price(unit_price: Decimal, order_discount_pct: Decimal) -> Decimal:
    """Apply order-level discount proration. DATA_DICTIONARY.md Business rules #2."""
    factor = Decimal("1") - (Decimal(order_discount_pct) / Decimal("100"))
    return (Decimal(unit_price) * factor).quantize(CENT, rounding=ROUND_HALF_UP)


def refund_amount(unit_price: Decimal, order_discount_pct: Decimal, qty: int) -> Decimal:
    """Refund price actually paid, never current/list price. DATA_DICTIONARY.md Business rules #3."""
    return (discounted_unit_price(unit_price, order_discount_pct) * qty).quantize(CENT)


def should_restock_return(condition: str) -> bool:
    """Good returns restock; damaged returns do not. DATA_DICTIONARY.md Business rules #3."""
    return condition.lower() == "good"


def promotion_applies(promo: Mapping[str, object], product: Mapping[str, object], sale_date: date) -> bool:
    """Promotion date window is inclusive and scoped by product/category. DATA_DICTIONARY.md Business rules #5."""
    start = date.fromisoformat(str(promo["start_date"]))
    end = date.fromisoformat(str(promo["end_date"]))
    if not (start <= sale_date <= end):
        return False
    scope_type = str(promo["scope_type"])
    scope_ref = str(promo["scope_ref"])
    if scope_type == "product":
        return scope_ref == product["product_id"]
    if scope_type == "category":
        return scope_ref == product["category"]
    return False


def promotional_unit_price(retail_price: Decimal, promotions: Iterable[Mapping[str, object]]) -> tuple[Decimal, Optional[str]]:
    """Choose the single promotion giving the lower price; no stacking. DATA_DICTIONARY.md Business rules #5."""
    best = money(retail_price)
    best_id: Optional[str] = None
    for promo in promotions:
        if promo["type"] != "percent_off":
            continue
        candidate = (money(retail_price) * (Decimal("1") - Decimal(str(promo["value"])) / Decimal("100"))).quantize(CENT)
        if candidate < best:
            best = candidate
            best_id = str(promo["promo_id"])
    return best, best_id


def line_revenue(unit_price: Decimal, order_discount_pct: Decimal, qty: int) -> Decimal:
    """Revenue is actual dollars paid after order-level discount. DATA_DICTIONARY.md Business rules #6."""
    return (discounted_unit_price(unit_price, order_discount_pct) * qty).quantize(CENT)


def product_margin(revenue: Decimal, unit_cost: Decimal, units_stayed_sold: int) -> Decimal:
    """Margin = revenue from units minus cost of units that stayed sold. DATA_DICTIONARY.md Business rules #6."""
    return (money(revenue) - money(unit_cost) * units_stayed_sold).quantize(CENT)


def rank_suppliers(rows: list[Mapping[str, object]]) -> Optional[Mapping[str, object]]:
    """Pick the lowest unit_cost supplier with lead_time_days <= 10. DATA_DICTIONARY.md Business rules #4."""
    eligible = [r for r in rows if int(r["lead_time_days"]) <= 10]
    if not eligible:
        return None
    return sorted(eligible, key=lambda r: (money(r["unit_cost"]), int(r["lead_time_days"]), str(r["supplier_id"])))[0]


def below_reorder_point(current_qty: int, reorder_point: int) -> bool:
    """Reorder workflow uses variants below reorder point. WRITEUP.md Step-0 assumption: tool uses <, not <=, per user request."""
    return current_qty < reorder_point


def days_of_cover(on_hand_qty: int, monthly_units: int) -> Optional[Decimal]:
    """Days cover = on_hand_qty / (monthly_units / 30). DATA_DICTIONARY.md Business rules #7."""
    if monthly_units <= 0:
        return None
    return (Decimal(on_hand_qty) / (Decimal(monthly_units) / Decimal("30"))).quantize(Decimal("0.1"))


def is_stockout_risk(on_hand_qty: int, reorder_point: int, monthly_units: int, horizon_days: int = 14) -> bool:
    """Flag if at/below reorder point or fewer than horizon days of cover. DATA_DICTIONARY.md Business rules #7."""
    cover = days_of_cover(on_hand_qty, monthly_units)
    return on_hand_qty <= reorder_point or (cover is not None and cover < Decimal(horizon_days))
