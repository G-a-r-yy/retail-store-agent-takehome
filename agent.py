from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from memory import SessionMemory
from models import (
    CheckInventoryInput,
    CreatePromotionInput,
    AdjustSaleQuantityInput,
    FindReturnableOrderLinesInput,
    InventorySummaryInput,
    NetRevenueInput,
    CustomerLookupInput,
    ProductLookupInput,
    ProcessReturnInput,
    ReceivePurchaseOrderInput,
    ReorderLowStockInput,
    RingUpSaleInput,
    StockoutRiskInput,
    TopProductsByMarginInput,
)
from tools import (
    AmbiguousOrderError,
    AmbiguousProductError,
    InsufficientStockError,
    InvalidReturnError,
    RetailTools,
    UnknownCustomerError,
    UnknownPurchaseOrderError,
)


SYSTEM_PROMPT = """You are a retail store operations agent.
Use tools for every fact-changing action and every price, refund, inventory, reorder, stockout, revenue, or margin calculation.
Never compute money or inventory yourself. Compose final replies from tool outputs.
Support multi-step user instructions by calling one tool, reading its result, then calling the next needed tool.

Defaults and clarification policy:
- Missing date defaults to today, 2026-06-19.
- Missing payment_method defaults to "unspecified".
- Walk-in means no customer record.
- Ask a concise clarification only when a tool reports ambiguity or when proceeding would choose between materially different outcomes.
- Use session memory for references like "that", "same customer", "the other one", and prior order/PO/return IDs.
- For follow-ups like "make that four instead" after a sale, call adjust_sale_quantity using last_order_id.
- For returns where the customer does not remember the order, call find_returnable_order_lines first; if condition is missing, ask good vs damaged before process_return.
- For "net revenue" or "revenue kept", call get_net_revenue. Do not use margin tools.
- For receiving a purchase order described as "complete", call receive_purchase_order with receive_all=true.
- For inventory questions that aggregate variants, such as color plus product across sizes, call check_inventory_summary.
"""


MODEL_BY_TOOL: dict[str, Any] = {
    "lookup_product": ProductLookupInput,
    "lookup_customer": CustomerLookupInput,
    "ring_up_sale": RingUpSaleInput,
    "adjust_sale_quantity": AdjustSaleQuantityInput,
    "process_return": ProcessReturnInput,
    "find_returnable_order_lines": FindReturnableOrderLinesInput,
    "create_promotion": CreatePromotionInput,
    "reorder_low_stock": ReorderLowStockInput,
    "receive_purchase_order": ReceivePurchaseOrderInput,
    "get_top_products_by_margin": TopProductsByMarginInput,
    "get_net_revenue": NetRevenueInput,
    "get_stockout_risk": StockoutRiskInput,
    "check_inventory": CheckInventoryInput,
    "check_inventory_summary": InventorySummaryInput,
}


TOOL_DESCRIPTIONS = {
    "lookup_product": "Resolve a natural-language product or SKU to one sellable variant.",
    "lookup_customer": "Resolve a customer by fuzzy name or ID. Walk-in is allowed as no customer_id.",
    "ring_up_sale": "Create an order, price promotions, validate inventory, and write sale inventory movements.",
    "adjust_sale_quantity": "Change a single-line sale's quantity, usually for follow-ups like 'make that four instead'.",
    "process_return": "Return sold units, compute refund from original price paid, and restock only good condition items.",
    "find_returnable_order_lines": "Find a customer's returnable sale lines for a product when the customer does not remember the order.",
    "create_promotion": "Create a future/current percent-off promotion scoped to product or category.",
    "reorder_low_stock": "Create purchase orders for variants below reorder point using deterministic supplier ranking.",
    "receive_purchase_order": "Receive units against an existing or synthesized purchase order.",
    "get_top_products_by_margin": "Report top products by margin for a period such as last month.",
    "get_net_revenue": "Report gross revenue minus refunds for a period; this is not margin.",
    "get_stockout_risk": "Report products below reorder point or with fewer than horizon days of cover.",
    "check_inventory": "Return derived current inventory and incoming open/partial POs for a variant.",
    "check_inventory_summary": "Return aggregate inventory for product/color queries and product-level stockout risk.",
}


DEFAULT_MODEL = "gpt-5.5"


def load_env_file(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class Agent:
    def __init__(self, tools: RetailTools, memory: SessionMemory | None = None, model: str | None = None):
        load_env_file()
        self.tools = tools
        self.memory = memory or SessionMemory()
        self.model = model or DEFAULT_MODEL
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required. Add it to .env or set it in your shell. This agent intentionally does not use a regex fallback parser.")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("The openai package is required. Install dependencies with: pip install -r requirements.txt") from exc
        self.client = OpenAI()

    def run_turn(self, user_text: str) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": f"Session memory: {json.dumps(self.memory.__dict__, default=str)}"},
            {"role": "user", "content": user_text},
        ]
        for _ in range(8):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self._tool_schemas(),
                tool_choice="auto",
            )
            msg = response.choices[0].message
            messages.append(msg.model_dump(exclude_none=True))
            calls = msg.tool_calls or []
            if not calls:
                return msg.content or ""
            for call in calls:
                name = call.function.name
                args = json.loads(call.function.arguments or "{}")
                result = self._call_tool(name, args)
                self.memory.update_from_result(result)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, default=str)})
        return "I reached the tool-call limit for this turn. Please split the request into smaller steps."

    def _call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            func: Callable[..., dict[str, Any]] = getattr(self.tools, name)
            return func(**args)
        except AmbiguousProductError as exc:
            return {"ok": False, "error_type": "AmbiguousProductError", "message": "Please clarify which product variant you mean.", "candidates": exc.candidates}
        except AmbiguousOrderError as exc:
            return {"ok": False, "error_type": "AmbiguousOrderError", "message": str(exc)}
        except InsufficientStockError as exc:
            return {"ok": False, "error_type": "InsufficientStockError", "message": str(exc), "sku": exc.sku, "requested": exc.requested, "available": exc.available}
        except UnknownCustomerError as exc:
            return {"ok": False, "error_type": "UnknownCustomerError", "message": str(exc)}
        except InvalidReturnError as exc:
            return {"ok": False, "error_type": "InvalidReturnError", "message": str(exc)}
        except UnknownPurchaseOrderError as exc:
            return {"ok": False, "error_type": "UnknownPurchaseOrderError", "message": str(exc)}

    def _tool_schemas(self) -> list[dict[str, Any]]:
        out = []
        for name, model in MODEL_BY_TOOL.items():
            schema = model.model_json_schema() if hasattr(model, "model_json_schema") else model.schema()
            out.append({"type": "function", "function": {"name": name, "description": TOOL_DESCRIPTIONS[name], "parameters": schema}})
        return out
