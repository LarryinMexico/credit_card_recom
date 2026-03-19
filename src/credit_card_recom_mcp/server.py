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
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping

import mcp.server.stdio
import mcp.types as types
from credit_card_recom_mcp.ctbc_data import (
    NormalizedCard,
    NormalizedData,
    RewardRule,
    get_data_status,
    get_normalized_data,
    is_rule_active,
)
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
TOOL_NAME_TEXT = "recommend_credit_card_from_text"
TRANSACTION_TYPES = {"online", "physicalForeign", "taxAndUtility"}
MONEY_PRECISION = Decimal("0.01")
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8000
DEFAULT_HTTP_PATH = "/mcp"
CHANNEL_KEYWORD_HINTS: dict[str, tuple[str, ...]] = {
    "travel": ("agoda", "booking", "trip.com", "hotel", "飯店", "旅館", "住宿", "訂房", "機票", "航空", "旅行社"),
    "wholesale": ("costco", "好市多"),
    "ecommerce": ("momo", "蝦皮", "pchome", "yahoo購物", "購物網", "網購", "電商"),
    "food_delivery": ("foodpanda", "uber eats", "ubereats", "外送"),
    "dining": ("星巴克", "咖啡", "餐廳", "吃飯", "麥當勞", "肯德基"),
    "insurance": ("保費", "保險"),
    "telecom": ("電信", "台灣大哥大", "台哥大", "台灣之星", "myvideo", "中華電信"),
}

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

TOOL_TEXT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "userMessage": {
            "type": "string",
            "description": "自然語言描述的消費情境。",
            "minLength": 1,
        }
    },
    "required": ["userMessage"],
}


@dataclass(frozen=True, slots=True)
class RecommendationRequest:
    """Validated request payload for the recommendation tool."""

    merchant_name: str
    transaction_amount: int
    transaction_type: str
    merchant_channel: str | None = None
    allowed_cards: list[str] | None = None


@dataclass(frozen=True, slots=True)
class ParsedTextRequest:
    merchant_name: str
    transaction_amount: int
    transaction_type: str
    merchant_channel: str | None
    allowed_cards: list[str] | None
    inferred_fields: list[str]


@dataclass(frozen=True, slots=True)
class AppliedReward:
    amount: Decimal
    rule: RewardRule | None
    notes: tuple[str, ...] = ()


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


def _evaluate_reward_rule(
    transaction_amount: int,
    rule: RewardRule,
) -> AppliedReward | None:
    if not is_rule_active(rule):
        return None

    notes: list[str] = []
    amount = Decimal(transaction_amount)
    if rule.min_spend_amount is not None and amount < rule.min_spend_amount:
        return None
    if rule.min_spend_amount is not None:
        notes.append(f"已套用最低消費門檻 {rule.min_spend_amount:.0f}")

    eligible_amount = amount
    if rule.spend_cap_amount is not None and eligible_amount > rule.spend_cap_amount:
        eligible_amount = rule.spend_cap_amount
        notes.append(f"已套用可計回饋消費上限 {rule.spend_cap_amount:.0f}")

    reward = calculate_reward(int(eligible_amount), rule.rate)
    if rule.reward_cap_amount is not None and reward > rule.reward_cap_amount:
        reward = rule.reward_cap_amount.quantize(MONEY_PRECISION)
        notes.append(f"已套用回饋上限 {rule.reward_cap_amount:.0f}")

    return AppliedReward(amount=reward, rule=rule, notes=tuple(notes))


def _select_best_rule_reward(
    request: RecommendationRequest,
    card: NormalizedCard,
) -> AppliedReward:
    candidates: list[RewardRule] = list(card.base_rules.get(request.transaction_type, []))
    if request.merchant_channel:
        candidates.extend(card.channel_rules.get(request.merchant_channel, []))

    best_result: AppliedReward | None = None
    for rule in candidates:
        evaluated = _evaluate_reward_rule(request.transaction_amount, rule)
        if evaluated is None:
            continue
        if best_result is None or evaluated.amount > best_result.amount:
            best_result = evaluated

    if best_result is not None:
        return best_result

    if candidates:
        return AppliedReward(amount=Decimal("0.00"), rule=None)

    fallback_rate = card.base_rates.get(request.transaction_type, Decimal("0.0"))
    return AppliedReward(
        amount=calculate_reward(request.transaction_amount, fallback_rate),
        rule=None,
    )


def build_reasoning(
    request: RecommendationRequest,
    recommended_card: str,
    reward_by_card: Mapping[str, Decimal],
    notes_by_card: Mapping[str, tuple[str, ...]] | None = None,
) -> str:
    """Produce a human-readable explanation for the final recommendation."""

    if request.transaction_type == "taxAndUtility":
        linepay_reward = reward_by_card.get("LinePayCard")
        business_reward = reward_by_card.get("BusinessTitaniumCard")
        if (
            recommended_card == "BusinessTitaniumCard"
            and linepay_reward is not None
            and business_reward is not None
            and len(reward_by_card) <= 2
        ):
            return (
                f"{request.merchant_name} 屬於 taxAndUtility 類型，LinePayCard 依規則不提供回饋，"
                f"因此系統強制推薦 BusinessTitaniumCard；預估回饋為 "
                f"{business_reward:.2f}。"
            )
        return (
            f"{request.merchant_name} 屬於 taxAndUtility 類型，依資料規則推薦 {recommended_card}；"
            f"預估回饋為 {reward_by_card[recommended_card]:.2f}。"
        )

    sorted_cards = sorted(reward_by_card.items(), key=lambda item: item[1], reverse=True)
    top_two = sorted_cards[:2]
    comparison_text = "；".join(
        f"{card_name} 預估回饋 {reward:.2f}" for card_name, reward in top_two
    )

    if request.transaction_type == "physicalForeign":
        reasoning = (
            f"{request.merchant_name} 為國外實體消費，已套用國外實體回饋率精算。"
            f"{comparison_text}。因此推薦 {recommended_card}。"
        )
        if notes_by_card and notes_by_card.get(recommended_card):
            return f"{reasoning}（{'；'.join(notes_by_card[recommended_card])}）"
        return reasoning

    reasoning = (
        f"{request.merchant_name} 為一般線上消費，依各卡國內一般消費回饋率計算。"
        f"{comparison_text}。因此推薦 {recommended_card}。"
    )
    if notes_by_card and notes_by_card.get(recommended_card):
        return f"{reasoning}（{'；'.join(notes_by_card[recommended_card])}）"
    return reasoning


def get_recommendation_payload(request: RecommendationRequest) -> dict[str, Any]:
    """Compute the best card and shape the structured result payload."""

    normalized_data = get_normalized_data()
    if normalized_data is None:
        return _get_payload_from_mock(request)
    return _get_payload_from_normalized_data(request, normalized_data)


def _get_payload_from_mock(request: RecommendationRequest) -> dict[str, Any]:
    reward_by_card = {
        card_name: calculate_reward(
            transaction_amount=request.transaction_amount,
            reward_rate=card_rules[request.transaction_type],
        )
        for card_name, card_rules in CREDIT_CARD_DATABASE.items()
    }

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


def _filter_cards(
    normalized_data: NormalizedData,
    allowed_cards: list[str] | None,
) -> list[NormalizedCard]:
    if not allowed_cards:
        return normalized_data.cards

    matched: list[NormalizedCard] = []
    for card in normalized_data.cards:
        for candidate in allowed_cards:
            if candidate in card.card_name or candidate in card.aliases:
                matched.append(card)
                break
    return matched if matched else normalized_data.cards


def _get_payload_from_normalized_data(
    request: RecommendationRequest,
    normalized_data: NormalizedData,
) -> dict[str, Any]:
    cards = _filter_cards(normalized_data, request.allowed_cards)

    reward_by_card: dict[str, Decimal] = {}
    notes_by_card: dict[str, tuple[str, ...]] = {}
    for card in cards:
        applied_reward = _select_best_rule_reward(request, card)
        reward_by_card[card.card_name] = applied_reward.amount
        notes_by_card[card.card_name] = applied_reward.notes

    if request.transaction_type == "taxAndUtility":
        recommended_card = max(reward_by_card, key=reward_by_card.get)
    else:
        recommended_card = max(reward_by_card, key=reward_by_card.get)

    reasoning = build_reasoning(
        request=request,
        recommended_card=recommended_card,
        reward_by_card=reward_by_card,
        notes_by_card=notes_by_card,
    )

    return {
        "recommendedCard": recommended_card,
        "estimatedRewardAmount": float(reward_by_card[recommended_card]),
        "reasoning": reasoning,
    }


def _extract_amount(text: str) -> int | None:
    match = re.search(r"(\d{1,9})\s*元", text)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d{1,9})", text)
    if match:
        return int(match.group(1))
    return _parse_chinese_amount(text)


def _parse_chinese_amount(text: str) -> int | None:
    mapping = {
        "零": 0,
        "一": 1,
        "二": 2,
        "兩": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    unit_map = {"十": 10, "百": 100, "千": 1000, "萬": 10000}
    total = 0
    current = 0
    found = False
    for ch in text:
        if ch in mapping:
            current = mapping[ch]
            found = True
        elif ch in unit_map:
            found = True
            unit = unit_map[ch]
            if current == 0:
                current = 1
            total += current * unit
            current = 0
    total += current
    return total if found else None


def _infer_transaction_type(text: str, merchant_channel: str | None) -> tuple[str, list[str]]:
    inferred: list[str] = []
    if any(token in text for token in ("保費", "保險")) or merchant_channel == "insurance":
        inferred.append("transactionType")
        return "online", inferred
    if any(token in text for token in ("水電", "瓦斯", "電信", "稅", "代扣", "學雜費", "公用事業")):
        inferred.append("transactionType")
        return "taxAndUtility", inferred
    if any(token in text for token in ("國外", "海外", "出國", "日本", "美國", "境外")):
        inferred.append("transactionType")
        return "physicalForeign", inferred
    if any(token in text for token in ("線上", "網購", "網路", "電商", "線上購物")):
        inferred.append("transactionType")
        return "online", inferred
    if merchant_channel == "ecommerce":
        inferred.append("transactionType")
        return "online", inferred
    return "online", inferred


def _infer_merchant_channel(text: str, normalized_data: NormalizedData | None) -> str | None:
    if normalized_data is None:
        return None
    lookup = normalized_data.merchant_index
    merchant_lower = text.lower().replace(" ", "")
    for key, channel_id in lookup.items():
        if key.replace(" ", "") in merchant_lower:
            return channel_id
    for channel_id, keywords in CHANNEL_KEYWORD_HINTS.items():
        if any(keyword.replace(" ", "") in merchant_lower for keyword in keywords):
            return channel_id
    return None


def _extract_merchant(text: str, normalized_data: NormalizedData | None) -> str | None:
    if normalized_data is not None:
        normalized_text = text.lower().replace(" ", "")
        for key in normalized_data.merchant_index.keys():
            if key.replace(" ", "") in normalized_text:
                return key
    match = re.search(r"[在於去到]\s*([^\s，。,]+)", text)
    if match:
        return match.group(1)
    return None


def _extract_allowed_cards(text: str, normalized_data: NormalizedData | None) -> list[str] | None:
    if normalized_data is None:
        return None
    normalized_text = text.lower().replace(" ", "")
    matched: list[str] = []
    for card in normalized_data.cards:
        card_key = card.card_name.lower().replace(" ", "")
        if card_key and card_key in normalized_text:
            matched.append(card.card_name)
    for alias, card in normalized_data.cards_by_alias.items():
        alias_key = alias.lower().replace(" ", "")
        if alias_key in normalized_text and card.card_name not in matched:
            matched.append(card.card_name)
    return matched or None


def parse_text_request(user_message: str) -> ParsedTextRequest:
    normalized_data = get_normalized_data()
    merchant = _extract_merchant(user_message, normalized_data) or "未指定商家"
    amount = _extract_amount(user_message)
    if amount is None:
        raise ValueError("無法從描述中解析消費金額，請補充金額。")
    merchant_channel = _infer_merchant_channel(user_message, normalized_data)
    transaction_type, inferred = _infer_transaction_type(user_message, merchant_channel)
    allowed_cards = _extract_allowed_cards(user_message, normalized_data)
    return ParsedTextRequest(
        merchant_name=merchant,
        transaction_amount=amount,
        transaction_type=transaction_type,
        merchant_channel=merchant_channel,
        allowed_cards=allowed_cards,
        inferred_fields=inferred,
    )


server = Server(SERVER_NAME)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """Register the server's single recommendation tool with strict schemas."""

    return [
        types.Tool(
            name=TOOL_NAME,
            description="依照商家、金額與交易型態推薦最佳信用卡（適用於已提供結構化欄位的請求）。",
            inputSchema=TOOL_INPUT_SCHEMA,
            outputSchema=TOOL_OUTPUT_SCHEMA,
        ),
        types.Tool(
            name=TOOL_NAME_TEXT,
            description="從自然語言描述解析消費情境並推薦最佳信用卡（當使用者以自然語言提問時請呼叫此工具）。",
            inputSchema=TOOL_TEXT_INPUT_SCHEMA,
            outputSchema=TOOL_OUTPUT_SCHEMA,
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    """Execute the recommendation tool and return both text and structured data."""

    if name not in {TOOL_NAME, TOOL_NAME_TEXT}:
        raise ValueError(f"Unknown tool: {name}")

    if name == TOOL_NAME_TEXT:
        user_message = arguments.get("userMessage") if arguments else None
        if not isinstance(user_message, str) or not user_message.strip():
            raise ValueError("userMessage must be a non-empty string.")
        parsed = parse_text_request(user_message)
        request = RecommendationRequest(
            merchant_name=parsed.merchant_name,
            transaction_amount=parsed.transaction_amount,
            transaction_type=parsed.transaction_type,
            merchant_channel=parsed.merchant_channel,
            allowed_cards=parsed.allowed_cards,
        )
    else:
        request = validate_recommendation_arguments(arguments)
        normalized_data = get_normalized_data()
        merchant_channel = _infer_merchant_channel(request.merchant_name, normalized_data)
        if merchant_channel:
            request = RecommendationRequest(
                merchant_name=request.merchant_name,
                transaction_amount=request.transaction_amount,
                transaction_type=request.transaction_type,
                merchant_channel=merchant_channel,
                allowed_cards=None,
            )

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

    data_status = get_data_status()
    return JSONResponse(
        {
            "status": "ok",
            "serverName": SERVER_NAME,
            "serverVersion": SERVER_VERSION,
            "mcpPath": DEFAULT_HTTP_PATH,
            "dataSource": data_status.source,
            "dataDir": data_status.data_dir,
            "cardCount": data_status.card_count,
            "dataLastUpdated": data_status.last_updated,
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
