# Retail Store Agent

Command-line AI agent for a small retail store. The agent uses OpenAI tool calling for language
understanding and deterministic Python/SQLite tools for prices, refunds, inventory, promotions,
reorders, purchase-order receiving, margin, and stockout risk.

## Setup

```bash
git clone <repo>
cd retail-store-agent-takehome
make setup
```

Then edit `.env` and add your OpenAI API key:

```text
OPENAI_API_KEY=sk-...
```

The model is hardcoded to `gpt-5.5` in `agent.py`.

## Run

```bash
make run
```

Each CLI run starts a fresh in-memory SQLite database from the CSVs. Mutations persist only for
the current interactive session; session memory supports follow-ups while the process is running,
but nothing is carried across restarts.

Example:

```text
$ python main.py
Retail Store Agent ready. Type an instruction, or 'quit' to exit.
> Ring up two Classic Tees, Blue Medium, and one Canvas Tote for a walk-in paying cash, dated today.
Created order O-1016 for 2 line(s), total $68.00.
> now refund the tote, it came back damaged
Recorded return R-2002; refund is $18.00.
```

If `make` is unavailable, the equivalent manual commands are:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# add OPENAI_API_KEY to .env
.venv/bin/python main.py
```

## Optional Verification

```bash
make test
```

Tests are not required to start or use the agent. They are included for reviewers who want to
verify the deterministic business rules and LLM tool-selection path.

## Design

See [WRITEUP.md](WRITEUP.md)
