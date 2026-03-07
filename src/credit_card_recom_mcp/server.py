"""Minimal MCP server for recommending the best credit card reward.

This module uses the official low-level Python MCP SDK so the tool schema can
be declared explicitly and kept strict. The server exposes a single tool,
`recommend_credit_card`, which returns a JSON string for backward-compatible
clients and structured JSON data for newer MCP clients.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

SERVER_NAME = "credit-card-recommendation-server"
SERVER_VERSION = "0.1.0"
TOOL_NAME = "recommend_credit_card"
TRANSACTION_TYPES = {"online", "physicalForeign", "taxAndUtility"}
MONEY_PRECISION = Decimal("0.01")
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8000
DEFAULT_HTTP_PATH = "/mcp"

# In-memory mock database. Rates are stored as Decimal-friendly strings to keep
# the reward calculation deterministic and easy to audit.
CREDIT_CARD_DATABASE: dict[str, dict[str, Decimal]] = {
    "LinePayCard": {
        "online": Decimal("0.005"),
        "physicalForeign": Decimal("0.028"),
        "taxAndUtility": Decimal("0.000"),
    },
    "BusinessTitaniumCard": {
        "online": Decimal("0.010"),
        "physicalForeign": Decimal("0.025"),
        "taxAndUtility": Decimal("0.003"),
    },
}

TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "merchantName": {
            "type": "string",
            "description": "商家名稱。",
            "minLength": 1,
        },
        "transactionAmount": {
            "type": "integer",
            "description": "消費金額。",
            "minimum": 0,
        },
        "transactionType": {
            "type": "string",
            "description": "交易型態。",
            "enum": ["online", "physicalForeign", "taxAndUtility"],
        },
    },
    "required": ["merchantName", "transactionAmount", "transactionType"],
}

TOOL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "recommendedCard": {
            "type": "string",
            "description": "最佳卡片名稱。",
        },
        "estimatedRewardAmount": {
            "type": "number",
            "description": "預估回饋金額。",
        },
        "reasoning": {
            "type": "string",
            "description": "推薦原因。",
        },
    },
    "required": ["recommendedCard", "estimatedRewardAmount", "reasoning"],
}


@dataclass(frozen=True, slots=True)
class RecommendationRequest:
    """Validated request payload for the recommendation tool."""

    merchant_name: str
    transaction_amount: int
    transaction_type: str


def validate_recommendation_arguments(arguments: Mapping[str, Any] | None) -> RecommendationRequest:
    """Validate raw MCP tool arguments against the strict business contract.

    The MCP schema helps clients up front, but we still validate on the server
    side so the business rules stay correct even if a client bypasses schema
    checks or calls the internal function directly.
    """

    if arguments is None:
        raise ValueError("Tool arguments are required.")

    expected_fields = {"merchantName", "transactionAmount", "transactionType"}
    actual_fields = set(arguments.keys())
    if actual_fields != expected_fields:
        unexpected = sorted(actual_fields - expected_fields)
        missing = sorted(expected_fields - actual_fields)
        errors: list[str] = []
        if missing:
            errors.append(f"missing fields: {', '.join(missing)}")
        if unexpected:
            errors.append(f"unexpected fields: {', '.join(unexpected)}")
        raise ValueError("Invalid input payload; " + "; ".join(errors) + ".")

    merchant_name = arguments["merchantName"]
    transaction_amount = arguments["transactionAmount"]
    transaction_type = arguments["transactionType"]

    if not isinstance(merchant_name, str) or not merchant_name.strip():
        raise ValueError("merchantName must be a non-empty string.")

    if isinstance(transaction_amount, bool) or not isinstance(transaction_amount, int):
        raise ValueError("transactionAmount must be an integer.")
    if transaction_amount < 0:
        raise ValueError("transactionAmount must be greater than or equal to 0.")

    if not isinstance(transaction_type, str) or transaction_type not in TRANSACTION_TYPES:
        raise ValueError(
            "transactionType must be one of: online, physicalForeign, taxAndUtility."
        )

    return RecommendationRequest(
        merchant_name=merchant_name.strip(),
        transaction_amount=transaction_amount,
        transaction_type=transaction_type,
    )


def calculate_reward(transaction_amount: int, reward_rate: Decimal) -> Decimal:
    """Calculate reward with two-decimal currency precision."""

    raw_reward = Decimal(transaction_amount) * reward_rate
    return raw_reward.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)


def build_reasoning(
    request: RecommendationRequest,
    recommended_card: str,
    reward_by_card: Mapping[str, Decimal],
) -> str:
    """Produce a human-readable explanation for the final recommendation."""

    if request.transaction_type == "taxAndUtility":
        return (
            f"{request.merchant_name} 屬於 taxAndUtility 類型，LinePayCard 依規則不提供回饋，"
            f"因此系統強制推薦 BusinessTitaniumCard；預估回饋為 "
            f"{reward_by_card['BusinessTitaniumCard']:.2f}。"
        )

    comparison_text = (
        f"LinePayCard 預估回饋 {reward_by_card['LinePayCard']:.2f}，"
        f"BusinessTitaniumCard 預估回饋 {reward_by_card['BusinessTitaniumCard']:.2f}。"
    )

    if request.transaction_type == "physicalForeign":
        return (
            f"{request.merchant_name} 為國外實體消費，已套用國外實體加碼回饋率精算。"
            f"{comparison_text} 因此推薦 {recommended_card}。"
        )

    return (
        f"{request.merchant_name} 為一般線上消費，依各卡國內一般消費回饋率計算。"
        f"{comparison_text} 因此推薦 {recommended_card}。"
    )


def get_recommendation_payload(request: RecommendationRequest) -> dict[str, Any]:
    """Compute the best card and shape the structured result payload."""

    reward_by_card = {
        card_name: calculate_reward(
            transaction_amount=request.transaction_amount,
            reward_rate=card_rules[request.transaction_type],
        )
        for card_name, card_rules in CREDIT_CARD_DATABASE.items()
    }

    # Tax and utility must always recommend the business card even if another
    # card somehow ties after future rule changes.
    if request.transaction_type == "taxAndUtility":
        recommended_card = "BusinessTitaniumCard"
    else:
        recommended_card = max(
            reward_by_card,
            key=lambda card_name: (reward_by_card[card_name], card_name == "BusinessTitaniumCard"),
        )

    reasoning = build_reasoning(
        request=request,
        recommended_card=recommended_card,
        reward_by_card=reward_by_card,
    )

    return {
        "recommendedCard": recommended_card,
        "estimatedRewardAmount": float(reward_by_card[recommended_card]),
        "reasoning": reasoning,
    }


server = Server(SERVER_NAME)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """Register the server's single recommendation tool with strict schemas."""

    return [
        types.Tool(
            name=TOOL_NAME,
            description="依照商家、金額與交易型態推薦最佳信用卡。",
            inputSchema=TOOL_INPUT_SCHEMA,
            outputSchema=TOOL_OUTPUT_SCHEMA,
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    """Execute the recommendation tool and return both text and structured data."""

    if name != TOOL_NAME:
        raise ValueError(f"Unknown tool: {name}")

    request = validate_recommendation_arguments(arguments)
    payload = get_recommendation_payload(request)

    # The user asked for a standard string containing structured JSON. We return
    # that string in TextContent while also attaching structuredContent so newer
    # MCP clients can consume typed data directly.
    payload_text = json.dumps(payload, ensure_ascii=False)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=payload_text)],
        structuredContent=payload,
    )


async def run_stdio_server() -> None:
    """Run the MCP server over stdio for local tooling and inspector usage."""

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            create_initialization_options(),
        )


def create_initialization_options() -> InitializationOptions:
    """Build the MCP initialization metadata shared by all transports."""

    return InitializationOptions(
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )


class StreamableHTTPASGIApp:
    """Minimal ASGI wrapper that forwards requests to the MCP session manager."""

    def __init__(self, session_manager: StreamableHTTPSessionManager):
        self._session_manager = session_manager

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self._session_manager.handle_request(scope, receive, send)


async def healthcheck(_: Request) -> JSONResponse:
    """Simple HTTP health endpoint for deployment and smoke testing."""

    return JSONResponse(
        {
            "status": "ok",
            "serverName": SERVER_NAME,
            "serverVersion": SERVER_VERSION,
            "mcpPath": DEFAULT_HTTP_PATH,
        }
    )


def create_streamable_http_app(
    *,
    path: str = DEFAULT_HTTP_PATH,
    json_response: bool = True,
    stateless_http: bool = True,
) -> Starlette:
    """Create a Starlette app that serves the MCP server over Streamable HTTP."""

    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=json_response,
        stateless=stateless_http,
    )
    mcp_asgi_app = StreamableHTTPASGIApp(session_manager)

    return Starlette(
        routes=[
            Route("/health", endpoint=healthcheck, methods=["GET"]),
            Route(path, endpoint=mcp_asgi_app),
        ],
        lifespan=lambda app: session_manager.run(),
    )


async def run_streamable_http_server() -> None:
    """Run the MCP server as a Streamable HTTP service."""

    import uvicorn

    host = os.getenv("CREDIT_CARD_RECOM_HOST", DEFAULT_HTTP_HOST)
    port = int(os.getenv("CREDIT_CARD_RECOM_PORT", str(DEFAULT_HTTP_PORT)))
    app = create_streamable_http_app()
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server_instance = uvicorn.Server(config)
    await server_instance.serve()


def main() -> None:
    """CLI entry point for the stdio transport."""

    asyncio.run(run_stdio_server())


def main_http() -> None:
    """CLI entry point for the Streamable HTTP transport."""

    asyncio.run(run_streamable_http_server())


if __name__ == "__main__":
    main()
