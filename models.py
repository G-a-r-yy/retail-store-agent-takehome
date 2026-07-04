from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ProductLookupInput(BaseModel):
    query: Optional[str] = None
    category: Optional[str] = None
    color: Optional[str] = None
    size: Optional[str] = None
    sku: Optional[str] = None


class CustomerLookupInput(BaseModel):
    name: Optional[str] = None
    customer_id: Optional[str] = None


class SaleLineInput(BaseModel):
    product_descriptor: str
    qty: int = Field(gt=0)


class RingUpSaleInput(BaseModel):
    line_items: list[SaleLineInput]
    customer_descriptor: Optional[str] = None
    payment_method: Optional[str] = None
    date: Optional[str] = None
    order_discount_pct: int = Field(default=0, ge=0, lt=100)


class ProcessReturnInput(BaseModel):
    order_id: Optional[str] = None
    order_line_id: Optional[str] = None
    product_descriptor: Optional[str] = None
    qty: int = Field(gt=0)
    condition: Literal["good", "damaged"]
    date: Optional[str] = None


class CreatePromotionInput(BaseModel):
    scope_type: Literal["product", "category"]
    scope_value: str
    discount_pct: int = Field(gt=0, lt=100)
    start_date: str
    end_date: str


class ReorderLowStockInput(BaseModel):
    date: Optional[str] = None


class ReceivePurchaseOrderInput(BaseModel):
    po_id: Optional[str] = None
    supplier_descriptor: Optional[str] = None
    product_descriptor: Optional[str] = None
    qty_ordered: Optional[int] = Field(default=None, gt=0)
    qty_received: Optional[int] = Field(default=None, gt=0)
    receive_all: bool = False
    date: Optional[str] = None


class TopProductsByMarginInput(BaseModel):
    period: str
    limit: int = Field(default=5, gt=0)


class StockoutRiskInput(BaseModel):
    horizon_days: int = Field(default=14, gt=0)


class CheckInventoryInput(BaseModel):
    product_descriptor: str


class AdjustSaleQuantityInput(BaseModel):
    order_id: str
    qty: int = Field(gt=0)
    line_no: Optional[int] = Field(default=None, gt=0)
    date: Optional[str] = None


class FindReturnableOrderLinesInput(BaseModel):
    customer_descriptor: str
    product_descriptor: str


class NetRevenueInput(BaseModel):
    period: str


class InventorySummaryInput(BaseModel):
    product_descriptor: str
    color: Optional[str] = None


class ToolResult(BaseModel):
    ok: bool = True
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
