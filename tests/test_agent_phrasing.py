from __future__ import annotations

import os

import pytest

from agent import Agent, load_env_file
from db import init_db
from memory import SessionMemory
from tools import RetailTools


load_env_file()
pytestmark = pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="OPENAI_API_KEY required for full LLM phrasing tests")


@pytest.fixture()
def agent(tmp_path):
    class SpyRetailTools(RetailTools):
        def __init__(self, conn):
            super().__init__(conn)
            self.calls = []

        def __getattribute__(self, name):
            attr = object.__getattribute__(self, name)
            if name in {
                "lookup_product",
                "lookup_customer",
                "ring_up_sale",
                "adjust_sale_quantity",
                "process_return",
                "find_returnable_order_lines",
                "create_promotion",
                "reorder_low_stock",
                "receive_purchase_order",
                "get_top_products_by_margin",
                "get_net_revenue",
                "get_stockout_risk",
                "check_inventory",
                "check_inventory_summary",
            } and callable(attr):
                def wrapper(*args, **kwargs):
                    self.calls.append((name, args, kwargs))
                    return attr(*args, **kwargs)

                return wrapper
            return attr

    tools = SpyRetailTools(init_db(tmp_path / "store.db"))
    return Agent(tools, SessionMemory()), tools


@pytest.mark.parametrize(
    "prompt,expected_tool",
    [
        ("Ring up two Classic Tees, Blue Medium, and one Canvas Tote for a walk-in paying cash, dated today.", "ring_up_sale"),
        ("Sell a blue medium classic tee pair plus a tote, cash, no customer.", "ring_up_sale"),
        ("Checkout 2 Classic Tee blue M and 1 Canvas Tote for a walk-in.", "ring_up_sale"),
        ("Ring up ten Canvas Totes for a walk-in.", "ring_up_sale"),
        ("Try to sell 10 totes with no customer attached.", "ring_up_sale"),
        ("Sell Sarah Chen one medium hoodie.", "ring_up_sale"),
        ("Ring up a hoodie in medium for customer Sarah Chen.", "ring_up_sale"),
        ("Reorder anything under its reorder threshold today.", "reorder_low_stock"),
        ("Create restock POs for every item below reorder point.", "reorder_low_stock"),
        ("A purchase order for 50 Canvas Totes from Northwind is open and 40 arrived today.", "receive_purchase_order"),
        ("Receive 40 units against an open Northwind PO for 50 Canvas Totes.", "receive_purchase_order"),
        ("Sarah Chen is returning one Navy Large hoodie from order O-1006. It's in good condition.", "process_return"),
        ("Process a good return for 1 navy large hoodie on O-1006.", "process_return"),
        ("Return the Canvas Tote from order O-1006; it is damaged.", "process_return"),
        ("The tote on O-1006 came back damaged, refund it.", "process_return"),
        ("Put all hoodies on 20% off from 2026-06-20 to 2026-06-22, then ring up one Gray Medium hoodie dated 2026-06-21 and tell me the price.", "create_promotion"),
        ("Create a 20 percent hoodie sale June 20 through June 22, then sell a gray medium hoodie on June 21.", "ring_up_sale"),
        ("What were my top five products by profit margin last month?", "get_top_products_by_margin"),
        ("Show the five best margin products for May 2026.", "get_top_products_by_margin"),
        ("What's about to stock out?", "get_stockout_risk"),
        ("Which products are running out soon?", "get_stockout_risk"),
        ("How many canvas totes are on hand?", "check_inventory_summary"),
        ("Check current inventory for Canvas Tote.", "check_inventory"),
        ("Ring up a hoodie for Marcus Reed.", "ring_up_sale"),
        ("Ring up three Wool Socks for Priya Patel with a 15% discount.", "ring_up_sale"),
        ("Marcus Reed wants to return a mug; I don't remember which order.", "find_returnable_order_lines"),
        ("Put everything in the goods category on 10% off starting tomorrow for a week.", "create_promotion"),
        ("What was our net revenue in May after refunds?", "get_net_revenue"),
        ("Ring up two Classic Tees, Blue Small, dated May 3rd, 2026.", "ring_up_sale"),
        ("The Northwind order for Canvas Totes came in complete; mark it received, dated today.", "receive_purchase_order"),
        ("How many Gray Hoodies do we have across both sizes, and are we at risk of running out?", "check_inventory_summary"),
    ],
)
def test_llm_paraphrases_call_tools(agent, prompt, expected_tool):
    agent_obj, spy_tools = agent
    agent_obj.run_turn(prompt)
    called = [name for name, _args, _kwargs in spy_tools.calls]
    assert expected_tool in called


def test_llm_follow_up_quantity_correction_uses_session_memory(agent):
    agent_obj, spy_tools = agent
    agent_obj.run_turn("Ring up three Wool Socks for Priya Patel with a 15% discount.")
    spy_tools.calls.clear()
    agent_obj.run_turn("Actually, make that four instead.")
    called = [name for name, _args, _kwargs in spy_tools.calls]
    assert "adjust_sale_quantity" in called
