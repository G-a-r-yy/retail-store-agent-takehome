from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
import sqlite3
from typing import Any, Optional

import business_rules as br
from db import current_qty, next_id, transaction
from models import (
    CheckInventoryInput,
    CreatePromotionInput,
    AdjustSaleQuantityInput,
    FindReturnableOrderLinesInput,
    InventorySummaryInput,
    NetRevenueInput,
    ProcessReturnInput,
    ReceivePurchaseOrderInput,
    ReorderLowStockInput,
    RingUpSaleInput,
    StockoutRiskInput,
    TopProductsByMarginInput,
)


class ToolError(Exception):
    pass


class AmbiguousProductError(ToolError):
    def __init__(self, candidates: list[dict[str, Any]]):
        super().__init__("Product description is ambiguous.")
        self.candidates = candidates


class AmbiguousOrderError(ToolError):
    pass


class InsufficientStockError(ToolError):
    def __init__(self, sku: str, requested: int, available: int):
        super().__init__(f"Insufficient stock for {sku}: requested {requested}, available {available}.")
        self.sku = sku
        self.requested = requested
        self.available = available


class UnknownCustomerError(ToolError):
    pass


class InvalidReturnError(ToolError):
    pass


class UnknownPurchaseOrderError(ToolError):
    pass


class RetailTools:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def lookup_product(self, query: Optional[str] = None, category: Optional[str] = None, color: Optional[str] = None, size: Optional[str] = None, sku: Optional[str] = None) -> dict[str, Any]:
        matches = self._product_candidates(query=query, category=category, color=color, size=size, sku=sku)
        if len(matches) == 1:
            return {"ok": True, "message": "Product resolved.", "data": matches[0]}
        if not matches:
            raise AmbiguousProductError([])
        raise AmbiguousProductError(matches)

    def lookup_customer(self, name: Optional[str] = None, customer_id: Optional[str] = None) -> dict[str, Any]:
        customer = self._resolve_customer(name, customer_id, allow_walk_in=True)
        if customer is None:
            return {"ok": True, "message": "Walk-in customer.", "data": {"customer_id": None}}
        return {"ok": True, "message": "Customer resolved.", "data": dict(customer)}

    def ring_up_sale(self, line_items: list[dict[str, Any]], customer_descriptor: Optional[str] = None, payment_method: Optional[str] = None, date: Optional[str] = None, order_discount_pct: int = 0) -> dict[str, Any]:
        inp = RingUpSaleInput(line_items=line_items, customer_descriptor=customer_descriptor, payment_method=payment_method, date=date, order_discount_pct=order_discount_pct)
        sale_date = self._date(inp.date)
        payment = inp.payment_method or "unspecified"
        customer = self._resolve_customer(inp.customer_descriptor, None, allow_walk_in=True)
        customer_id = customer["customer_id"] if customer else None
        order_discount = Decimal(inp.order_discount_pct)
        resolved_lines = []
        totals = {"subtotal": Decimal("0"), "total": Decimal("0"), "margin": Decimal("0")}
        for item in inp.line_items:
            product = self.lookup_product(query=item.product_descriptor)["data"]
            qty = item.qty
            available = current_qty(self.conn, product["sku"])
            if available < qty:
                raise InsufficientStockError(product["sku"], qty, available)
            active = self._active_promos(product, sale_date)
            unit_price, promo_id = br.promotional_unit_price(br.money(product["retail_price"]), active)
            cost = self._frozen_cogs_cost(product["product_id"])
            revenue = br.line_revenue(unit_price, order_discount, qty)
            margin = br.product_margin(revenue, cost, qty)
            totals["subtotal"] += unit_price * qty
            totals["total"] += revenue
            totals["margin"] += margin
            paid_unit_price = br.discounted_unit_price(unit_price, order_discount)
            resolved_lines.append({"product": product, "qty": qty, "unit_price": unit_price, "paid_unit_price": paid_unit_price, "promo_id": promo_id, "line_total": revenue, "margin": margin})
        with transaction(self.conn):
            order_id = next_id(self.conn, "orders", "order_id", "O-")
            self.conn.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", (order_id, sale_date.isoformat(), customer_id, str(inp.order_discount_pct), payment))
            for idx, line in enumerate(resolved_lines, start=1):
                sku = line["product"]["sku"]
                self.conn.execute("INSERT INTO order_lines VALUES (?, ?, ?, ?, ?, ?)", (order_id, idx, sku, line["qty"], str(line["unit_price"]), line["promo_id"]))
                self.conn.execute("INSERT INTO inventory_movements(sku, delta, reason, reference_id, timestamp) VALUES (?, ?, ?, ?, ?)", (sku, -line["qty"], "sale", order_id, sale_date.isoformat()))
        lines = [
            {
                "sku": l["product"]["sku"],
                "product_name": l["product"]["product_name"],
                "qty": l["qty"],
                "unit_price": str(l["unit_price"]),
                "paid_unit_price": str(l["paid_unit_price"]),
                "promo_id": l["promo_id"],
                "line_total": str(l["line_total"]),
                "margin": str(l["margin"]),
            }
            for l in resolved_lines
        ]
        msg = f"Created order {order_id} for {len(lines)} line(s), total ${totals['total']:.2f}."
        if inp.payment_method is None:
            msg += " Payment method defaulted to unspecified."
        return {"ok": True, "message": msg, "data": {"order_id": order_id, "customer_id": customer_id, "date": sale_date.isoformat(), "payment_method": payment, "order_discount_pct": inp.order_discount_pct, "lines": lines, "subtotal": str(totals["subtotal"]), "total": str(totals["total"]), "margin": str(totals["margin"])}}

    def process_return(self, order_id: Optional[str] = None, order_line_id: Optional[str] = None, product_descriptor: Optional[str] = None, qty: int = 1, condition: str = "good", date: Optional[str] = None) -> dict[str, Any]:
        inp = ProcessReturnInput(order_id=order_id, order_line_id=order_line_id, product_descriptor=product_descriptor, qty=qty, condition=condition, date=date)
        return_date = self._date(inp.date)
        line = self._resolve_order_line(inp.order_id, inp.order_line_id, inp.product_descriptor)
        already = self.conn.execute("SELECT COALESCE(SUM(quantity),0) AS qty FROM returns WHERE order_id=? AND sku=?", (line["order_id"], line["sku"])).fetchone()["qty"]
        if int(already) + inp.qty > int(line["quantity"]):
            raise InvalidReturnError("Return quantity exceeds unreturned quantity on the sale line.")
        order = self.conn.execute("SELECT * FROM orders WHERE order_id=?", (line["order_id"],)).fetchone()
        refund = br.refund_amount(br.money(line["unit_price"]), Decimal(order["order_discount_pct"]), inp.qty)
        with transaction(self.conn):
            return_id = next_id(self.conn, "returns", "return_id", "R-")
            self.conn.execute(
                "INSERT INTO returns VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (return_id, return_date.isoformat(), line["order_id"], line["line_no"], line["sku"], inp.qty, inp.condition, str(refund)),
            )
            if br.should_restock_return(inp.condition):
                self.conn.execute("INSERT INTO inventory_movements(sku, delta, reason, reference_id, timestamp) VALUES (?, ?, ?, ?, ?)", (line["sku"], inp.qty, "return", return_id, return_date.isoformat()))
        return {"ok": True, "message": f"Recorded return {return_id}; refund is ${refund:.2f}.", "data": {"return_id": return_id, "order_id": line["order_id"], "line_no": line["line_no"], "sku": line["sku"], "quantity": inp.qty, "condition": inp.condition, "refund_amount": str(refund), "restocked": br.should_restock_return(inp.condition), "date": return_date.isoformat()}}

    def create_promotion(self, scope_type: str, scope_value: str, discount_pct: int, start_date: str, end_date: str) -> dict[str, Any]:
        inp = CreatePromotionInput(scope_type=scope_type, scope_value=scope_value, discount_pct=discount_pct, start_date=start_date, end_date=end_date)
        scope_ref = self._promotion_scope_ref(inp.scope_type, inp.scope_value)
        with transaction(self.conn):
            promo_id = next_id(self.conn, "promotions", "promo_id", "PR-")
            desc = f"{inp.discount_pct}% off {inp.scope_type} {scope_ref}"
            self.conn.execute("INSERT INTO promotions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (promo_id, desc, "percent_off", str(inp.discount_pct), inp.scope_type, scope_ref, inp.start_date, inp.end_date))
        return {"ok": True, "message": f"Created promotion {promo_id}.", "data": {"promo_id": promo_id, "scope_type": inp.scope_type, "scope_ref": scope_ref, "discount_pct": inp.discount_pct, "start_date": inp.start_date, "end_date": inp.end_date}}

    def reorder_low_stock(self, date: Optional[str] = None) -> dict[str, Any]:
        po_date = self._date(date)
        low_rows = self.conn.execute(
            """
            SELECT p.*, i.reorder_point, i.reorder_qty,
                   i.snapshot_qty + COALESCE(SUM(m.delta),0) AS current_qty
            FROM products p JOIN inventory_snapshots i ON i.sku=p.sku
            LEFT JOIN inventory_movements m ON m.sku=p.sku
            GROUP BY p.sku HAVING current_qty < i.reorder_point
            """
        ).fetchall()
        grouped: dict[str, list[tuple[sqlite3.Row, sqlite3.Row]]] = {}
        for product in low_rows:
            supplier = br.rank_suppliers([dict(r) for r in self.conn.execute("SELECT * FROM supplier_catalog WHERE product_id=?", (product["product_id"],)).fetchall()])
            if supplier:
                grouped.setdefault(str(supplier["supplier_id"]), []).append((product, supplier))
        ordered = []
        po_ids = []
        with transaction(self.conn):
            for supplier_id, items in grouped.items():
                po_id = next_id(self.conn, "purchase_orders", "po_id", "PO-")
                po_ids.append(po_id)
                self.conn.execute("INSERT INTO purchase_orders VALUES (?, ?, ?, ?, ?)", (po_id, supplier_id, po_date.isoformat(), "open", 0))
                for line_no, (product, supplier) in enumerate(items, start=1):
                    self.conn.execute("INSERT INTO purchase_order_lines VALUES (?, ?, ?, ?, ?, ?)", (po_id, line_no, product["sku"], product["reorder_qty"], 0, str(supplier["unit_cost"])))
                    ordered.append({"po_id": po_id, "sku": product["sku"], "qty": product["reorder_qty"], "supplier_id": supplier_id, "unit_cost": str(supplier["unit_cost"])})
        return {"ok": True, "message": f"Created {len(po_ids)} purchase order(s).", "data": {"po_ids": po_ids, "items": ordered, "date": po_date.isoformat()}}

    def receive_purchase_order(self, po_id: Optional[str] = None, supplier_descriptor: Optional[str] = None, product_descriptor: Optional[str] = None, qty_ordered: Optional[int] = None, qty_received: Optional[int] = None, receive_all: bool = False, date: Optional[str] = None) -> dict[str, Any]:
        inp = ReceivePurchaseOrderInput(po_id=po_id, supplier_descriptor=supplier_descriptor, product_descriptor=product_descriptor, qty_ordered=qty_ordered, qty_received=qty_received, receive_all=receive_all, date=date)
        receive_date = self._date(inp.date)
        po, line, synthesized = self._resolve_or_synthesize_po(inp)
        remaining = int(line["qty_ordered"]) - int(line["qty_received"])
        if inp.receive_all:
            receive_qty = remaining
        elif inp.qty_received is not None:
            receive_qty = min(inp.qty_received, remaining)
        else:
            raise UnknownPurchaseOrderError("Quantity received is required unless receive_all is true.")
        if receive_qty <= 0:
            raise UnknownPurchaseOrderError("Purchase order line is already fully received.")
        new_received = int(line["qty_received"]) + receive_qty
        status = "received/closed" if new_received >= int(line["qty_ordered"]) else "partial"
        with transaction(self.conn):
            self.conn.execute("UPDATE purchase_order_lines SET qty_received=? WHERE po_id=? AND line_no=?", (new_received, po["po_id"], line["line_no"]))
            self.conn.execute("UPDATE purchase_orders SET status=? WHERE po_id=?", (status, po["po_id"]))
            self.conn.execute("INSERT INTO inventory_movements(sku, delta, reason, reference_id, timestamp) VALUES (?, ?, ?, ?, ?)", (line["sku"], receive_qty, "receiving", po["po_id"], receive_date.isoformat()))
        return {"ok": True, "message": f"Received {receive_qty} unit(s) on {po['po_id']}." + (" Synthesized the PO from the instruction first." if synthesized else ""), "data": {"po_id": po["po_id"], "sku": line["sku"], "qty_received": receive_qty, "qty_ordered": line["qty_ordered"], "status": status, "synthesized": synthesized, "date": receive_date.isoformat()}}

    def adjust_sale_quantity(self, order_id: str, qty: int, line_no: Optional[int] = None, date: Optional[str] = None) -> dict[str, Any]:
        inp = AdjustSaleQuantityInput(order_id=order_id, qty=qty, line_no=line_no, date=date)
        rows = self.conn.execute("SELECT * FROM order_lines WHERE order_id=?", (inp.order_id,)).fetchall()
        if not rows:
            raise AmbiguousOrderError(f"Order {inp.order_id} does not exist.")
        if inp.line_no is not None:
            rows = [r for r in rows if int(r["line_no"]) == inp.line_no]
        if len(rows) != 1:
            raise AmbiguousOrderError("Quantity adjustment needs a single sale line.")
        line = rows[0]
        old_qty = int(line["quantity"])
        delta_qty = inp.qty - old_qty
        if delta_qty > 0:
            available = current_qty(self.conn, line["sku"])
            if available < delta_qty:
                raise InsufficientStockError(line["sku"], delta_qty, available)
        order = self.conn.execute("SELECT * FROM orders WHERE order_id=?", (inp.order_id,)).fetchone()
        product = self.conn.execute("SELECT * FROM products WHERE sku=?", (line["sku"],)).fetchone()
        revenue = br.line_revenue(br.money(line["unit_price"]), Decimal(order["order_discount_pct"]), inp.qty)
        margin = br.product_margin(revenue, self._frozen_cogs_cost(product["product_id"]), inp.qty)
        adjust_date = self._date(inp.date)
        with transaction(self.conn):
            self.conn.execute("UPDATE order_lines SET quantity=? WHERE order_id=? AND line_no=?", (inp.qty, inp.order_id, line["line_no"]))
            if delta_qty:
                self.conn.execute("INSERT INTO inventory_movements(sku, delta, reason, reference_id, timestamp) VALUES (?, ?, ?, ?, ?)", (line["sku"], -delta_qty, "sale_adjustment", inp.order_id, adjust_date.isoformat()))
        return {"ok": True, "message": f"Updated order {inp.order_id} line {line['line_no']} to quantity {inp.qty}; new line total is ${revenue:.2f}.", "data": {"order_id": inp.order_id, "line_no": line["line_no"], "sku": line["sku"], "quantity": inp.qty, "line_total": str(revenue), "margin": str(margin), "date": adjust_date.isoformat()}}

    def get_top_products_by_margin(self, period: str, limit: int = 5) -> dict[str, Any]:
        inp = TopProductsByMarginInput(period=period, limit=limit)
        start, end = self._period(inp.period)
        rows = self.conn.execute(
            """
            SELECT o.order_id, p.product_id, p.product_name, p.sku, ol.unit_price, ol.quantity, o.order_discount_pct
            FROM order_lines ol JOIN orders o ON o.order_id=ol.order_id JOIN products p ON p.sku=ol.sku
            WHERE o.order_date BETWEEN ? AND ?
            """,
            (start, end),
        ).fetchall()
        by_product: dict[str, dict[str, Any]] = {}
        for row in rows:
            returned = self.conn.execute(
                "SELECT condition, COALESCE(SUM(quantity),0) AS qty FROM returns WHERE order_id=? AND sku=? GROUP BY condition",
                (row["order_id"], row["sku"]),
            ).fetchall()
            returned_by_condition = {r["condition"]: int(r["qty"] or 0) for r in returned}
            good_qty = min(int(row["quantity"]), returned_by_condition.get("good", 0))
            damaged_qty = min(int(row["quantity"]) - good_qty, returned_by_condition.get("damaged", 0))
            kept_revenue_qty = int(row["quantity"]) - good_qty - damaged_qty
            cost_qty = int(row["quantity"]) - good_qty
            revenue = br.line_revenue(br.money(row["unit_price"]), Decimal(row["order_discount_pct"]), kept_revenue_qty)
            cost = self._frozen_cogs_cost(row["product_id"])
            margin = (revenue - cost * cost_qty).quantize(br.CENT)
            bucket = by_product.setdefault(row["product_id"], {"product_id": row["product_id"], "product_name": row["product_name"], "units": 0, "revenue": Decimal("0"), "margin": Decimal("0")})
            bucket["units"] += cost_qty
            bucket["revenue"] += revenue
            bucket["margin"] += margin
        items = sorted(by_product.values(), key=lambda r: r["margin"], reverse=True)[: inp.limit]
        out = [{**i, "revenue": str(i["revenue"]), "margin": str(i["margin"])} for i in items]
        return {"ok": True, "message": f"Top {len(out)} products by margin for {inp.period}.", "data": {"period_start": start, "period_end": end, "items": out}}

    def get_net_revenue(self, period: str) -> dict[str, Any]:
        inp = NetRevenueInput(period=period)
        start, end = self._period(inp.period)
        rows = self.conn.execute(
            """
            SELECT ol.unit_price, ol.quantity, o.order_discount_pct
            FROM order_lines ol JOIN orders o ON o.order_id=ol.order_id
            WHERE o.order_date BETWEEN ? AND ?
            """,
            (start, end),
        ).fetchall()
        gross = sum((br.line_revenue(br.money(r["unit_price"]), Decimal(r["order_discount_pct"]), int(r["quantity"])) for r in rows), Decimal("0")).quantize(br.CENT)
        refunds = br.money(self.conn.execute("SELECT COALESCE(SUM(refund_amount),0) AS total FROM returns WHERE return_date BETWEEN ? AND ?", (start, end)).fetchone()["total"])
        net = (gross - refunds).quantize(br.CENT)
        return {"ok": True, "message": f"Net revenue for {inp.period} was ${net:.2f}.", "data": {"period_start": start, "period_end": end, "gross_revenue": str(gross), "refunds": str(refunds), "net_revenue": str(net)}}

    def get_stockout_risk(self, horizon_days: int = 14) -> dict[str, Any]:
        inp = StockoutRiskInput(horizon_days=horizon_days)
        risks = []
        for product_id in [r["product_id"] for r in self.conn.execute("SELECT DISTINCT product_id FROM products").fetchall()]:
            item = self._product_stockout_metrics(product_id, inp.horizon_days)
            if item["reorder_risk"] or item["cover_risk"]:
                risks.append(item)
        return {"ok": True, "message": f"Found {len(risks)} stockout risk item(s).", "data": {"items": risks, "horizon_days": inp.horizon_days}}

    def check_inventory(self, product_descriptor: str) -> dict[str, Any]:
        inp = CheckInventoryInput(product_descriptor=product_descriptor)
        product = self.lookup_product(query=inp.product_descriptor)["data"]
        inv = self.conn.execute("SELECT * FROM inventory_snapshots WHERE sku=?", (product["sku"],)).fetchone()
        incoming = [dict(r) for r in self.conn.execute(
            "SELECT po.po_id, po.status, pol.qty_ordered, pol.qty_received FROM purchase_orders po JOIN purchase_order_lines pol ON pol.po_id=po.po_id WHERE pol.sku=? AND po.status IN ('open','partial')",
            (product["sku"],),
        ).fetchall()]
        return {"ok": True, "message": f"Inventory for {product['sku']} is {current_qty(self.conn, product['sku'])}.", "data": {"sku": product["sku"], "current_qty": current_qty(self.conn, product["sku"]), "reorder_point": inv["reorder_point"], "incoming": incoming}}

    def check_inventory_summary(self, product_descriptor: str, color: Optional[str] = None) -> dict[str, Any]:
        inp = InventorySummaryInput(product_descriptor=product_descriptor, color=color)
        candidates = self._product_candidates(query=inp.product_descriptor, color=inp.color)
        if not candidates:
            raise AmbiguousProductError([])
        product_ids = sorted({c["product_id"] for c in candidates})
        if len(product_ids) != 1:
            raise AmbiguousProductError(candidates)
        subset_qty = sum(current_qty(self.conn, c["sku"]) for c in candidates)
        metrics = self._product_stockout_metrics(product_ids[0], 14)
        return {"ok": True, "message": f"{inp.product_descriptor} has {subset_qty} unit(s) on hand; product stockout risk is {'yes' if metrics['at_risk'] else 'no'}.", "data": {"matched_skus": [c["sku"] for c in candidates], "current_qty": subset_qty, **metrics}}

    def find_returnable_order_lines(self, customer_descriptor: str, product_descriptor: str) -> dict[str, Any]:
        inp = FindReturnableOrderLinesInput(customer_descriptor=customer_descriptor, product_descriptor=product_descriptor)
        customer = self._resolve_customer(inp.customer_descriptor, None, allow_walk_in=False)
        products = self._product_candidates(query=inp.product_descriptor)
        if not products:
            raise AmbiguousProductError([])
        skus = [p["sku"] for p in products]
        placeholders = ",".join(["?"] * len(skus))
        rows = self.conn.execute(
            f"""
            SELECT o.order_id, ol.line_no, ol.sku, ol.quantity, ol.unit_price, o.order_discount_pct
            FROM orders o JOIN order_lines ol ON ol.order_id=o.order_id
            WHERE o.customer_id=? AND ol.sku IN ({placeholders})
            ORDER BY o.order_date, o.order_id, ol.line_no
            """,
            (customer["customer_id"], *skus),
        ).fetchall()
        candidates = []
        for row in rows:
            returned = int(self.conn.execute("SELECT COALESCE(SUM(quantity),0) AS qty FROM returns WHERE order_id=? AND sku=?", (row["order_id"], row["sku"])).fetchone()["qty"] or 0)
            remaining = int(row["quantity"]) - returned
            if remaining > 0:
                candidates.append({"order_id": row["order_id"], "line_no": row["line_no"], "sku": row["sku"], "returnable_qty": remaining, "refund_per_unit": str(br.discounted_unit_price(br.money(row["unit_price"]), Decimal(row["order_discount_pct"])))})
        if not candidates:
            raise InvalidReturnError("No returnable matching sale line was found.")
        message = "Found one returnable line. Ask whether the item is good or damaged before processing the return." if len(candidates) == 1 else "Multiple returnable lines match; ask which order or line."
        return {"ok": True, "message": message, "data": {"customer_id": customer["customer_id"], "candidates": candidates, "condition_required": True}}

    def _product_candidates(self, query: Optional[str] = None, category: Optional[str] = None, color: Optional[str] = None, size: Optional[str] = None, sku: Optional[str] = None) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM products").fetchall()]
        q = (query or "").lower()
        if sku:
            rows = [r for r in rows if r["sku"].lower() == sku.lower()]
        if category:
            rows = [r for r in rows if r["category"].lower() == category.lower()]
        if color:
            rows = [r for r in rows if (r["color"] or "").lower().startswith(color.lower())]
        if size:
            size_map = {"small": "s", "medium": "m", "large": "l"}
            s = size_map.get(size.lower(), size.lower())
            rows = [r for r in rows if (r["size"] or "").lower() == s]
        if q:
            words = q.replace("-", " ").split()
            size_words = {"small": "s", "medium": "m", "large": "l", "s": "s", "m": "m", "l": "l"}
            colors = {"blue", "black", "gray", "grey", "navy"}
            detected_size = next((size_words[w] for w in words if w in size_words), None)
            detected_color = next((w for w in words if w in colors), None)
            if detected_size:
                rows = [r for r in rows if (r["size"] or "").lower() == detected_size]
            if detected_color:
                norm = "gray" if detected_color == "grey" else detected_color
                rows = [r for r in rows if (r["color"] or "").lower() == norm]
            aliases = {"tee": "classic tee", "tees": "classic tee", "hoodie": "pullover hoodie", "hoodies": "pullover hoodie", "tote": "canvas tote", "totes": "canvas tote", "mug": "ceramic mug", "socks": "wool socks", "sock": "wool socks"}
            wanted = next((aliases[w] for w in words if w in aliases), q)
            rows = [r for r in rows if wanted in r["product_name"].lower() or wanted in r["sku"].lower() or SequenceMatcher(None, wanted, r["product_name"].lower()).ratio() > 0.65]
        return rows

    def _resolve_customer(self, name: Optional[str], customer_id: Optional[str], allow_walk_in: bool) -> Optional[sqlite3.Row]:
        if customer_id:
            row = self.conn.execute("SELECT * FROM customers WHERE customer_id=?", (customer_id,)).fetchone()
            if not row:
                raise UnknownCustomerError(f"Unknown customer {customer_id}.")
            return row
        if not name or name.lower() in {"walk-in", "walk in", "walkin"}:
            return None if allow_walk_in else None
        rows = self.conn.execute("SELECT * FROM customers").fetchall()
        scored = [(SequenceMatcher(None, name.lower(), r["name"].lower()).ratio(), r) for r in rows]
        scored = [x for x in scored if x[0] > 0.55 or name.lower() in x[1]["name"].lower()]
        if not scored:
            raise UnknownCustomerError(f"No customer matched {name}.")
        scored.sort(key=lambda x: x[0], reverse=True)
        if len(scored) > 1 and abs(scored[0][0] - scored[1][0]) < 0.05:
            raise UnknownCustomerError(f"Multiple customers matched {name}.")
        return scored[0][1]

    def _date(self, value: Optional[str]) -> date:
        if not value:
            return br.TODAY
        normalized = value.strip().lower()
        if normalized == "today":
            return br.TODAY
        if normalized == "tomorrow":
            return br.TODAY + timedelta(days=1)
        try:
            return date.fromisoformat(value)
        except ValueError:
            cleaned = normalized.replace("st", "").replace("nd", "").replace("rd", "").replace("th", "")
            for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
                try:
                    return datetime.strptime(cleaned, fmt).date()
                except ValueError:
                    continue
            raise

    def _active_promos(self, product: dict[str, Any], sale_date: date) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM promotions").fetchall()]
        return [r for r in rows if br.promotion_applies(r, product, sale_date)]

    def _frozen_cogs_cost(self, product_id: str) -> Decimal:
        """Frozen Northwind cost for COGS/margin only. DATA_DICTIONARY.md Business rules #1."""
        row = self.conn.execute("SELECT unit_cost FROM supplier_catalog WHERE supplier_id='SUP-NW' AND product_id=?", (product_id,)).fetchone()
        return br.money(row["unit_cost"])

    def _purchase_order_unit_cost(self, supplier_id: str, product_id: str) -> Decimal:
        """Supplier-specific purchase cost for new POs only. DATA_DICTIONARY.md Business rules #4."""
        row = self.conn.execute("SELECT unit_cost FROM supplier_catalog WHERE supplier_id=? AND product_id=?", (supplier_id, product_id)).fetchone()
        return br.money(row["unit_cost"])

    def _product_stockout_metrics(self, product_id: str, horizon_days: int) -> dict[str, Any]:
        """Shared product-level stockout calculation. DATA_DICTIONARY.md Business rules #7."""
        variants = [dict(r) for r in self.conn.execute("SELECT * FROM products WHERE product_id=?", (product_id,)).fetchall()]
        if not variants:
            raise AmbiguousProductError([])
        on_hand = 0
        monthly_units = 0
        trigger_skus = []
        for row in variants:
            sku_qty = current_qty(self.conn, row["sku"])
            on_hand += sku_qty
            inv = self.conn.execute("SELECT reorder_point FROM inventory_snapshots WHERE sku=?", (row["sku"],)).fetchone()
            if sku_qty <= int(inv["reorder_point"]):
                trigger_skus.append(row["sku"])
            monthly_units += int(self.conn.execute(
                """
                SELECT COALESCE(SUM(ol.quantity),0) AS q
                FROM order_lines ol JOIN orders o ON o.order_id=ol.order_id
                WHERE ol.sku=? AND o.order_date BETWEEN '2026-05-01' AND '2026-05-31'
                """,
                (row["sku"],),
            ).fetchone()["q"] or 0)
        cover = br.days_of_cover(on_hand, monthly_units)
        cover_risk = cover is not None and cover < Decimal(horizon_days)
        reorder_risk = bool(trigger_skus)
        return {
            "product_id": product_id,
            "product_name": variants[0]["product_name"],
            "product_on_hand": on_hand,
            "on_hand": on_hand,
            "monthly_units": monthly_units,
            "days_of_cover": str(cover) if cover is not None else None,
            "reorder_trigger_skus": trigger_skus,
            "reorder_risk": reorder_risk,
            "cover_risk": cover_risk,
            "at_risk": reorder_risk or cover_risk,
        }

    def _resolve_order_line(self, order_id: Optional[str], order_line_id: Optional[str], product_descriptor: Optional[str]) -> sqlite3.Row:
        if order_line_id and ":" in order_line_id:
            order_id, line_no = order_line_id.split(":", 1)
            row = self.conn.execute("SELECT * FROM order_lines WHERE order_id=? AND line_no=?", (order_id, line_no)).fetchone()
            if row:
                return row
        if not order_id:
            raise AmbiguousOrderError("An order id is required to resolve the return.")
        rows = self.conn.execute("SELECT * FROM order_lines WHERE order_id=?", (order_id,)).fetchall()
        if not rows:
            raise InvalidReturnError(f"Order {order_id} does not exist.")
        if product_descriptor:
            product = self.lookup_product(query=product_descriptor)["data"]
            rows = [r for r in rows if r["sku"] == product["sku"]]
        if len(rows) != 1:
            raise AmbiguousOrderError("Return line is ambiguous; specify the product or line.")
        return rows[0]

    def _promotion_scope_ref(self, scope_type: str, scope_value: str) -> str:
        if scope_type == "category":
            return scope_value.lower()
        return self._resolve_product_id(scope_value)

    def _resolve_product_id(self, descriptor: str) -> str:
        candidates = self._product_candidates(query=descriptor)
        product_ids = sorted({row["product_id"] for row in candidates})
        if len(product_ids) == 1:
            return product_ids[0]
        if not product_ids:
            raise AmbiguousProductError([])
        raise AmbiguousProductError(candidates)

    def _resolve_supplier(self, descriptor: Optional[str]) -> sqlite3.Row:
        if not descriptor:
            raise UnknownPurchaseOrderError("Supplier is required.")
        rows = self.conn.execute("SELECT * FROM suppliers").fetchall()
        for row in rows:
            if descriptor.lower() in row["supplier_name"].lower() or descriptor.lower() == row["supplier_id"].lower():
                return row
        raise UnknownPurchaseOrderError(f"Unknown supplier {descriptor}.")

    def _resolve_or_synthesize_po(self, inp: ReceivePurchaseOrderInput) -> tuple[sqlite3.Row, sqlite3.Row, bool]:
        if inp.po_id:
            po = self.conn.execute("SELECT * FROM purchase_orders WHERE po_id=?", (inp.po_id,)).fetchone()
            if not po:
                raise UnknownPurchaseOrderError(f"Unknown PO {inp.po_id}.")
            line = self.conn.execute("SELECT * FROM purchase_order_lines WHERE po_id=? ORDER BY line_no LIMIT 1", (inp.po_id,)).fetchone()
            return po, line, False
        supplier = self._resolve_supplier(inp.supplier_descriptor)
        product = self.lookup_product(query=inp.product_descriptor)["data"] if inp.product_descriptor else None
        rows = self.conn.execute(
            """
            SELECT po.*, pol.line_no, pol.sku, pol.qty_ordered, pol.qty_received, pol.unit_cost
            FROM purchase_orders po JOIN purchase_order_lines pol ON pol.po_id=po.po_id
            WHERE po.supplier_id=? AND po.status IN ('open','partial') AND (? IS NULL OR pol.sku=?)
            """,
            (supplier["supplier_id"], product["sku"] if product else None, product["sku"] if product else None),
        ).fetchall()
        if len(rows) == 1:
            return rows[0], rows[0], False
        if len(rows) > 1:
            raise AmbiguousOrderError("Multiple open purchase orders match.")
        if not product or not inp.qty_ordered:
            raise UnknownPurchaseOrderError("No matching PO exists and there is not enough detail to synthesize one.")
        catalog = self.conn.execute("SELECT * FROM supplier_catalog WHERE supplier_id=? AND product_id=?", (supplier["supplier_id"], product["product_id"])).fetchone()
        with transaction(self.conn):
            po_id = next_id(self.conn, "purchase_orders", "po_id", "PO-")
            self.conn.execute("INSERT INTO purchase_orders VALUES (?, ?, ?, ?, ?)", (po_id, supplier["supplier_id"], self._date(inp.date).isoformat(), "open", 1))
            self.conn.execute("INSERT INTO purchase_order_lines VALUES (?, ?, ?, ?, ?, ?)", (po_id, 1, product["sku"], inp.qty_ordered, 0, catalog["unit_cost"]))
        po = self.conn.execute("SELECT * FROM purchase_orders WHERE po_id=?", (po_id,)).fetchone()
        line = self.conn.execute("SELECT * FROM purchase_order_lines WHERE po_id=?", (po_id,)).fetchone()
        return po, line, True

    def _period(self, period: str) -> tuple[str, str]:
        normalized = period.strip().lower()
        if normalized == "last month":
            return "2026-05-01", "2026-05-31"
        if normalized in {"may 2026", "may"}:
            return "2026-05-01", "2026-05-31"
        if ":" in period:
            start, end = period.split(":", 1)
            return start, end
        raise ValueError("Use 'last month' or YYYY-MM-DD:YYYY-MM-DD.")
