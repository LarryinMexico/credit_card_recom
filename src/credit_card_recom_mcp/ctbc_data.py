"""CTBC data loader and normalizer for credit card recommendation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

DEFAULT_DATA_DIR = "/tmp/CTBC_Data"

TRANSACTION_TYPE_ONLINE = "online"
TRANSACTION_TYPE_PHYSICAL_FOREIGN = "physicalForeign"
TRANSACTION_TYPE_TAX_UTILITY = "taxAndUtility"

BASELINE_RULES: dict[str, dict[str, Decimal]] = {
    "LinePayCard": {
        TRANSACTION_TYPE_ONLINE: Decimal("0.005"),
        TRANSACTION_TYPE_PHYSICAL_FOREIGN: Decimal("0.028"),
        TRANSACTION_TYPE_TAX_UTILITY: Decimal("0.000"),
    },
    "BusinessTitaniumCard": {
        TRANSACTION_TYPE_ONLINE: Decimal("0.010"),
        TRANSACTION_TYPE_PHYSICAL_FOREIGN: Decimal("0.025"),
        TRANSACTION_TYPE_TAX_UTILITY: Decimal("0.003"),
    },
}


@dataclass(frozen=True, slots=True)
class ConditionalRule:
    channel_id: str
    description: str
    rate: Decimal | None
    source: str


@dataclass(slots=True)
class NormalizedCard:
    card_id: str
    card_name: str
    status: str
    base_rates: dict[str, Decimal] = field(default_factory=dict)
    channel_rates: dict[str, Decimal] = field(default_factory=dict)
    conditional_rules: list[ConditionalRule] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    source_notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class NormalizedData:
    cards: list[NormalizedCard]
    cards_by_name: dict[str, NormalizedCard]
    cards_by_alias: dict[str, NormalizedCard]
    merchant_index: dict[str, str]
    last_updated: str | None


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def get_data_dir() -> Path | None:
    env_dir = os.getenv("CTBC_DATA_DIR", "").strip()
    if env_dir:
        path = Path(env_dir)
        if path.exists():
            return path
    project_root = Path(__file__).resolve().parents[3]
    repo_data_dir = project_root / "CTBC_Data"
    if repo_data_dir.exists():
        return repo_data_dir
    default_path = Path(DEFAULT_DATA_DIR)
    return default_path if default_path.exists() else None


def load_raw_data(data_dir: Path) -> dict[str, Any]:
    """Step 1: Load raw CTBC data files from disk."""

    return {
        "ctbc_cards": _load_json(data_dir / "ctbc_cards.json"),
        "card_features": _load_json(data_dir / "card_features.json"),
        "microsite_deals": _load_json(data_dir / "microsite_deals.json"),
        "channels": _load_json(data_dir / "channels.json"),
    }


def _is_conditional_text(text: str) -> bool:
    lowered = text.replace(" ", "")
    keywords = [
        "最高",
        "加碼",
        "需登錄",
        "限",
        "滿額",
        "活動",
        "指定",
        "期間",
        "之前",
        "前",
        "登錄",
        "月",
        "日",
        "限量",
        "回饋無上限",
    ]
    return any(keyword in lowered for keyword in keywords)


def _iter_channel_rules(channels: Iterable[dict[str, Any]], source: str) -> Iterable[dict[str, Any]]:
    for channel in channels:
        yield {
            "channel_id": channel.get("channel_id") or "general",
            "rate": channel.get("cashback_rate"),
            "cashback_type": channel.get("cashback_type"),
            "description": channel.get("cashback_description") or channel.get("conditions") or "",
            "source": source,
        }


def _classify_rate_target(channel_id: str, description: str) -> str | None:
    text = description.replace(" ", "")
    if any(token in text for token in ("代扣", "水電", "瓦斯", "電信", "稅", "學雜費", "公用")):
        return TRANSACTION_TYPE_TAX_UTILITY
    if "國外實體" in text or ("國外" in text and "實體" in text):
        return TRANSACTION_TYPE_PHYSICAL_FOREIGN
    if "國外" in text and "實體" not in text:
        return TRANSACTION_TYPE_PHYSICAL_FOREIGN
    if "國內一般" in text or "一般消費" in text or "國內" in text:
        return TRANSACTION_TYPE_ONLINE
    if channel_id == "overseas":
        return TRANSACTION_TYPE_PHYSICAL_FOREIGN
    if channel_id == "general":
        return TRANSACTION_TYPE_ONLINE
    return None


def _merge_rate(existing: Decimal | None, candidate: Decimal) -> Decimal:
    if existing is None:
        return candidate
    return candidate if candidate > existing else existing


def normalize_cards(raw_data: dict[str, Any]) -> list[NormalizedCard]:
    """Step 2: Normalize raw CTBC data into structured card rules."""

    ctbc_cards = raw_data["ctbc_cards"]["cards"]
    feature_cards = raw_data["card_features"].get("cards", {})
    microsite_cards = raw_data["microsite_deals"].get("cards", {})

    normalized_cards: list[NormalizedCard] = []
    for card in ctbc_cards:
        card_id = card.get("card_id", "")
        card_name = card.get("card_name", "")
        status = card.get("card_status") or "unknown"
        normalized = NormalizedCard(
            card_id=card_id,
            card_name=card_name,
            status=status,
            base_rates={},
            channel_rates={},
            conditional_rules=[],
            aliases=[],
            source_notes=[],
        )

        feature_channels = feature_cards.get(card_id, {}).get("channels", [])
        microsite_deals = microsite_cards.get(card_id, {}).get("deals", [])

        for rule in _iter_channel_rules(card.get("channels", []), "ctbc_cards"):
            _apply_rule(normalized, rule)

        for rule in _iter_channel_rules(feature_channels, "card_features"):
            _apply_rule(normalized, rule)

        for deal in microsite_deals:
            description = deal.get("benefit") or ""
            channel_id = deal.get("channel_id") or "general"
            rate = deal.get("cashback_rate")
            conditional = ConditionalRule(
                channel_id=channel_id,
                description=description,
                rate=Decimal(str(rate)) if rate is not None else None,
                source="microsite_deals",
            )
            normalized.conditional_rules.append(conditional)

        normalized_cards.append(normalized)

    return normalized_cards


def _apply_rule(card: NormalizedCard, rule: dict[str, Any]) -> None:
    rate = rule.get("rate")
    cashback_type = rule.get("cashback_type")
    description = rule.get("description") or ""
    channel_id = rule.get("channel_id") or "general"
    source = rule.get("source", "unknown")

    if cashback_type and cashback_type != "cash":
        card.conditional_rules.append(
            ConditionalRule(
                channel_id=channel_id,
                description=description,
                rate=Decimal(str(rate)) if rate is not None else None,
                source=source,
            )
        )
        return

    if rate is None:
        card.conditional_rules.append(
            ConditionalRule(
                channel_id=channel_id,
                description=description,
                rate=None,
                source=source,
            )
        )
        return

    if _is_conditional_text(description):
        card.conditional_rules.append(
            ConditionalRule(
                channel_id=channel_id,
                description=description,
                rate=Decimal(str(rate)),
                source=source,
            )
        )
        return

    rate_value = Decimal(str(rate))
    target = _classify_rate_target(channel_id, description)
    if target:
        existing = card.base_rates.get(target)
        card.base_rates[target] = _merge_rate(existing, rate_value)
        return

    existing_channel = card.channel_rates.get(channel_id)
    card.channel_rates[channel_id] = _merge_rate(existing_channel, rate_value)


def merge_card_aliases(cards: list[NormalizedCard]) -> None:
    """Step 3: Attach aliases so legacy tool names map to real CTBC cards."""

    for card in cards:
        if "LINE Pay" in card.card_name or "LINE_Pay" in card.card_name:
            card.aliases.append("LinePayCard")
        if "商旅鈦金卡" in card.card_name:
            card.aliases.append("BusinessTitaniumCard")


def apply_baseline_fallbacks(cards: list[NormalizedCard]) -> None:
    """Step 4: Apply baseline rules when data is missing or required by policy."""

    for card in cards:
        for alias in card.aliases:
            baseline = BASELINE_RULES.get(alias)
            if not baseline:
                continue
            for key, value in baseline.items():
                if key == TRANSACTION_TYPE_TAX_UTILITY and alias == "LinePayCard":
                    card.base_rates[key] = Decimal("0.000")
                elif key == TRANSACTION_TYPE_TAX_UTILITY and alias == "BusinessTitaniumCard":
                    card.base_rates[key] = Decimal("0.003")
                else:
                    existing = card.base_rates.get(key)
                    if existing is None:
                        card.base_rates[key] = value


def build_merchant_index(raw_data: dict[str, Any]) -> dict[str, str]:
    """Step 5: Build a merchant/synonym index for category inference."""

    index: dict[str, str] = {}
    categories = raw_data.get("channels", {}).get("channel_categories", {})
    for channel_id, info in categories.items():
        for merchant in info.get("merchants", []):
            index[merchant.lower()] = channel_id
        for synonym in info.get("synonyms", []):
            index[synonym.lower()] = channel_id
    return index


def build_normalized_schema(raw_data: dict[str, Any]) -> NormalizedData:
    cards = normalize_cards(raw_data)
    merge_card_aliases(cards)
    apply_baseline_fallbacks(cards)
    merchant_index = build_merchant_index(raw_data)
    cards_by_name = {card.card_name: card for card in cards}
    cards_by_alias = {
        alias: card
        for card in cards
        for alias in card.aliases
    }
    last_updated = raw_data.get("ctbc_cards", {}).get("last_updated")
    return NormalizedData(
        cards=cards,
        cards_by_name=cards_by_name,
        cards_by_alias=cards_by_alias,
        merchant_index=merchant_index,
        last_updated=last_updated,
    )


_NORMALIZED_CACHE: NormalizedData | None = None


def get_normalized_data() -> NormalizedData | None:
    """Load and cache CTBC data if available on disk."""

    global _NORMALIZED_CACHE
    if _NORMALIZED_CACHE is not None:
        return _NORMALIZED_CACHE

    data_dir = get_data_dir()
    if data_dir is None:
        return None

    raw_data = load_raw_data(data_dir)
    _NORMALIZED_CACHE = build_normalized_schema(raw_data)
    return _NORMALIZED_CACHE


def reset_normalized_cache() -> None:
    global _NORMALIZED_CACHE
    _NORMALIZED_CACHE = None
