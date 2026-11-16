"""M8 编排：合同 → 台账 → 规划口径。页面与预算都只读这里。"""
from __future__ import annotations

from ... import reasons
from ..m2_window.contract import CHANNEL_CODES
from ..m4_attribution import models as m4_models
from ..m4_attribution import service as m4_service
from . import contract as contract_rules
from . import imports, ledger


def contract(settings) -> dict:
    return contract_rules.load(settings, CHANNEL_CODES)


def overview(settings, store, run_id: str | None, model: str = "time_decay") -> dict:
    """一次结算视图：合同阶梯、逐渠道台账、批次、规划口径与原因码。"""
    contract_data = contract_rules.load(settings, CHANNEL_CODES)
    run = store.get_run(run_id) if run_id else store.latest_run()
    observed = (run or {}).get("stats", {}).get("spend", {})
    attribution: dict[str, dict] = {}
    if run is not None:
        try:
            aggregates = m4_service.aggregates(store, run["run_id"], model)
        except ValueError:
            aggregates = m4_service.aggregates(store, run["run_id"], "time_decay")
        attribution = {row["channel"]: row for row in aggregates["channels"]}
    ledger_data = ledger.build(store, contract_data, CHANNEL_CODES, attribution)
    basis = ledger.plan_basis(ledger_data, CHANNEL_CODES, observed)
    codes = _codes(contract_data, ledger_data, basis)
    return {
        "run_id": (run or {}).get("run_id"),
        "model": model,
        "model_label": m4_models.label(model),
        "contract": {
            "contract_id": contract_data["contract_id"],
            "digest": contract_data["digest"],
            "configured": contract_data["configured"],
            "path": contract_data["path"],
            "channels": contract_rules.describe(contract_data, CHANNEL_CODES),
        },
        "ledger": ledger_data,
        "basis": basis,
        "reasons": [{"code": code, "label": reasons.label(code),
                     "level": reasons.level(code)} for code in codes],
    }


def _codes(contract_data: dict, ledger_data: dict, basis: dict) -> list[str]:
    found: list[str] = []
    if not contract_data["configured"]:
        found.append("ST_CONTRACT_MISSING")
    if ledger_data["totals"]["pending_lines"]:
        found.append("ST_PENDING_EXCLUDED")
    if any(item["superseded_pending_lines"] for item in ledger_data["channels"]):
        found.append("ST_PENDING_SUPERSEDED")
    if any(line["tier_up"] for item in ledger_data["channels"]
           for line in item["lines"] if line["rebate_status"] == "confirmed"):
        found.append("ST_TIER_UP")
    if any(line["mismatch"] for item in ledger_data["channels"] for line in item["lines"]):
        found.append("ST_REBATE_MISMATCH")
    if basis["missing_channels"]:
        found.append("BD_SETTLEMENT_MISSING" if not basis["settled_channels"]
                     else "BD_OBSERVED_BASIS")
    else:
        found.append("BD_SETTLED_BASIS")
    return found


def import_dataset(settings, store, dataset: str | None, path: str | None) -> dict:
    if path:
        from pathlib import Path

        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = imports.directory(settings) / path
        if not candidate.exists():
            raise ValueError(f"结算文件不存在：{candidate}")
        return imports.import_file(settings, store, candidate)
    return imports.import_samples(settings, store)
