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

The server uses the low-level MCP SDK and exposes one tool named `recommend_credit_card`.

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

## External Data (CTBC_Data)

This repo includes a full `CTBC_Data/` folder at the project root. The server
will auto-detect it if `CTBC_DATA_DIR` is not set. Tests use a minimal subset
under `tests/fixtures/ctbc_data` so they stay fast and deterministic.

If you want to use a custom CTBC dataset location, set `CTBC_DATA_DIR` to the
folder that contains these files:

- `ctbc_cards.json`
- `card_features.json`
- `microsite_deals.json`
- `channels.json`

Example:

```bash
CTBC_DATA_DIR=/tmp/CTBC_Data \
env UV_CACHE_DIR=/tmp/uv-cache uv run --python 3.12 credit-card-recommendation-http-server
```

When the dataset is available, the server automatically switches from the mock
dictionary to the normalized CTBC rules.

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

Expected results:

- `Amazon / online / 3000` -> `BusinessTitaniumCard`, `30.0`
- `Tokyo Donki / physicalForeign / 10000` -> `LinePayCard`, `280.0`
- `Taipei Water / taxAndUtility / 1000` -> `BusinessTitaniumCard`, `3.0`

If the first request is slow, that is usually Render's free-tier cold start.

## Natural Language Tool

The server also exposes `recommend_credit_card_from_text`, which accepts a
single field `userMessage` and parses merchant name, amount, and transaction
type before calling the same recommendation engine.

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
