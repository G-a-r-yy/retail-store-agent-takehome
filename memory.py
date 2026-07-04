from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SessionMemory:
    last_order_id: Optional[str] = None
    last_return_id: Optional[str] = None
    last_customer_id: Optional[str] = None
    last_product_ids: list[str] = field(default_factory=list)
    last_purchase_order_id: Optional[str] = None
    last_date: Optional[str] = None
    last_tool_result: Optional[dict[str, Any]] = None

    def update_from_result(self, result: dict[str, Any]) -> None:
        data = result.get("data", result)
        if data.get("order_id"):
            self.last_order_id = data["order_id"]
        if data.get("return_id"):
            self.last_return_id = data["return_id"]
        if data.get("customer_id"):
            self.last_customer_id = data["customer_id"]
        if data.get("po_id"):
            self.last_purchase_order_id = data["po_id"]
        elif data.get("po_ids"):
            self.last_purchase_order_id = data["po_ids"][-1] if data["po_ids"] else self.last_purchase_order_id
        if data.get("date"):
            self.last_date = data["date"]
        skus = []
        for line in data.get("lines", []) + data.get("items", []):
            if isinstance(line, dict) and line.get("sku"):
                skus.append(line["sku"])
        if skus:
            self.last_product_ids = skus
        self.last_tool_result = result
