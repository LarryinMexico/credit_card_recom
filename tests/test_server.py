"""Tests for the credit card recommendation MCP server."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from credit_card_recom_mcp import remote_bridge
from credit_card_recom_mcp.ctbc_data import get_normalized_data, reset_normalized_cache
from credit_card_recom_mcp.server import (
    RecommendationRequest,
    TOOL_INPUT_SCHEMA,
    TOOL_NAME,
    TOOL_NAME_TEXT,
    create_streamable_http_app,
    get_recommendation_payload,
    parse_text_request,
    validate_recommendation_arguments,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ctbc_data"


def _write_dataset(tmp_path: Path, cards: list[dict[str, object]]) -> Path:
    data_dir = tmp_path / "ctbc_data"
    data_dir.mkdir()
    (data_dir / "ctbc_cards.json").write_text(
        json.dumps({"last_updated": "2026-03-19", "cards": cards}, ensure_ascii=False),
        encoding="utf-8",
    )
    (data_dir / "card_features.json").write_text(
        json.dumps({"cards": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (data_dir / "microsite_deals.json").write_text(
        json.dumps({"cards": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (data_dir / "channels.json").write_text(
        json.dumps(
            {
                "channel_categories": {
                    "ecommerce": {"merchants": ["測試商城"], "synonyms": ["網購"]},
                    "tax_utility": {"merchants": ["中華電信"], "synonyms": ["電信"]},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return data_dir


def _write_multi_bank_dataset(
    tmp_path: Path,
    *,
    ctbc_cards: list[dict[str, object]],
    fubon_cards: list[dict[str, object]],
) -> Path:
    data_dir = tmp_path / "multi_bank_data"
    data_dir.mkdir()
    (data_dir / "ctbc_cards.json").write_text(
        json.dumps(
            {
                "version": "1.0",
                "last_updated": "2026-03-19",
                "bank": "CTBC",
                "cards": ctbc_cards,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (data_dir / "fubon_cards.json").write_text(
        json.dumps(
            {
                "version": "1.0",
                "last_updated": "2026-03-20",
                "bank": "Fubon",
                "cards": fubon_cards,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return data_dir


@pytest.fixture(autouse=True)
def ctbc_fixture_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CTBC_DATA_DIR", str(FIXTURE_DIR))
    reset_normalized_cache()
    yield
    reset_normalized_cache()


def test_validate_recommendation_arguments_rejects_extra_fields() -> None:
    """The strict schema contract must reject unexpected properties."""

    assert TOOL_INPUT_SCHEMA["additionalProperties"] is False

    with pytest.raises(ValueError, match="unexpected fields"):
        validate_recommendation_arguments(
            {
                "merchantName": "LINE Pay",
                "transactionAmount": 1000,
                "transactionType": "online",
                "extra": "not-allowed",
            }
        )


def test_tax_and_utility_forces_business_titanium() -> None:
    """taxAndUtility must always recommend the business card."""

    payload = get_recommendation_payload(
        validate_recommendation_arguments(
            {
                "merchantName": "Taipei Water",
                "transactionAmount": 1000,
                "transactionType": "taxAndUtility",
            }
        )
    )

    assert payload["recommendedCard"] == "中信商旅鈦金卡"
    assert payload["estimatedRewardAmount"] == pytest.approx(3.0)


def test_normalized_data_loads_fixture() -> None:
    data = get_normalized_data()
    assert data is not None
    assert "LINE Pay信用卡" in data.cards_by_name
    assert "LinePayCard" in data.cards_by_alias
    assert "BusinessTitaniumCard" in data.cards_by_alias


def test_parse_text_request_infers_amount_and_type() -> None:
    parsed = parse_text_request("我在餐廳吃飯刷卡一千元")
    assert parsed.transaction_amount == 1000
    assert parsed.transaction_type == "online"


def test_reward_cap_changes_recommendation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = _write_dataset(
        tmp_path,
        [
            {
                "card_id": "cap_card",
                "card_name": "高回饋上限卡",
                "card_status": "active",
                "channels": [
                    {
                        "channel_id": "general",
                        "cashback_type": "cash",
                        "cashback_rate": 0.05,
                        "cashback_description": "國內一般消費5%，上限100元",
                        "max_cashback_per_period": 100,
                        "min_spend": None,
                        "conditions": "國內一般消費5%，上限100元",
                        "valid_start": None,
                        "valid_end": None,
                    }
                ],
            },
            {
                "card_id": "flat_card",
                "card_name": "平穩回饋卡",
                "card_status": "active",
                "channels": [
                    {
                        "channel_id": "general",
                        "cashback_type": "cash",
                        "cashback_rate": 0.015,
                        "cashback_description": "國內一般消費1.5%，回饋無上限",
                        "max_cashback_per_period": None,
                        "min_spend": None,
                        "conditions": "國內一般消費1.5%，回饋無上限",
                        "valid_start": None,
                        "valid_end": None,
                    }
                ],
            },
        ],
    )
    monkeypatch.setenv("CTBC_DATA_DIR", str(data_dir))
    reset_normalized_cache()

    payload = get_recommendation_payload(
        RecommendationRequest(
            merchant_name="測試商城",
            transaction_amount=10000,
            transaction_type="online",
        )
    )

    assert payload["recommendedCard"] == "平穩回饋卡"
    assert payload["estimatedRewardAmount"] == pytest.approx(150.0)


def test_tax_and_utility_uses_highest_real_ctbc_reward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = _write_dataset(
        tmp_path,
        [
            {
                "card_id": "linepay",
                "card_name": "LINE Pay信用卡",
                "card_status": "active",
                "channels": [
                    {
                        "channel_id": "general",
                        "cashback_type": "cash",
                        "cashback_rate": 0.005,
                        "cashback_description": "國內一般消費0.5%",
                        "max_cashback_per_period": None,
                        "min_spend": None,
                        "conditions": "國內一般消費0.5%",
                        "valid_start": None,
                        "valid_end": None,
                    }
                ],
            },
            {
                "card_id": "business",
                "card_name": "中信商旅鈦金卡",
                "card_status": "active",
                "channels": [
                    {
                        "channel_id": "general",
                        "cashback_type": "cash",
                        "cashback_rate": 0.003,
                        "cashback_description": "代扣水電瓦斯電信0.3%",
                        "max_cashback_per_period": None,
                        "min_spend": None,
                        "conditions": "代扣水電瓦斯電信0.3%",
                        "valid_start": None,
                        "valid_end": None,
                    }
                ],
            },
            {
                "card_id": "cht",
                "card_name": "中華電信聯名卡",
                "card_status": "active",
                "channels": [
                    {
                        "channel_id": "general",
                        "cashback_type": "cash",
                        "cashback_rate": 0.02,
                        "cashback_description": "代扣中華電信費2%",
                        "max_cashback_per_period": None,
                        "min_spend": None,
                        "conditions": "代扣中華電信費2%",
                        "valid_start": None,
                        "valid_end": None,
                    }
                ],
            },
        ],
    )
    monkeypatch.setenv("CTBC_DATA_DIR", str(data_dir))
    reset_normalized_cache()

    payload = get_recommendation_payload(
        RecommendationRequest(
            merchant_name="中華電信",
            transaction_amount=1000,
            transaction_type="taxAndUtility",
        )
    )

    assert payload["recommendedCard"] == "中華電信聯名卡"
    assert payload["estimatedRewardAmount"] == pytest.approx(20.0)


def test_min_spend_threshold_blocks_bonus_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = _write_dataset(
        tmp_path,
        [
            {
                "card_id": "threshold_card",
                "card_name": "高門檻回饋卡",
                "card_status": "active",
                "channels": [
                    {
                        "channel_id": "general",
                        "cashback_type": "cash",
                        "cashback_rate": 0.05,
                        "cashback_description": "國內一般消費5%",
                        "max_cashback_per_period": None,
                        "min_spend": 5000,
                        "conditions": "國內一般消費5%",
                        "valid_start": None,
                        "valid_end": None,
                    }
                ],
            },
            {
                "card_id": "base_card",
                "card_name": "低門檻回饋卡",
                "card_status": "active",
                "channels": [
                    {
                        "channel_id": "general",
                        "cashback_type": "cash",
                        "cashback_rate": 0.01,
                        "cashback_description": "國內一般消費1%",
                        "max_cashback_per_period": None,
                        "min_spend": None,
                        "conditions": "國內一般消費1%",
                        "valid_start": None,
                        "valid_end": None,
                    }
                ],
            },
        ],
    )
    monkeypatch.setenv("CTBC_DATA_DIR", str(data_dir))
    reset_normalized_cache()

    payload = get_recommendation_payload(
        RecommendationRequest(
            merchant_name="測試商城",
            transaction_amount=3000,
            transaction_type="online",
        )
    )

    assert payload["recommendedCard"] == "低門檻回饋卡"
    assert payload["estimatedRewardAmount"] == pytest.approx(30.0)


def test_multi_bank_loader_supports_new_repo_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = _write_multi_bank_dataset(
        tmp_path,
        ctbc_cards=[
            {
                "card_id": "ctbc_linepay",
                "bank_id": "ctbc",
                "card_name": "LINE Pay信用卡",
                "card_status": "active",
                "channels": [
                    {
                        "channel_id": "general",
                        "cashback_type": "cash",
                        "cashback_rate": 0.005,
                        "cashback_description": "國內一般消費0.5%",
                        "max_cashback_per_period": None,
                        "min_spend": None,
                        "conditions": "",
                        "valid_start": None,
                        "valid_end": None,
                        "merchants": [],
                    }
                ],
            }
        ],
        fubon_cards=[
            {
                "card_id": "fubon_j_travel",
                "bank_id": "fubon",
                "card_name": "富邦J Travel卡",
                "card_status": "active",
                "channels": [
                    {
                        "channel_id": "travel",
                        "cashback_type": "cash",
                        "cashback_rate": 0.06,
                        "cashback_description": "旅遊相關消費6%",
                        "max_cashback_per_period": 1500,
                        "min_spend": None,
                        "conditions": "限訂房平台或旅行社消費，每月回饋上限 1,500 元",
                        "valid_start": None,
                        "valid_end": None,
                        "merchants": ["Agoda"],
                    },
                    {
                        "channel_id": "general",
                        "cashback_type": "cash",
                        "cashback_rate": 0.01,
                        "cashback_description": "一般消費1%",
                        "max_cashback_per_period": None,
                        "min_spend": None,
                        "conditions": "",
                        "valid_start": None,
                        "valid_end": None,
                        "merchants": [],
                    },
                ],
            }
        ],
    )
    monkeypatch.setenv("CTBC_DATA_DIR", str(data_dir))
    reset_normalized_cache()

    data = get_normalized_data()
    assert data is not None
    assert "LINE Pay信用卡" in data.cards_by_name
    assert "富邦J Travel卡" in data.cards_by_name
    assert data.cards_by_name["富邦J Travel卡"].bank_id == "fubon"
    assert data.merchant_index["agoda"] == "travel"

    payload = get_recommendation_payload(
        RecommendationRequest(
            merchant_name="Agoda",
            transaction_amount=20000,
            transaction_type="online",
            merchant_channel="travel",
            allowed_cards=["LINE Pay信用卡", "富邦J Travel卡"],
        )
    )

    assert payload["recommendedCard"] == "富邦J Travel卡"
    assert payload["estimatedRewardAmount"] == pytest.approx(1200.0)


def test_text_request_prefers_fubon_travel_rule_with_new_channel_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = _write_multi_bank_dataset(
        tmp_path,
        ctbc_cards=[
            {
                "card_id": "ctbc_travel",
                "bank_id": "ctbc",
                "card_name": "中信商旅鈦金卡",
                "card_status": "active",
                "channels": [
                    {
                        "channel_id": "general",
                        "cashback_type": "cash",
                        "cashback_rate": 0.01,
                        "cashback_description": "國內外一般消費1%",
                        "max_cashback_per_period": None,
                        "min_spend": None,
                        "conditions": "",
                        "valid_start": None,
                        "valid_end": None,
                        "merchants": [],
                    },
                    {
                        "channel_id": "overseas_general",
                        "cashback_type": "cash",
                        "cashback_rate": 0.025,
                        "cashback_description": "國外消費2.5%",
                        "max_cashback_per_period": None,
                        "min_spend": None,
                        "conditions": "",
                        "valid_start": None,
                        "valid_end": None,
                        "merchants": [],
                    },
                ],
            }
        ],
        fubon_cards=[
            {
                "card_id": "fubon_j_travel",
                "bank_id": "fubon",
                "card_name": "富邦J Travel卡",
                "card_status": "active",
                "channels": [
                    {
                        "channel_id": "travel",
                        "cashback_type": "cash",
                        "cashback_rate": 0.06,
                        "cashback_description": "旅遊相關消費6%",
                        "max_cashback_per_period": 1500,
                        "min_spend": None,
                        "conditions": "限訂房平台或旅行社消費，每月回饋上限 1,500 元",
                        "valid_start": None,
                        "valid_end": None,
                        "merchants": ["Agoda", "Booking.com", "訂房網站"],
                    },
                    {
                        "channel_id": "general",
                        "cashback_type": "cash",
                        "cashback_rate": 0.01,
                        "cashback_description": "一般消費1%",
                        "max_cashback_per_period": None,
                        "min_spend": None,
                        "conditions": "",
                        "valid_start": None,
                        "valid_end": None,
                        "merchants": [],
                    },
                ],
            }
        ],
    )
    monkeypatch.setenv("CTBC_DATA_DIR", str(data_dir))
    reset_normalized_cache()

    parsed = parse_text_request("我在 Agoda 訂房 20000 元，我有富邦J Travel卡和中信商旅鈦金卡")
    payload = get_recommendation_payload(
        RecommendationRequest(
            merchant_name=parsed.merchant_name,
            transaction_amount=parsed.transaction_amount,
            transaction_type=parsed.transaction_type,
            merchant_channel=parsed.merchant_channel,
            allowed_cards=parsed.allowed_cards,
        )
    )

    assert parsed.merchant_channel == "travel"
    assert parsed.transaction_type == "online"
    assert payload["recommendedCard"] == "富邦J Travel卡"
    assert payload["estimatedRewardAmount"] == pytest.approx(1200.0)


def test_text_request_prefers_fubon_costco_online_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = _write_multi_bank_dataset(
        tmp_path,
        ctbc_cards=[
            {
                "card_id": "ctbc_travel",
                "bank_id": "ctbc",
                "card_name": "中信商旅鈦金卡",
                "card_status": "active",
                "channels": [
                    {
                        "channel_id": "general",
                        "cashback_type": "cash",
                        "cashback_rate": 0.01,
                        "cashback_description": "國內外一般消費1%",
                        "max_cashback_per_period": None,
                        "min_spend": None,
                        "conditions": "",
                        "valid_start": None,
                        "valid_end": None,
                        "merchants": [],
                    }
                ],
            }
        ],
        fubon_cards=[
            {
                "card_id": "fubon_costco",
                "bank_id": "fubon",
                "card_name": "富邦Costco聯名卡",
                "card_status": "active",
                "channels": [
                    {
                        "channel_id": "wholesale",
                        "cashback_type": "cash",
                        "cashback_rate": 0.02,
                        "cashback_description": "Costco實體2%",
                        "max_cashback_per_period": None,
                        "min_spend": None,
                        "conditions": "限 Costco 好市多實體門市消費",
                        "valid_start": None,
                        "valid_end": None,
                        "merchants": ["Costco好市多"],
                    },
                    {
                        "channel_id": "ecommerce",
                        "cashback_type": "cash",
                        "cashback_rate": 0.03,
                        "cashback_description": "Costco線上3%",
                        "max_cashback_per_period": None,
                        "min_spend": None,
                        "conditions": "限 Costco 官方網路商店消費",
                        "valid_start": None,
                        "valid_end": None,
                        "merchants": ["Costco線上商店"],
                    },
                    {
                        "channel_id": "general",
                        "cashback_type": "cash",
                        "cashback_rate": 0.01,
                        "cashback_description": "一般1%",
                        "max_cashback_per_period": None,
                        "min_spend": None,
                        "conditions": "",
                        "valid_start": None,
                        "valid_end": None,
                        "merchants": [],
                    },
                ],
            }
        ],
    )
    monkeypatch.setenv("CTBC_DATA_DIR", str(data_dir))
    reset_normalized_cache()

    parsed = parse_text_request("我在 Costco 線上商店買 10000 元，我有富邦Costco聯名卡和中信商旅鈦金卡")
    payload = get_recommendation_payload(
        RecommendationRequest(
            merchant_name=parsed.merchant_name,
            transaction_amount=parsed.transaction_amount,
            transaction_type=parsed.transaction_type,
            merchant_channel=parsed.merchant_channel,
            allowed_cards=parsed.allowed_cards,
        )
    )

    assert parsed.merchant_channel == "ecommerce"
    assert payload["recommendedCard"] == "富邦Costco聯名卡"
    assert payload["estimatedRewardAmount"] == pytest.approx(300.0)


@pytest.mark.asyncio
async def test_mcp_tool_returns_json_string_and_structured_content() -> None:
    """The registered MCP tool should expose both text JSON and structured data."""

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "credit_card_recom_mcp.server"],
        env={"CTBC_DATA_DIR": str(FIXTURE_DIR)},
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools_result = await session.list_tools()
            registered_tool = next(tool for tool in tools_result.tools if tool.name == TOOL_NAME)
            assert registered_tool.inputSchema["additionalProperties"] is False
            assert registered_tool.inputSchema["required"] == [
                "merchantName",
                "transactionAmount",
                "transactionType",
            ]
            assert registered_tool.inputSchema["properties"]["transactionType"]["enum"] == [
                "online",
                "physicalForeign",
                "taxAndUtility",
            ]
            assert registered_tool.outputSchema["required"] == [
                "recommendedCard",
                "estimatedRewardAmount",
                "reasoning",
            ]

            result = await session.call_tool(
                TOOL_NAME,
                {
                    "merchantName": "Tokyo Donki",
                    "transactionAmount": 10000,
                    "transactionType": "physicalForeign",
                },
            )

    text_blocks = [content for content in result.content if isinstance(content, types.TextContent)]
    assert len(text_blocks) == 1

    payload_from_text = json.loads(text_blocks[0].text)
    assert payload_from_text["recommendedCard"] == "LINE Pay信用卡"
    assert payload_from_text["estimatedRewardAmount"] == pytest.approx(280.0)
    assert result.structuredContent == payload_from_text


@pytest.mark.asyncio
async def test_mcp_text_tool_parses_natural_language() -> None:
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "credit_card_recom_mcp.server"],
        env={"CTBC_DATA_DIR": str(FIXTURE_DIR)},
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(
                TOOL_NAME_TEXT,
                {"userMessage": "我最近在餐廳吃飯要刷卡，消費金額是一千塊，我有LINE Pay信用卡和中信商旅鈦金卡"},
            )

    text_blocks = [content for content in result.content if isinstance(content, types.TextContent)]
    payload_from_text = json.loads(text_blocks[0].text)
    assert payload_from_text["recommendedCard"] == "中信商旅鈦金卡"


@pytest.mark.asyncio
async def test_streamable_http_transport_exposes_same_tool_behavior() -> None:
    """The Streamable HTTP transport should expose the same tool contract."""

    app = create_streamable_http_app()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            health_response = await http_client.get("/health")
            assert health_response.status_code == 200
            assert health_response.json()["status"] == "ok"

            async with streamable_http_client(
                "http://testserver/mcp",
                http_client=http_client,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    result = await session.call_tool(
                        TOOL_NAME,
                        {
                            "merchantName": "Taipei Water",
                            "transactionAmount": 1000,
                            "transactionType": "taxAndUtility",
                        },
                    )

    text_blocks = [content for content in result.content if isinstance(content, types.TextContent)]
    payload_from_text = json.loads(text_blocks[0].text)
    assert payload_from_text["recommendedCard"] == "中信商旅鈦金卡"
    assert payload_from_text["estimatedRewardAmount"] == pytest.approx(3.0)
    assert result.structuredContent == payload_from_text


@pytest.mark.asyncio
async def test_stdio_remote_bridge_forwards_to_streamable_http_server() -> None:
    """The stdio bridge should expose whatever tools the remote session returns."""

    mirrored_tool = types.Tool(
        name=TOOL_NAME,
        description="proxy",
        inputSchema=TOOL_INPUT_SCHEMA,
        outputSchema={
            "type": "object",
            "properties": {"recommendedCard": {"type": "string"}},
            "required": ["recommendedCard"],
        },
    )

    async def fake_session(_: object) -> list[types.Tool]:
        return [mirrored_tool]

    original = remote_bridge.with_remote_session
    remote_bridge.with_remote_session = fake_session  # type: ignore[assignment]
    try:
        tools = await remote_bridge.list_tools()
    finally:
        remote_bridge.with_remote_session = original  # type: ignore[assignment]

    assert [tool.name for tool in tools] == [TOOL_NAME]


@pytest.mark.asyncio
async def test_stdio_remote_bridge_forwards_call_results() -> None:
    """The stdio bridge should return the remote tool call result unchanged."""

    expected = types.CallToolResult(
        content=[types.TextContent(type="text", text='{"recommendedCard":"BusinessTitaniumCard"}')],
        structuredContent={"recommendedCard": "BusinessTitaniumCard"},
    )

    async def fake_session(_: object) -> types.CallToolResult:
        return expected

    original = remote_bridge.with_remote_session
    remote_bridge.with_remote_session = fake_session  # type: ignore[assignment]
    try:
        result = await remote_bridge.call_tool(
            TOOL_NAME,
            {
                "merchantName": "Taipei Water",
                "transactionAmount": 1000,
                "transactionType": "taxAndUtility",
            },
        )
    finally:
        remote_bridge.with_remote_session = original  # type: ignore[assignment]

    assert result == expected
