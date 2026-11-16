"""M8 入口：返点合同、结算台账、结算批次导入。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ... import errors
from . import service
from ..m4_attribution import models as m4_models

router = APIRouter(prefix="/api", tags=["m8-settlement"])


class SettlementLine(BaseModel):
    period: str
    channel: str
    settled_spend: str
    rebate: str | None = None
    status: str = "confirmed"


class SettlementImport(BaseModel):
    run_id: str | None = None
    source: str = "json"
    batch_id: str | None = None
    lines: list[SettlementLine] = Field(default_factory=list)


def _model(query: str | None) -> str:
    model = query or "time_decay"
    if model not in m4_models.MODEL_CODES:
        raise errors.BadRequest(f"未知归因模型：{model}",
                                {"allowed": list(m4_models.MODEL_CODES)})
    return model


@router.get("/settlements")
def get_settlements(request: Request) -> dict:
    store = request.app.state.store
    run_id = request.query_params.get("run_id")
    if run_id and store.get_run(run_id) is None:
        raise errors.NotFoundRun(f"运行不存在：{run_id}")
    return service.overview(request.app.state.settings, store, run_id,
                            _model(request.query_params.get("model")))


@router.post("/settlements")
def post_settlement(request: Request, body: SettlementImport) -> dict:
    from . import imports

    store = request.app.state.store
    if body.run_id and store.get_run(body.run_id) is None:
        raise errors.NotFoundRun(f"运行不存在：{body.run_id}")
    payload = {
        "source": body.source,
        "batch_id": body.batch_id,
        "lines": [line.model_dump() for line in body.lines],
    }
    result = imports.import_payload(request.app.state.settings, store, payload)
    return result


@router.post("/settlements/samples")
def post_settlement_samples(request: Request) -> dict:
    from . import imports

    return imports.import_samples(request.app.state.settings,
                                  request.app.state.store)


@router.get("/rebate-contract")
def get_contract(request: Request) -> dict:
    from ..m2_window.contract import CHANNEL_CODES
    from . import contract as contract_rules

    loaded = contract_rules.load(request.app.state.settings, CHANNEL_CODES)
    return {
        "contract_id": loaded["contract_id"],
        "digest": loaded["digest"],
        "configured": loaded["configured"],
        "channels": contract_rules.describe(loaded, CHANNEL_CODES),
    }
