from __future__ import annotations

import sys

from agent import Agent
from db import init_db
from memory import SessionMemory
from tools import RetailTools


def main() -> int:
    try:
        conn = init_db()
        agent = Agent(RetailTools(conn), SessionMemory())
    except Exception as exc:
        print(f"Startup error: {exc}", file=sys.stderr)
        return 1
    print("Retail Store Agent ready. Type an instruction, or 'quit' to exit.")
    while True:
        try:
            user_text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if user_text.lower() in {"quit", "exit"}:
            return 0
        if not user_text:
            continue
        try:
            print(agent.run_turn(user_text))
        except Exception as exc:
            print(f"Agent error: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
