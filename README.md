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

## Inspect with MCP Inspector

```bash
npx -y @modelcontextprotocol/inspector \
  env UV_CACHE_DIR=/tmp/uv-cache uv run --python 3.12 credit-card-recommendation-server
```

## Run tests

```bash
env UV_CACHE_DIR=/tmp/uv-cache uv run --python 3.12 pytest
```
