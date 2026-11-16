"""返点合同：渠道累计花费阶梯、整组回溯费率、整数分返点计算。

阶梯是「累计花费」阶梯：某个渠道的累计已结算花费跨过门槛后，**整组**投放
按新费率回溯计算返点，而不是只对超出部分加价。因此返点函数必须可增量拆分：

    rebate_at(cumulative) = floor(cumulative × rate(cumulative) / 10000)
    本期返点 = rebate_at(累计含本期) − rebate_at(累计不含本期)

这样逐期累加等于按最终档位对全部累计花费算一次，差额恒为 0。
费率用基点（bp，1bp = 0.01%）存整数，避免浮点。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from ... import errors
from ...manifest import file_digest

BP_SCALE = 10_000


@dataclass(frozen=True)
class Tier:
    index: int
    min_spend_cents: int
    rate_bp: int

    @property
    def rate_pct(self) -> Decimal:
        return (Decimal(self.rate_bp) / Decimal(100)).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class ChannelContract:
    channel: str
    tiers: tuple[Tier, ...]

    def tier_for(self, cumulative_cents: int) -> Tier:
        """累计花费落在哪一档：取最后一个门槛 ≤ 累计额的档。"""
        chosen = self.tiers[0]
        for tier in self.tiers:
            if cumulative_cents >= tier.min_spend_cents:
                chosen = tier
            else:
                break
        return chosen

    def rebate_at(self, cumulative_cents: int) -> int:
        total = int(cumulative_cents)
        if total <= 0:
            return 0
        tier = self.tier_for(total)
        return total * tier.rate_bp // BP_SCALE

    def rebate_for(self, cumulative_before: int, added_cents: int) -> dict:
        """跨档时整组回溯：本期返点 = 含本期累计返点 − 不含本期累计返点。"""
        after = int(cumulative_before) + int(added_cents)
        before_tier = self.tier_for(int(cumulative_before))
        after_tier = self.tier_for(after)
        rebate = self.rebate_at(after) - self.rebate_at(int(cumulative_before))
        return {
            "rebate_cents": rebate,
            "cumulative_before_cents": int(cumulative_before),
            "cumulative_after_cents": after,
            "tier": after_tier,
            "previous_tier": before_tier,
            "tier_up": after_tier.index != before_tier.index and after > int(cumulative_before),
        }


def contract_path(settings) -> Path:
    return settings.sample_dir / "rebate_contract.json"


def _parse_tiers(channel: str, raw: dict) -> ChannelContract:
    rows = raw.get("tiers") if isinstance(raw, dict) else None
    if not isinstance(rows, list) or not rows:
        raise errors.BadRequest(f"渠道 {channel} 缺少返点阶梯", {"channel": channel})
    tiers: list[Tier] = []
    seen: set[int] = set()
    for index, row in enumerate(rows):
        try:
            min_cents = int(Decimal(str(row["min_spend_yuan"])) * 100)
            rate_bp = int(Decimal(str(row["rate_pct"])) * 100)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise errors.BadRequest(
                f"渠道 {channel} 的第 {index + 1} 档门槛或费率无法解析",
                {"channel": channel, "tier": index + 1})
        if min_cents < 0 or rate_bp < 0 or rate_bp > BP_SCALE:
            raise errors.BadRequest(
                f"渠道 {channel} 的第 {index + 1} 档门槛或费率越界",
                {"channel": channel, "tier": index + 1,
                 "min_spend_cents": min_cents, "rate_bp": rate_bp})
        if min_cents in seen:
            raise errors.BadRequest(
                f"渠道 {channel} 的门槛 {min_cents} 分重复",
                {"channel": channel, "min_spend_cents": min_cents})
        seen.add(min_cents)
        tiers.append(Tier(index=index, min_spend_cents=min_cents, rate_bp=rate_bp))
    tiers.sort(key=lambda tier: tier.min_spend_cents)
    if tiers[0].min_spend_cents != 0:
        raise errors.BadRequest(f"渠道 {channel} 的第一档门槛必须为 0",
                                {"channel": channel})
    for left, right in zip(tiers, tiers[1:]):
        if right.rate_bp < left.rate_bp:
            raise errors.BadRequest(
                f"渠道 {channel} 的费率必须随累计花费单调不减",
                {"channel": channel, "previous_rate_bp": left.rate_bp,
                 "rate_bp": right.rate_bp})
    # 档位序号按门槛重排，页面上的「第 N 档」与实际累计顺序一致
    tiers = [Tier(index=position, min_spend_cents=tier.min_spend_cents,
                  rate_bp=tier.rate_bp) for position, tier in enumerate(tiers)]
    return ChannelContract(channel=channel, tiers=tuple(tiers))


def load(settings, channels: tuple[str, ...]) -> dict:
    """读取返点合同；文件缺失时给零费率单档，界面标注未配置。"""
    path = contract_path(settings)
    digest = None
    raw: dict = {}
    if path.exists():
        digest = file_digest(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise errors.BadRequest(f"返点合同无法解析：{path.name}", {"error": str(exc)})
    contract_id = str(raw.get("contract_id") or "未配置")
    configured = isinstance(raw.get("channels"), dict)
    result: dict[str, ChannelContract] = {}
    for code in channels:
        item = (raw.get("channels") or {}).get(code) if configured else None
        if item is None:
            result[code] = ChannelContract(
                channel=code, tiers=(Tier(index=0, min_spend_cents=0, rate_bp=0),))
        else:
            result[code] = _parse_tiers(code, item)
    return {
        "contract_id": contract_id,
        "digest": digest,
        "path": str(path),
        "configured": configured,
        "channels": result,
    }


def describe(contract: dict, channels: tuple[str, ...]) -> list[dict]:
    rows = []
    for code in channels:
        item = contract["channels"][code]
        rows.append({
            "channel": code,
            "tiers": [{"index": tier.index,
                       "min_spend_cents": tier.min_spend_cents,
                       "rate_bp": tier.rate_bp,
                       "rate_pct": str(tier.rate_pct)} for tier in item.tiers],
        })
    return rows
