# Credit Card Recommendation MCP Server

This project is a minimal viable product implemented with the official Python MCP SDK.

## Requirements

- Python 3.11 or newer
- `uv` for dependency management

## Install

```bash
env UV_CACHE_DIR=/tmp/uv-cache uv sync --python 3.12 --extra dev
```

## Run the MCP server

The server uses the low-level MCP SDK and exposes two tools:

- `recommend_credit_card`
- `recommend_credit_card_from_text`

```bash
env UV_CACHE_DIR=/tmp/uv-cache uv run --python 3.12 credit-card-recommendation-server
```

## Run the Streamable HTTP MCP server

This mode is intended for remote clients or deployment behind a stable URL.

```bash
cd /Users/weichengchen/credit_card_recom
CREDIT_CARD_RECOM_HOST=127.0.0.1 \
CREDIT_CARD_RECOM_PORT=8000 \
env UV_CACHE_DIR=/tmp/uv-cache uv run --python 3.12 credit-card-recommendation-http-server
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

The MCP endpoint will be available at:

```text
http://127.0.0.1:8000/mcp
```

The health response also reports which data source is active:

- `dataSource`: `ctbc` or `mock`
- `dataDir`: resolved dataset path (if any)
- `cardCount`: number of cards loaded
- `dataLastUpdated`: dataset timestamp (if available)

## External Data (CTBC_Data)

This repo includes a full `CTBC_Data/` folder at the project root. The server
will auto-detect it if `CTBC_DATA_DIR` is not set. Tests use a minimal subset
under `tests/fixtures/ctbc_data` so they stay fast and deterministic.

If you want to use a custom dataset location, set `CTBC_DATA_DIR` to the
folder that contains either:

- The legacy CTBC layout:
  - `ctbc_cards.json`
  - `card_features.json`
  - `microsite_deals.json`
  - `channels.json`
- Or the newer multi-bank layout:
  - one or more `*_cards.json` files such as `ctbc_cards.json` and `fubon_cards.json`

Example:

```bash
CTBC_DATA_DIR=/tmp/CTBC_Data \
env UV_CACHE_DIR=/tmp/uv-cache uv run --python 3.12 credit-card-recommendation-http-server
```

When the dataset is available, the server automatically switches from the mock
dictionary to normalized bank card rules. The loader currently supports both
the legacy CTBC-only dataset and the newer multi-bank card-list dataset.

## Architecture

The server currently exposes two MCP tools:

- `recommend_credit_card`: strict structured input for deterministic reward calculation
- `recommend_credit_card_from_text`: natural-language input that is parsed into the same internal request shape before reward calculation

Current request flow:

1. MCP client calls one of the two tools.
2. `recommend_credit_card` validates `merchantName`, `transactionAmount`, and `transactionType` in [server.py](/Users/weichengchen/credit_card_recom/src/credit_card_recom_mcp/server.py).
3. `recommend_credit_card_from_text` parses `userMessage` into merchant, amount, transaction type, and candidate cards in [server.py](/Users/weichengchen/credit_card_recom/src/credit_card_recom_mcp/server.py).
4. The server loads normalized CTBC card data from [ctbc_data.py](/Users/weichengchen/credit_card_recom/src/credit_card_recom_mcp/ctbc_data.py). If no dataset is available, it falls back to the in-memory mock rules.
5. Reward calculation compares candidate cards and returns both `TextContent` JSON and MCP `structuredContent`.

Current data normalization flow:

1. `load_raw_data()` scans all available `*_cards.json` files and also supports the legacy CTBC side files when present.
2. `normalize_cards()` extracts base rates, channel rates, caps, thresholds, and conditional rules from the raw bank card data.
3. `merge_card_aliases()` maps legacy names such as `LinePayCard` and `BusinessTitaniumCard` onto real card records when those aliases exist in the current dataset.
4. `apply_baseline_fallbacks()` fills gaps for the baseline demo cards so recommendation behavior stays stable when some scraped fields are incomplete.
5. `build_merchant_index()` creates the merchant/category lookup from both legacy channel definitions and per-card merchant lists used by the natural-language tool.

Current design boundaries:

- The server owns reward computation and dataset normalization across the currently supported bank card files.
- The natural-language tool is intentionally lightweight and rule-based. It is designed for MCP hosts that do not reliably transform free text into structured tool arguments.
- The engine now enforces straightforward rule constraints that can be applied deterministically from scraped data: minimum spend, explicit cashback caps, simple spend caps, and rule validity dates.
- Conditional promotions are still preserved separately when they require registration, bundle conditions, or text that cannot yet be normalized safely into deterministic computation.

## Cursor Remote Usage

The deployed Render service is available at:

```text
https://credit-card-recom.onrender.com/mcp
```

To use this MCP server from Cursor, create `.cursor/mcp.json` in your project
or update `~/.cursor/mcp.json` with:

```json
{
  "mcpServers": {
    "credit-card-recom": {
      "url": "https://credit-card-recom.onrender.com/mcp"
    }
  }
}
```

Then reload Cursor and test in the chat panel with prompts like:

```text
請使用 recommend_credit_card 工具，merchantName=Amazon、transactionAmount=3000、transactionType=online
```

```text
請使用 recommend_credit_card 工具，merchantName=Tokyo Donki、transactionAmount=10000、transactionType=physicalForeign
```

```text
請使用 recommend_credit_card 工具，merchantName=Taipei Water、transactionAmount=1000、transactionType=taxAndUtility
```

Expected results when the server is still using the built-in mock rules:

- `Amazon / online / 3000` -> `BusinessTitaniumCard`, `30.0`
- `Tokyo Donki / physicalForeign / 10000` -> `LinePayCard`, `280.0`
- `Taipei Water / taxAndUtility / 1000` -> `BusinessTitaniumCard`, `3.0`

Expected results when the server is using the bundled multi-bank dataset:

- `我代扣中華電信費用 1000 元，我只有中華電信聯名卡` -> `中華電信聯名卡`, `20.0`
- `我在 Agoda 訂房 20000 元，我有富邦J Travel卡和LINE Pay信用卡` -> `富邦J Travel卡`, `1200.0`
- `我在 Costco 線上商店買 10000 元，我有富邦Costco聯名卡和LINE Pay信用卡` -> `富邦Costco聯名卡`, `300.0`

If the first request is slow, that is usually Render's free-tier cold start.

## Natural Language Tool

The server also exposes `recommend_credit_card_from_text`, which accepts a
single field `userMessage` and parses merchant name, amount, transaction
type, merchant channel, and candidate cards before calling the same
recommendation engine.

If your MCP host does not auto-call tools, explicitly ask it to use the tool
or select the tool from the UI (for example: "請使用 recommend_credit_card_from_text
工具，userMessage=...").

## Local Bridge For MCP Hosts Without Remote URL Support

Some MCP hosts do not support remote MCP URLs yet and only accept local
`command` / `args` servers. For those hosts, run the included bridge and point
it at your deployed Render URL.

Example host config:

```json
{
  "mcpServers": {
    "credit-card-recom-render": {
      "command": "env",
      "args": [
        "UV_CACHE_DIR=/tmp/uv-cache",
        "REMOTE_MCP_URL=https://credit-card-recom.onrender.com/mcp",
        "uv",
        "run",
        "--directory",
        "/Users/weichengchen/credit_card_recom",
        "--python",
        "3.12",
        "credit-card-recommendation-remote-bridge"
      ]
    }
  }
}
```

The bridge is a local stdio process, but every tool call is forwarded to the
remote Render deployment.

## Inspect with MCP Inspector

```bash
npx -y @modelcontextprotocol/inspector \
  env UV_CACHE_DIR=/tmp/uv-cache uv run --python 3.12 credit-card-recommendation-server
```

## Run tests

```bash
env UV_CACHE_DIR=/tmp/uv-cache uv run --python 3.12 pytest
```
