"""CTBC data loader and normalizer for credit card recommendation."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable

DEFAULT_DATA_DIR = "/tmp/CTBC_Data"

TRANSACTION_TYPE_ONLINE = "online"
TRANSACTION_TYPE_PHYSICAL_FOREIGN = "physicalForeign"
TRANSACTION_TYPE_TAX_UTILITY = "taxAndUtility"

BASELINE_RULES_RESOURCE = "baseline_rules.json"


@dataclass(frozen=True, slots=True)
class ConditionalRule:
    channel_id: str
    description: str
    rate: Decimal | None
    source: str


@dataclass(frozen=True, slots=True)
class RewardRule:
    channel_id: str
    rate: Decimal
    description: str
    source: str
    reward_cap_amount: Decimal | None = None
    min_spend_amount: Decimal | None = None
    spend_cap_amount: Decimal | None = None
    requires_registration: bool = False
    valid_start: str | None = None
    valid_end: str | None = None


@dataclass(slots=True)
class NormalizedCard:
    card_id: str
    bank_id: str | None
    card_name: str
    status: str
    base_rates: dict[str, Decimal] = field(default_factory=dict)
    channel_rates: dict[str, Decimal] = field(default_factory=dict)
    base_rules: dict[str, list[RewardRule]] = field(default_factory=dict)
    channel_rules: dict[str, list[RewardRule]] = field(default_factory=dict)
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


@dataclass(frozen=True, slots=True)
class DataStatus:
    source: str
    data_dir: str | None
    card_count: int
    last_updated: str | None


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_baseline_rules() -> dict[str, dict[str, Decimal]]:
    raw_rules = json.loads(
        files("credit_card_recom_mcp").joinpath("data", BASELINE_RULES_RESOURCE).read_text(
            encoding="utf-8"
        )
    )
    return {
        alias: {rule_name: Decimal(str(rate)) for rule_name, rate in rules.items()}
        for alias, rules in raw_rules.items()
    }


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
    """Step 1: Load card data files from disk.

    The loader supports both the legacy CTBC-only dataset layout and the newer
    multi-bank layout where each bank ships a dedicated `*_cards.json`.
    """

    bank_card_files: dict[str, dict[str, Any]] = {}
    for path in sorted(data_dir.glob("*_cards.json")):
        bank_card_files[path.stem] = _load_json(path)

    return {
        "bank_card_files": bank_card_files,
        "card_features": _load_json(data_dir / "card_features.json")
        if (data_dir / "card_features.json").exists()
        else {"cards": {}},
        "microsite_deals": _load_json(data_dir / "microsite_deals.json")
        if (data_dir / "microsite_deals.json").exists()
        else {"cards": {}},
        "channels": _load_json(data_dir / "channels.json")
        if (data_dir / "channels.json").exists()
        else {"channel_categories": {}},
    }


def _is_conditional_rule(rule: dict[str, Any]) -> bool:
    description = rule.get("description") or ""
    merchants = rule.get("merchants") or []
    channel_id = rule.get("channel_id") or "general"
    lowered = description.replace(" ", "")
    if "無上限" in lowered:
        lowered = lowered.replace("無上限", "")
    hard_keywords = [
        "需登錄",
        "每月登錄",
        "加碼",
        "需登錄",
        "滿額",
        "登錄",
        "限量",
        "限綁定",
        "限新戶",
        "限指定",
        "限本卡",
        "額滿",
        "限額",
        "同月",
        "不同品牌",
        "擇一",
        "互斥",
        "指定代碼",
    ]
    if any(keyword in lowered for keyword in hard_keywords):
        return True
    if "指定通路" in lowered and not merchants:
        return True
    if channel_id == "general" and "最高" in lowered and "一般消費" not in lowered:
        return True
    return False


def _iter_channel_rules(channels: Iterable[dict[str, Any]], source: str) -> Iterable[dict[str, Any]]:
    for channel in channels:
        yield {
            "channel_id": channel.get("channel_id") or "general",
            "merchants": channel.get("merchants") or [],
            "rate": channel.get("cashback_rate"),
            "cashback_type": channel.get("cashback_type"),
            "description": channel.get("cashback_description") or channel.get("conditions") or "",
            "max_cashback_per_period": channel.get("max_cashback_per_period"),
            "min_spend": channel.get("min_spend"),
            "valid_start": channel.get("valid_start"),
            "valid_end": channel.get("valid_end"),
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
    if channel_id in {"overseas", "overseas_general"}:
        return TRANSACTION_TYPE_PHYSICAL_FOREIGN
    if channel_id == "general":
        return TRANSACTION_TYPE_ONLINE
    return None


def _merge_rate(existing: Decimal | None, candidate: Decimal) -> Decimal:
    if existing is None:
        return candidate
    return candidate if candidate > existing else existing


def _append_rule(
    container: dict[str, list[RewardRule]],
    key: str,
    reward_rule: RewardRule,
) -> None:
    container.setdefault(key, []).append(reward_rule)


def _parse_amount_pattern(text: str, patterns: list[str]) -> Decimal | None:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return Decimal(match.group(1).replace(",", ""))
    return None


def _parse_reward_cap_amount(
    description: str,
    max_cashback_per_period: Any,
) -> Decimal | None:
    if max_cashback_per_period not in (None, "", 0):
        return Decimal(str(max_cashback_per_period))
    return _parse_amount_pattern(
        description,
        [
            r"上限\s*([0-9,]+(?:\.[0-9]+)?)\s*(?:元|點|A金|好多金)",
            r"每[月期帳單戶]+.*?上限\s*([0-9,]+(?:\.[0-9]+)?)\s*(?:元|點|A金|好多金)",
        ],
    )


def _parse_spend_cap_amount(description: str) -> Decimal | None:
    return _parse_amount_pattern(
        description,
        [
            r"消費金額上限(?:為)?[^0-9]{0,12}(?:NT\$?|新臺幣)?\s*([0-9,]+)\s*元",
            r"可累積.*?上限(?:為)?[^0-9]{0,12}(?:NT\$?|新臺幣)?\s*([0-9,]+)\s*元",
        ],
    )


def _parse_min_spend_amount(min_spend: Any, description: str) -> Decimal | None:
    if min_spend not in (None, "", 0):
        return Decimal(str(min_spend))
    return _parse_amount_pattern(
        description,
        [
            r"新增消費滿\s*(?:NT\$?|新臺幣)?\s*([0-9,]+)\s*元",
            r"帳單(?:需)?達\s*(?:NT\$?|新臺幣)?\s*([0-9,]+)\s*元",
            r"消費滿\s*(?:NT\$?|新臺幣)?\s*([0-9,]+)\s*元",
        ],
    )


def _build_reward_rule(
    *,
    channel_id: str,
    rate_value: Decimal,
    description: str,
    source: str,
    max_cashback_per_period: Any,
    min_spend: Any,
    valid_start: str | None,
    valid_end: str | None,
) -> RewardRule:
    return RewardRule(
        channel_id=channel_id,
        rate=rate_value,
        description=description,
        source=source,
        reward_cap_amount=_parse_reward_cap_amount(description, max_cashback_per_period),
        min_spend_amount=_parse_min_spend_amount(min_spend, description),
        spend_cap_amount=_parse_spend_cap_amount(description),
        requires_registration="登錄" in description.replace(" ", ""),
        valid_start=valid_start,
        valid_end=valid_end,
    )


def normalize_cards(raw_data: dict[str, Any]) -> list[NormalizedCard]:
    """Step 2: Normalize raw bank card data into structured card rules."""

    bank_card_files = raw_data["bank_card_files"]
    feature_cards = raw_data["card_features"].get("cards", {})
    microsite_cards = raw_data["microsite_deals"].get("cards", {})

    normalized_cards: list[NormalizedCard] = []
    for dataset_name, dataset in bank_card_files.items():
        bank_name = (dataset.get("bank") or dataset.get("bank_id") or dataset_name).lower()
        cards = dataset.get("cards", [])
        for card in cards:
            card_id = card.get("card_id", "")
            card_name = card.get("card_name", "")
            status = card.get("card_status") or "unknown"
            bank_id = card.get("bank_id") or bank_name
            normalized = NormalizedCard(
                card_id=card_id,
                bank_id=bank_id,
                card_name=card_name,
                status=status,
                base_rates={},
                channel_rates={},
                base_rules={},
                channel_rules={},
                conditional_rules=[],
                aliases=[],
                source_notes=[],
            )

            feature_channels = feature_cards.get(card_id, {}).get("channels", [])
            microsite_deals = microsite_cards.get(card_id, {}).get("deals", [])

            for rule in _iter_channel_rules(card.get("channels", []), dataset_name):
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
    max_cashback_per_period = rule.get("max_cashback_per_period")
    min_spend = rule.get("min_spend")
    valid_start = rule.get("valid_start")
    valid_end = rule.get("valid_end")

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

    if _is_conditional_rule(rule):
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
    reward_rule = _build_reward_rule(
        channel_id=channel_id,
        rate_value=rate_value,
        description=description,
        source=source,
        max_cashback_per_period=max_cashback_per_period,
        min_spend=min_spend,
        valid_start=valid_start,
        valid_end=valid_end,
    )
    target = _classify_rate_target(channel_id, description)
    if target:
        existing = card.base_rates.get(target)
        card.base_rates[target] = _merge_rate(existing, rate_value)
        _append_rule(card.base_rules, target, reward_rule)
        return

    existing_channel = card.channel_rates.get(channel_id)
    card.channel_rates[channel_id] = _merge_rate(existing_channel, rate_value)
    _append_rule(card.channel_rules, channel_id, reward_rule)


def merge_card_aliases(cards: list[NormalizedCard]) -> None:
    """Step 3: Attach aliases so legacy tool names map to real CTBC cards."""

    for card in cards:
        if "LINE Pay" in card.card_name or "LINE_Pay" in card.card_name:
            card.aliases.append("LinePayCard")
        if "商旅鈦金卡" in card.card_name:
            card.aliases.append("BusinessTitaniumCard")


def apply_baseline_fallbacks(cards: list[NormalizedCard]) -> None:
    """Step 4: Apply baseline rules when data is missing or required by policy."""

    baseline_rules = _load_baseline_rules()
    for card in cards:
        for alias in card.aliases:
            baseline = baseline_rules.get(alias)
            if not baseline:
                continue
            for key, value in baseline.items():
                if key == TRANSACTION_TYPE_TAX_UTILITY and alias == "LinePayCard":
                    card.base_rates[key] = Decimal("0.000")
                    _append_rule(
                        card.base_rules,
                        key,
                        RewardRule(
                            channel_id="general",
                            rate=Decimal("0.000"),
                            description="Baseline fallback",
                            source="baseline_rules",
                        ),
                    )
                elif key == TRANSACTION_TYPE_TAX_UTILITY and alias == "BusinessTitaniumCard":
                    card.base_rates[key] = Decimal("0.003")
                    _append_rule(
                        card.base_rules,
                        key,
                        RewardRule(
                            channel_id="general",
                            rate=Decimal("0.003"),
                            description="Baseline fallback",
                            source="baseline_rules",
                        ),
                    )
                else:
                    existing = card.base_rates.get(key)
                    if existing is None:
                        card.base_rates[key] = value
                        _append_rule(
                            card.base_rules,
                            key,
                            RewardRule(
                                channel_id="general",
                                rate=value,
                                description="Baseline fallback",
                                source="baseline_rules",
                            ),
                        )


def build_merchant_index(raw_data: dict[str, Any]) -> dict[str, str]:
    """Step 5: Build a merchant/synonym index for category inference."""

    index: dict[str, str] = {}
    categories = raw_data.get("channels", {}).get("channel_categories", {})
    for channel_id, info in categories.items():
        for merchant in info.get("merchants", []):
            index[merchant.lower()] = channel_id
        for synonym in info.get("synonyms", []):
            index[synonym.lower()] = channel_id
    for dataset in raw_data.get("bank_card_files", {}).values():
        for card in dataset.get("cards", []):
            for channel in card.get("channels", []):
                channel_id = channel.get("channel_id") or "general"
                for merchant in channel.get("merchants", []):
                    merchant_key = merchant.lower()
                    if merchant_key:
                        index[merchant_key] = channel_id
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
    last_updated_candidates = [
        dataset.get("last_updated")
        for dataset in raw_data.get("bank_card_files", {}).values()
        if dataset.get("last_updated")
    ]
    last_updated = max(last_updated_candidates) if last_updated_candidates else None
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


def is_rule_active(rule: RewardRule) -> bool:
    today = date.today()
    if rule.valid_start:
        start = date.fromisoformat(rule.valid_start)
        if today < start:
            return False
    if rule.valid_end:
        end = date.fromisoformat(rule.valid_end)
        if today > end:
            return False
    return True


def get_data_status() -> DataStatus:
    normalized = get_normalized_data()
    if normalized is None:
        return DataStatus(
            source="mock",
            data_dir=str(get_data_dir()) if get_data_dir() else None,
            card_count=0,
            last_updated=None,
        )
    return DataStatus(
        source="ctbc",
        data_dir=str(get_data_dir()) if get_data_dir() else None,
        card_count=len(normalized.cards),
        last_updated=normalized.last_updated,
    )
