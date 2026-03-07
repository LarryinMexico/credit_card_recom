"""Tests for the credit card recommendation MCP server."""

from __future__ import annotations

import json
import sys

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from credit_card_recom_mcp.server import (
    TOOL_INPUT_SCHEMA,
    TOOL_NAME,
    create_streamable_http_app,
    get_recommendation_payload,
    validate_recommendation_arguments,
)


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

    assert payload["recommendedCard"] == "BusinessTitaniumCard"
    assert payload["estimatedRewardAmount"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_mcp_tool_returns_json_string_and_structured_content() -> None:
    """The registered MCP tool should expose both text JSON and structured data."""

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "credit_card_recom_mcp.server"],
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
    assert payload_from_text["recommendedCard"] == "LinePayCard"
    assert payload_from_text["estimatedRewardAmount"] == pytest.approx(280.0)
    assert result.structuredContent == payload_from_text


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
    assert payload_from_text["recommendedCard"] == "BusinessTitaniumCard"
    assert payload_from_text["estimatedRewardAmount"] == pytest.approx(3.0)
    assert result.structuredContent == payload_from_text
