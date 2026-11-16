"""M5 编排：约束读取 → 成本口径 → 边际 ROI 贪心 → 版本校验 → 落库。

成本口径有三种：
- `observed`：本轮导入的观察花费（尚未月结，默认口径，向后兼容）；
- `settled`：已核销结算成本 = 累计已结算花费 − 累计已核销返点；
- `auto`：六个渠道都有已确认结算时用 `settled`，否则退回 `observed`。

具体用哪个口径、合同编号、结算批次与待确认返点都写进方案 payload，
事后复核能看出这一版是拿哪份证据算的。版本一旦落库就是不可改写的快照；
取证据失败时不写新版本，旧版本原样保留。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from ... import errors, manifest
from ..m2_window.contract import CHANNEL_LABELS, CHANNEL_CODES
from ..m4_attribution import models as m4_models
from ..m4_attribution import service as m4_service
from ..m8_settlement import contract as rebate_contract
from ..m8_settlement import ledger as rebate_ledger
from . import batches, greedy, roi as roi_rules

DEFAULT_CONSTRAINTS = {"min_yuan": 1000, "cap_yuan": 30000}
BASIS_MODES = ("auto", "observed", "settled")


def constraints_path(settings):
    return settings.sample_dir / "budget_plan.json"


def load_constraints(settings) -> dict:
    path = constraints_path(settings)
    if not path.exists():
        payload = {"total_yuan": 60000,
                   "channels": {code: dict(DEFAULT_CONSTRAINTS) for code in CHANNEL_LABELS}}
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
    channels = {}
    for code, label in CHANNEL_LABELS.items():
        item = payload.get("channels", {}).get(code, {})
        channels[code] = {
            "label": label,
            "min_cents": int(Decimal(str(item.get("min_yuan", 1000))) * 100),
            "cap_cents": int(Decimal(str(item.get("cap_yuan", 30000))) * 100),
        }
    return {"total_cents": int(Decimal(str(payload.get("total_yuan", 60000))) * 100),
            "channels": channels,
            "digest": manifest.file_digest(path) if path.exists() else None,
            "path": str(path)}


def evidence(settings, store, run_id: str, model: str) -> dict:
    """收集返点口径所需的全部证据，并给出每个渠道的规划成本。"""
    contract = rebate_contract.load(settings, CHANNEL_CODES)
    aggregates = m4_service.aggregates(store, run_id, model)
    attribution = {row["channel"]: row for row in aggregates["channels"]}
    ledger = rebate_ledger.build(store, contract, CHANNEL_CODES, attribution)
    run = store.get_run(run_id) or {}
    observed = run.get("stats", {}).get("spend", {})
    basis = rebate_ledger.plan_basis(ledger, CHANNEL_CODES, observed)
    return {"contract": contract, "ledger": ledger, "basis": basis,
            "attribution": attribution}


def _choose_basis(requested: str, basis: dict) -> str:
    if requested not in BASIS_MODES:
        raise errors.BadRequest(f"未知成本口径：{requested}",
                                {"allowed": list(BASIS_MODES)})
    if requested == "settled":
        if basis["missing_channels"]:
            raise errors.BudgetEvidenceMissing(
                "缺少已核销结算证据，未生成新版本，旧版本保留",
                {"missing_channels": basis["missing_channels"],
                 "settled_channels": basis["settled_channels"]})
        return "settled"
    if requested == "auto":
        return "settled" if not basis["missing_channels"] and basis["rows"] else "observed"
    return "observed"


def _plan_cost(code: str, chosen: str, basis_rows: dict, observed: dict) -> int:
    """规划分母：选定口径下该渠道的成本；某渠道缺证据时退回观察花费。"""
    row = basis_rows[code]
    if chosen == "settled" and row["basis"] == "settled":
        return int(row["effective_cost_cents"])
    return int(observed.get(code, {}).get("cost_cents", 0))


def reallocate(settings, store, run_id: str, plan_version: int,
               total_cents: int, model: str = "time_decay",
               basis_mode: str = "auto") -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise errors.NotFoundRun(f"运行不存在：{run_id}")
    if total_cents <= 0:
        raise errors.BadRequest("预算必须大于 0")
    if model not in m4_models.MODEL_CODES:
        raise errors.BadRequest(f"未知归因模型：{model}",
                                {"allowed": list(m4_models.MODEL_CODES)})
    batches.check_version(store, run_id, plan_version)

    plan = load_constraints(settings)
    collected = evidence(settings, store, run_id, model)
    chosen = _choose_basis(basis_mode, collected["basis"])
    basis_rows = collected["basis"]["rows"]
    attribution = collected["attribution"]
    spend_before = run["stats"].get("spend", {})

    rois = {}
    for code in CHANNEL_CODES:
        gmv = int(attribution.get(code, {}).get("gmv_cents", 0))
        rois[code] = roi_rules.base_roi(gmv, _plan_cost(code, chosen, basis_rows,
                                                        spend_before))

    result = greedy.allocate(total_cents, plan["channels"], rois,
                             settings.budget_step_cents, settings.marginal_decay)

    prior = store.latest_budget(run_id)
    prior_rows = ({row["channel"]: row for row in prior["payload"].get("rows", [])}
                  if prior else {})

    rows = []
    for code in sorted(plan["channels"]):
        info = plan["channels"][code]
        new_cents = int(result["spend"][code])
        before = basis_rows[code]
        prior_row = prior_rows.get(code, {})
        rows.append({
            "channel": code,
            "label": info["label"],
            "min_cents": info["min_cents"],
            "cap_cents": info["cap_cents"],
            "spend_before_cents": int(spend_before.get(code, {}).get("cost_cents", 0)),
            "spend_new_cents": new_cents,
            "delta_cents": new_cents - int(spend_before.get(code, {})
                                           .get("cost_cents", 0)),
            "budget_before_cents": (int(prior_row["spend_new_cents"])
                                    if prior_row else None),
            "budget_delta_cents": (new_cents - int(prior_row["spend_new_cents"])
                                   if prior_row else None),
            "settled_spend_cents": int(before["settled_spend_cents"]),
            "confirmed_rebate_cents": int(before["rebate_cents"]),
            "effective_cost_cents": int(before["effective_cost_cents"]),
            "pending_rebate_cents": int(before["pending_rebate_cents"]),
            "cost_basis": before["basis"],
            "basis_period": before["basis_period"],
            "tier_index": before["tier_index"],
            "tier_rate_pct": before["tier_rate_pct"],
            "tier_min_spend_cents": before["tier_min_spend_cents"],
            "share_pct": (round(int(attribution.get(code, {}).get("gmv_cents", 0)) * 100.0
                                / int(collected["ledger"]["totals"]["gmv_cents"]), 2)
                          if collected["ledger"]["totals"]["gmv_cents"] else 0.0),
            "gmv_cents": int(attribution.get(code, {}).get("gmv_cents", 0)),
            "base_roi": str(rois[code]),
            "marginal_roi": str(roi_rules.marginal_roi(
                rois[code], int(result["steps"][code]), settings.marginal_decay)),
            "steps": int(result["steps"][code]),
            "share_new_pct": round(new_cents * 100.0 / total_cents, 2),
            "hit": ("上限" if new_cents >= info["cap_cents"] else
                    ("下限" if new_cents <= info["min_cents"] else "—")),
        })

    payload = {
        "run_id": run_id,
        "plan_version": int(plan_version),
        "model": model,
        "total_cents": int(total_cents),
        "balanced": sum(row["spend_new_cents"] for row in rows) == int(total_cents),
        "rows": rows,
        "trace_count": len(result["trace"]),
        "constraints_digest": plan["digest"],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "basis": chosen,
        "basis_requested": basis_mode,
        "basis_reason": ("BD_SETTLED_BASIS" if chosen == "settled"
                         else "BD_OBSERVED_BASIS"),
        "prior_plan_version": prior["plan_version"] if prior else None,
        "evidence": {
            "contract_id": collected["contract"]["contract_id"],
            "contract_digest": collected["contract"]["digest"],
            "settled_spend_cents": int(collected["basis"]["settled_spend_cents"]),
            "confirmed_rebate_cents": int(collected["basis"]["rebate_cents"]),
            "pending_rebate_cents": int(collected["basis"]["pending_rebate_cents"]),
            "settled_channels": int(collected["basis"]["confirmed_channels"]),
            "batches": len(collected["ledger"]["batches"]),
            "allocatable_total_cents": int(total_cents),
        },
    }
    store.save_budget(run_id, plan_version, total_cents, payload, payload["created_at"])
    return payload


def save_guarded(settings, store, run_id: str, plan_version: int, total_cents: int,
                 model: str, basis_mode: str) -> dict:
    """落库时再校验一次版本：并发提交同一版本时后者得到 409，而不是覆盖前者。"""
    try:
        return reallocate(settings, store, run_id, plan_version, total_cents, model,
                          basis_mode)
    except sqlite3.IntegrityError as exc:
        latest = store.next_plan_version(run_id) - 1
        raise errors.StaleVersion(
            f"方案版本 {plan_version} 已存在，当前最大版本 {latest}",
            {"latest_version": latest, "your_version": int(plan_version)}) from exc
