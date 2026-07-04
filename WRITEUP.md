# Retail Store Agent Writeup

## My Approach (High Level Summary)

The agent separates language understanding from store operations. The LLM reads the user's
instruction, chooses one or more structured tools, and writes the final reply from tool results.
All business logic lives in deterministic Python: pricing, discounts, refunds, inventory
movements, purchase orders, revenue, margin, and stockout calculations are never computed by the
LLM. 

Note: this design decision was made so that the LLM/AI layer functions more like a light input parser and tool caller. This decision was made rather than having an LLM do everything end to end, because doing so could result in things like hallucinations, incorrect db (which is our source of truth) changes, etc, when instead most realistic prompts will follow patterns that can instead be done deterministically. 

SQLite is used as the session source of truth. On each CLI start, the database is initialized
fresh from the CSV seed data and then mutated in memory for the rest of that interactive session.
Session memory stores recent order/customer/product/PO context so follow-ups like "make that
four" or "return that" can be resolved while still validating every action through the tool layer.



## Domain model

The CLI uses an in-memory database initialized from the CSVs on each start, so there is no
cross-session persistence. The schema models `Product`, `Customer`, `Supplier`,
`SupplierCatalog`, `InventoryItem` snapshots, `InventoryMovement`, `Order`, `OrderLine`,
`Return`, `Promotion`, `PurchaseOrder`, and `PurchaseOrderLine`.

The SQLite schema, CSV loading, transaction helper, ID allocation, and current-inventory
derivation live in `db.py`.

Inventory is derived as:

```text
current_qty = inventory_snapshots.snapshot_qty + SUM(inventory_movements.delta)
```

Sales, good returns, and receiving all append movement rows instead of overwriting stock.
Purchase orders use `open -> partial -> received/closed`.

## Step-0 rule and assumption table

| Rule area | Source | Implemented rule |
|---|---|---|
| Assignment date | `DATA_DICTIONARY.md`, opening section | Today is `2026-06-19`; "last month" is May 2026. |
| Money and dates | `DATA_DICTIONARY.md`, opening section | Money is USD; dates are `YYYY-MM-DD`. Code uses `Decimal`, not float. |
| Product variants | `products.csv` section | A sellable unit is a SKU; variants share `product_id`. |
| Walk-in sales | `customers.csv` and `orders.csv` sections | A sale may have no `customer_id`; walk-in is not ambiguous. |
| Costing/COGS | Business rule 1 | Use Northwind unit cost for every unit, on hand or sold. |
| Order discount | Business rule 2 | Apply whole-order discount equally per line; round unit discounted price half-up to cents. |
| Promotion reflection in history | `order_lines.csv` section | Historical `unit_price` already includes item-level promotions. |
| Refund calculation | Business rule 3 | Refund the price actually paid for returned units, using original order discount. |
| Return restocking | Business rule 3 | Good returns increase sellable inventory; damaged returns do not. |
| Supplier ranking | Business rule 4 | Pick lowest `unit_cost` supplier with `lead_time_days <= 10`; slower cheaper suppliers are ineligible. |
| Promotion window | Business rule 5 | Promotion applies only when sale date is within inclusive `[start_date, end_date]`. |
| Promotion stacking | Business rule 5 | If multiple promotions apply, choose the one giving the lower price; no stacking. |
| Revenue | Business rule 6 | Revenue is actual dollars paid after order discounts. |
| Net revenue | Business rule 6 | Revenue kept subtracts refunds issued in that same period. |
| Margin | Business rule 6 | Product margin is revenue from units minus cost of units that stayed sold. Returned-and-restocked units are neither revenue nor cost. |
| Stockout risk | Business rule 7 | Product velocity is May units sold summed across variants; days cover is total product on-hand divided by monthly units / 30; risk if any variant SKU is at/below its own reorder point or product cover is fewer than 14 days. |
| Reorder point comparison | User request for `reorder_low_stock`; dictionary says "falls to or below" in inventory section but tool says "current_qty < reorder_point" | Assumption follows the explicit tool requirement: reorder automation uses strict `< reorder_point`; stockout risk separately uses the dictionary signal. |
| Product-level stockout aggregation | NOT SPECIFIED -- ASSUMPTION | Reorder points live per SKU while stockout risk is reported per product, so a product triggers the reorder-point half of stockout risk when any variant SKU is at or below its own reorder point. |
| Missing payment method | User request, Ask vs. Default | Default to `unspecified` and state that in the receipt. |
| ID allocation | NOT SPECIFIED -- ASSUMPTION | IDs continue the largest numeric suffix already in SQLite: `O-1016`, `R-2002`, `PO-1`, etc. This avoids collisions with seed CSV IDs. |
| Cross-session persistence | User clarification | The database is in-memory by default; state mutations last only for the current CLI process. |
| Synthesized PO date | User request for receive tool | If a described PO does not exist, create it from supplied facts using given date or today. |

## Tool/action layer

The agent exposes these Pydantic-modeled tools:

The tool implementations live in `tools.py`; the Pydantic input schemas live in `models.py`;
the LLM tool orchestration lives in `agent.py`.

| Tool name | Parameters | Description |
|---|---|---|
| `lookup_product` | `query?`, `category?`, `color?`, `size?`, `sku?` | Resolves natural-language product mentions to one sellable SKU. Returns candidates when the mention is genuinely ambiguous. |
| `lookup_customer` | `name?`, `customer_id?` | Resolves a known customer by ID or fuzzy name. Walk-in/no customer means no `customer_id`. |
| `ring_up_sale` | `line_items[{product_descriptor, qty}]`, `customer_descriptor?`, `payment_method?`, `date?`, `order_discount_pct?` | Creates an order and order lines, applies active item promotions, applies any order-level discount, validates inventory, writes sale inventory movements, and returns a receipt. |
| `adjust_sale_quantity` | `order_id`, `qty`, `line_no?`, `date?` | Adjusts a single-line sale quantity for follow-ups such as "make that four instead", preserving the original promo unit price and order discount. |
| `process_return` | `order_id?`, `order_line_id?`, `product_descriptor?`, `qty`, `condition`, `date?` | Resolves a sale line, validates remaining returnable quantity, computes the refund from the original price paid, records the return, and restocks only good-condition returns. |
| `find_returnable_order_lines` | `customer_descriptor`, `product_descriptor` | Finds a customer's returnable order lines when the user does not remember the order ID. If condition is missing, the agent asks before completing the return. |
| `create_promotion` | `scope_type`, `scope_value`, `discount_pct`, `start_date`, `end_date` | Creates a product- or category-scoped percent-off promotion. Promotions apply only to sales dated inside the inclusive window and do not alter past sales. |
| `reorder_low_stock` | `date?` | Finds variants below reorder point, selects the best eligible supplier by rule, groups lines by supplier, and creates purchase orders. |
| `receive_purchase_order` | `po_id?`, `supplier_descriptor?`, `product_descriptor?`, `qty_ordered?`, `qty_received?`, `receive_all?`, `date?` | Receives inventory against an existing open/partial PO, or synthesizes a stated PO when enough facts are provided. `receive_all` marks the remaining quantity as received. |
| `get_top_products_by_margin` | `period`, `limit` | Computes product margin for a period using revenue from units that stayed sold minus frozen Northwind COGS. |
| `get_net_revenue` | `period` | Computes period-level net revenue: dollars paid on orders in the period minus refunds issued in the period. This does not use costs or product-level margin logic. |
| `get_stockout_risk` | `horizon_days?` | Reports products at stockout risk using the shared product-level velocity/days-of-cover calculation plus per-SKU reorder triggers. |
| `check_inventory` | `product_descriptor` | Returns current derived inventory, reorder point, and incoming open/partial POs for a single SKU. |
| `check_inventory_summary` | `product_descriptor`, `color?` | Returns aggregate inventory for product/color queries across variants and the product-level stockout-risk verdict. |

## Ask-vs-default policy

The agent asks only when proceeding would choose between materially different outcomes, such as
ambiguous product variants, multiple matching customers, or multiple matching open POs. Missing
date defaults to `2026-06-19`, missing payment method defaults to `unspecified`, and walk-in means
no customer record. A named customer with no match is an error/clarification, not a walk-in sale.

## Session memory

`SessionMemory` stores the last order, return, customer, product SKUs, purchase order, date, and
tool result for the current process only. The LLM sees this memory each turn so it can resolve
references like "that order" or "same customer"; deterministic tools still validate the resolved IDs.

## LLM boundary

The LLM interprets language, chooses tools, and writes final replies from tool output. Pricing,
refunds, margins, inventory changes, supplier choice, and stockout math live in deterministic
Python in `business_rules.py` and `tools.py`.
