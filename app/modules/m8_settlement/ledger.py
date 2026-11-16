"""返点台账：把「合同档位 / 已结算花费 / 待确认返点 / 归因份额」分开留痕。

台账只做三件事：
1. 按账期排序把已确认结算逐期累加，用合同阶梯算出每期返点；
   跨档时整组回溯，所以每期返点 = 含本期累计返点 − 不含本期累计返点。
2. 待确认行单独列出并给出估算，但**不进入**任何收益口径。
3. 同一渠道同一账期，正式结算行取代待确认行；正式结算重复由导入时拦下。

台账在读取时按全部历史重算，而不是复用导入时的快照：先补录上月、后导入本月，
档位与返点仍然一致，差额恒为 0。
"""
from __future__ import annotations

from ..m2_window.contract import CHANNEL_LABELS

CONFIRMED = "confirmed"
PENDING = "pending"


def _line_payload(row: dict, contract, cumulative_before: int,
                  cumulative_after: int, contract_rebate: int,
                  applied_rebate: int, status: str, superseded: bool = False) -> dict:
    tier = contract.tier_for(cumulative_after)
    reported = row.get("reported_rebate_cents")
    return {
        "batch_id": row["batch_id"],
        "period": row["period"],
        "channel": row["channel"],
        "settled_spend_cents": int(row["settled_spend_cents"]),
        "cumulative_before_cents": int(cumulative_before),
        "cumulative_after_cents": int(cumulative_after),
        "tier_index": tier.index,
        "tier_rate_bp": tier.rate_bp,
        "tier_rate_pct": str(tier.rate_pct),
        "tier_min_spend_cents": tier.min_spend_cents,
        "contract_rebate_cents": int(contract_rebate),
        "reported_rebate_cents": None if reported is None else int(reported),
        "rebate_cents": int(applied_rebate),
        "rebate_status": status,
        "tier_up": tier.index != contract.tier_for(cumulative_before).index,
        "mismatch": (reported is not None and int(reported) != int(contract_rebate)),
        "superseded": bool(superseded),
        "counted": status == CONFIRMED and not superseded,
    }


def channel_ledger(code: str, entries: list[dict], contract) -> dict:
    confirmed = sorted(
        (entry for entry in entries if entry["rebate_status"] == CONFIRMED),
        key=lambda entry: (entry["period"], entry["batch_id"]))
    pending = sorted(
        (entry for entry in entries if entry["rebate_status"] == PENDING),
        key=lambda entry: (entry["period"], entry["batch_id"]))
    confirmed_periods = {entry["period"] for entry in confirmed}

    lines: list[dict] = []
    cumulative = 0
    for entry in confirmed:
        before = cumulative
        added = int(entry["settled_spend_cents"])
        after = before + added
        contract_rebate = contract.rebate_at(after) - contract.rebate_at(before)
        reported = entry.get("reported_rebate_cents")
        applied = contract_rebate if reported is None else int(reported)
        lines.append(_line_payload(entry, contract, before, after, contract_rebate,
                                   applied, CONFIRMED))
        cumulative = after

    superseded_pending = 0
    for entry in pending:
        added = int(entry["settled_spend_cents"])
        superseded = entry["period"] in confirmed_periods
        estimate = contract.rebate_for(cumulative, added)["rebate_cents"]
        reported = entry.get("reported_rebate_cents")
        # 待确认行：有媒体预估值就原样展示（等待核销），没有就按合同估算。
        # 无论哪种，都只是待确认，不进入任何收益口径。
        shown = int(reported) if reported is not None else estimate
        payload = _line_payload(entry, contract, cumulative, cumulative + added,
                                estimate, shown, PENDING, superseded)
        lines.append(payload)
        if superseded:
            superseded_pending += 1

    active_pending = [row for row in lines
                      if row["rebate_status"] == PENDING and not row["superseded"]]
    confirmed_rebate = sum(row["rebate_cents"] for row in lines
                           if row["rebate_status"] == CONFIRMED)
    latest = None
    for row in lines:
        if row["rebate_status"] == CONFIRMED:
            latest = row
    tier = contract.tier_for(cumulative)
    next_tier = next((item for item in contract.tiers
                      if item.min_spend_cents > cumulative), None)
    return {
        "channel": code,
        "label": CHANNEL_LABELS.get(code, code),
        "tiers": [{"index": item.index, "min_spend_cents": item.min_spend_cents,
                   "rate_bp": item.rate_bp, "rate_pct": str(item.rate_pct)}
                  for item in contract.tiers],
        "settled_spend_cents": cumulative,
        "confirmed_rebate_cents": confirmed_rebate,
        "pending_spend_cents": sum(row["settled_spend_cents"] for row in active_pending),
        "pending_rebate_cents": sum(row["rebate_cents"] for row in active_pending),
        "pending_lines": len(active_pending),
        "superseded_pending_lines": superseded_pending,
        "current_tier": {"index": tier.index, "min_spend_cents": tier.min_spend_cents,
                         "rate_bp": tier.rate_bp, "rate_pct": str(tier.rate_pct)},
        "next_tier": (None if next_tier is None else
                      {"index": next_tier.index,
                       "min_spend_cents": next_tier.min_spend_cents,
                       "rate_bp": next_tier.rate_bp, "rate_pct": str(next_tier.rate_pct)}),
        "to_next_tier_cents": (0 if next_tier is None
                               else max(0, next_tier.min_spend_cents - cumulative)),
        "confirmed_periods": sorted(confirmed_periods),
        "latest_confirmed": latest,
        "lines": lines,
    }


def build(store, contract: dict, channels: tuple[str, ...],
          attribution: dict | None = None) -> dict:
    """返回按渠道的台账、批次汇总与合计；归因份额只作展示，不参与返点。"""
    attribution = attribution or {}
    entries: dict[str, list[dict]] = {code: [] for code in channels}
    orphan: list[dict] = []
    for line in store.settlement_lines():
        if line["channel"] in entries:
            entries[line["channel"]].append(line)
        else:
            orphan.append(line)

    rows = []
    for code in channels:
        item = channel_ledger(code, entries[code], contract["channels"][code])
        share = attribution.get(code, {})
        item["gmv_cents"] = int(share.get("gmv_cents", 0))
        item["share_pct"] = share.get("share_pct")
        item["has_confirmed"] = item["latest_confirmed"] is not None
        rows.append(item)

    batch_totals: dict[str, dict] = {}
    for item in rows:
        for line in item["lines"]:
            bucket = batch_totals.setdefault(line["batch_id"], {
                "batch_id": line["batch_id"], "period": line["period"],
                "lines": 0, "confirmed_lines": 0, "pending_lines": 0,
                "settled_spend_cents": 0, "confirmed_rebate_cents": 0,
                "pending_rebate_cents": 0, "mismatches": 0,
                "tier_ups": 0})
            bucket["lines"] += 1
            bucket["settled_spend_cents"] += line["settled_spend_cents"]
            if line["rebate_status"] == CONFIRMED:
                bucket["confirmed_lines"] += 1
                bucket["confirmed_rebate_cents"] += line["rebate_cents"]
            elif not line["superseded"]:
                bucket["pending_lines"] += 1
                bucket["pending_rebate_cents"] += line["rebate_cents"]
            if line["mismatch"]:
                bucket["mismatches"] += 1
            if line["tier_up"] and line["rebate_status"] == CONFIRMED:
                bucket["tier_ups"] += 1

    batches = []
    for batch in store.settlement_batches():
        totals = batch_totals.get(batch["batch_id"], {})
        batches.append({
            **{key: batch[key] for key in
               ("batch_id", "period", "source", "digest", "contract_id",
                "contract_digest", "line_count", "received_at")},
            "confirmed_lines": int(totals.get("confirmed_lines", batch["confirmed_lines"])),
            "pending_lines": int(totals.get("pending_lines", batch["pending_lines"])),
            "settled_spend_cents": int(totals.get("settled_spend_cents",
                                                  batch["settled_spend_cents"])),
            "confirmed_rebate_cents": int(totals.get("confirmed_rebate_cents",
                                                     batch["confirmed_rebate_cents"])),
            "pending_rebate_cents": int(totals.get("pending_rebate_cents",
                                                   batch["pending_rebate_cents"])),
            "mismatches": int(totals.get("mismatches", 0)),
            "tier_ups": int(totals.get("tier_ups", 0)),
        })

    totals = {
        "settled_spend_cents": sum(item["settled_spend_cents"] for item in rows),
        "confirmed_rebate_cents": sum(item["confirmed_rebate_cents"] for item in rows),
        "pending_spend_cents": sum(item["pending_spend_cents"] for item in rows),
        "pending_rebate_cents": sum(item["pending_rebate_cents"] for item in rows),
        "gmv_cents": sum(item["gmv_cents"] for item in rows),
        "confirmed_channels": sum(1 for item in rows if item["has_confirmed"]),
        "pending_lines": sum(item["pending_lines"] for item in rows),
        "orphan_lines": len(orphan),
    }
    totals["effective_cost_cents"] = max(
        0, totals["settled_spend_cents"] - totals["confirmed_rebate_cents"])
    return {
        "contract_id": contract["contract_id"],
        "contract_digest": contract["digest"],
        "contract_configured": contract["configured"],
        "channels": rows,
        "batches": batches,
        "totals": totals,
        "orphan": orphan,
    }


def plan_basis(ledger: dict, channels: tuple[str, ...], observed_spend: dict) -> dict:
    """规划用的成本口径：累计已结算花费扣掉累计已核销返点。

    返点按「整组回溯」计算，逐期增量相加等于按最终档位对全部累计花费算一次，
    所以 cumulative 花费 − cumulative 返点就是整组可核销成本。
    没有已核销证据的渠道退回本轮观察花费；待确认返点只展示，绝不进入 effective_cost。
    """
    rows: dict[str, dict] = {}
    for item in ledger["channels"]:
        code = item["channel"]
        observed = int(observed_spend.get(code, {}).get("cost_cents", 0))
        latest = item["latest_confirmed"]
        if latest is not None:
            cumulative = int(item["settled_spend_cents"])
            rebate = int(item["confirmed_rebate_cents"])
            contract_rebate = sum(int(line["contract_rebate_cents"])
                                  for line in item["lines"]
                                  if line["rebate_status"] == CONFIRMED)
            effective = max(0, cumulative - rebate)
            rows[code] = {
                "basis": "settled",
                "basis_period": latest["period"],
                "settled_spend_cents": cumulative,
                "rebate_cents": rebate,
                "contract_rebate_cents": contract_rebate,
                "pending_rebate_cents": int(item["pending_rebate_cents"]),
                "effective_cost_cents": effective,
                "cumulative_spend_cents": cumulative,
                "observed_spend_cents": observed,
                "tier_index": int(item["current_tier"]["index"]),
                "tier_rate_pct": item["current_tier"]["rate_pct"],
                "tier_rate_bp": int(item["current_tier"]["rate_bp"]),
                "tier_min_spend_cents": int(item["current_tier"]["min_spend_cents"]),
                "periods": len(item["confirmed_periods"]),
            }
        else:
            rows[code] = {
                "basis": "observed",
                "basis_period": None,
                "settled_spend_cents": 0,
                "rebate_cents": 0,
                "contract_rebate_cents": 0,
                "pending_rebate_cents": int(item["pending_rebate_cents"]),
                "effective_cost_cents": observed,
                "cumulative_spend_cents": 0,
                "observed_spend_cents": observed,
                "tier_index": None,
                "tier_rate_pct": None,
                "tier_rate_bp": None,
                "tier_min_spend_cents": None,
                "periods": 0,
            }
    settled_channels = [code for code, row in rows.items() if row["basis"] == "settled"]
    missing = [code for code in channels if code not in rows or rows[code]["basis"] != "settled"]
    return {
        "rows": rows,
        "basis": ("settled" if rows and not missing
                  else ("mixed" if settled_channels else "observed")),
        "settled_channels": settled_channels,
        "missing_channels": missing,
        "settled_spend_cents": sum(row["settled_spend_cents"] for row in rows.values()),
        "rebate_cents": sum(row["rebate_cents"] for row in rows.values()),
        "pending_rebate_cents": sum(row["pending_rebate_cents"]
                                    for row in rows.values()),
        "confirmed_channels": len(settled_channels),
    }
