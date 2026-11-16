"""渠道 ROI 与边际 ROI：基础 ROI 来自已归因金额与本渠道成本。

「成本」有两种口径：本轮观察花费（未月结）与已核销结算成本
（累计已结算花费 − 累计已核销返点）。后者才是返点到账后的真实投放成本。
边际 ROI 随加投档数衰减，避免把全部预算压给一个渠道。
"""
from __future__ import annotations

from decimal import Decimal


def base_roi(gmv_cents: int, spend_cents: int) -> Decimal:
    if spend_cents <= 0:
        return Decimal(0)
    return (Decimal(int(gmv_cents)) / Decimal(int(spend_cents))).quantize(Decimal("0.0001"))


def rebate_adjusted_roi(gmv_cents: int, settled_spend_cents: int,
                        rebate_cents: int) -> dict:
    """返点后 ROI：分母是扣掉已核销返点的成本，返点本身仍单列留痕。"""
    settled = int(settled_spend_cents)
    rebate = int(rebate_cents)
    effective = max(0, settled - rebate)
    return {
        "settled_spend_cents": settled,
        "rebate_cents": rebate,
        "effective_cost_cents": effective,
        "roi": base_roi(gmv_cents, effective),
    }


def marginal_roi(roi: Decimal, given_steps: int, decay: Decimal) -> Decimal:
    return (Decimal(roi) / (Decimal(1) + Decimal(decay) * int(given_steps))).quantize(
        Decimal("0.0001"))
