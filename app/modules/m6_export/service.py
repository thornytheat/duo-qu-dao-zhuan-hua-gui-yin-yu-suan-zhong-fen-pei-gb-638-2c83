"""M6 编排：按类型取证据行，拼 CSV / JSON / 清单。"""
from __future__ import annotations

from ... import errors
from ..m4_attribution import models as m4_models
from . import csv_out, json_out, manifest_out


def _run(store, run_id: str | None) -> dict:
    run = store.get_run(run_id) if run_id else store.latest_run()
    if run is None:
        raise errors.NotFoundRun("还没有运行记录")
    return run


def export_csv(store, run_id: str | None, kind: str, model: str,
               settings=None) -> dict:
    if kind not in csv_out.EXPORTERS:
        raise errors.BadRequest(f"未知导出类型：{kind}",
                                {"allowed": sorted(csv_out.EXPORTERS)})
    run = _run(store, run_id)
    if kind == "touch":
        body = csv_out.touchpoints_csv(store.touchpoints(run["run_id"]))
    elif kind == "order":
        body = csv_out.orders_csv(store.orders(run["run_id"]))
    elif kind == "rebate":
        body = csv_out.rebates_csv(rebate_lines(store, settings))
    else:
        if model not in m4_models.MODEL_CODES:
            raise errors.BadRequest(f"未知归因模型：{model}",
                                    {"allowed": list(m4_models.MODEL_CODES)})
        body = csv_out.credits_csv(store.credits(run["run_id"], model))
    stem, _ = csv_out.EXPORTERS[kind]
    return {"filename": f"{run['run_id']}-{stem}.csv", "body": body}


def rebate_lines(store, settings) -> list[dict]:
    """返点台账的逐行证据；设置缺失时用默认样例合同。"""
    if settings is None:
        return []
    from ..m8_settlement import contract as contract_rules
    from ..m8_settlement import ledger as rebate_ledger
    from ..m2_window.contract import CHANNEL_CODES

    contract = contract_rules.load(settings, CHANNEL_CODES)
    ledger = rebate_ledger.build(store, contract, CHANNEL_CODES)
    rows: list[dict] = []
    for channel in ledger["channels"]:
        rows.extend(channel["lines"])
    return rows


def export_json(store, run_id: str | None, settings=None) -> dict:
    run = _run(store, run_id)
    stats = run["stats"]
    model = "time_decay"
    credits = store.credits(run["run_id"], model)
    rebates = rebate_lines(store, settings)
    return json_out.bundle(
        run, stats.get("accounts", {}),
        {code: int(value.get("gmv_cents", 0))
         for code, value in store.model_totals(run["run_id"]).items()},
        stats.get("spend", {}), store.touchpoints(run["run_id"]),
        store.orders(run["run_id"]), credits, rebates=rebates,
        settlement=_settlement_summary(store, settings))


def _settlement_summary(store, settings) -> dict:
    if settings is None:
        return {}
    from ..m8_settlement import service as settlement_service

    view = settlement_service.overview(settings, store, None)
    return {"contract": view["contract"], "totals": view["ledger"]["totals"],
            "basis": view["basis"], "batches": view["ledger"]["batches"],
            "reasons": view["reasons"]}


def manifest(store, run_id: str | None, settings) -> dict:
    run = _run(store, run_id)
    return manifest_out.build(run, settings)
