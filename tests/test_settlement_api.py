"""M8 接口与页面：合同、台账、批次导入幂等、页面可核对。"""
from __future__ import annotations

from fixtures.expected import DIRTY


def test_rebate_contract_endpoint(client):
    payload = client.get("/api/rebate-contract").json()
    assert payload["configured"] is True
    assert payload["contract_id"] == "CT-2026-Q4"
    codes = {row["channel"] for row in payload["channels"]}
    assert "douyin_feed" in codes
    first = payload["channels"][0]["tiers"]
    assert first[0]["min_spend_cents"] == 0
    assert [row["rate_bp"] for row in first] == sorted(row["rate_bp"] for row in first)


def test_settlement_samples_import_is_idempotent(client_dirty):
    first = client_dirty.post("/api/settlements/samples").json()
    assert first["files"] >= 2
    assert first["imported"] == first["files"]
    second = client_dirty.post("/api/settlements/samples").json()
    assert second["imported"] == 0
    assert second["repeated"] == first["files"]
    view = client_dirty.get("/api/settlements").json()
    totals = view["ledger"]["totals"]
    assert totals["settled_spend_cents"] > 0
    assert totals["confirmed_rebate_cents"] > 0
    assert totals["pending_rebate_cents"] > 0
    assert totals["effective_cost_cents"] == (totals["settled_spend_cents"]
                                              - totals["confirmed_rebate_cents"])
    # 每个渠道都有当前档位与逐行留痕
    for channel in view["ledger"]["channels"]:
        assert channel["current_tier"]["rate_bp"] >= 0
        assert channel["lines"]


def test_settlement_json_import_and_repeat(client_dirty):
    body = {
        "source": "unit",
        "lines": [
            {"period": "2026-12", "channel": "douyin_feed",
             "settled_spend": "1000.00", "rebate": "30.00", "status": "confirmed"},
            {"period": "2026-12", "channel": "xhs_seed",
             "settled_spend": "500.00", "rebate": "", "status": "pending"},
        ],
    }
    first = client_dirty.post("/api/settlements", json=body).json()
    assert first["repeat"] is False
    assert first["confirmed_lines"] == 1
    assert first["pending_lines"] == 1
    assert first["accepted"] == 2
    again = client_dirty.post("/api/settlements", json=body).json()
    assert again["repeat"] is True
    assert again["accepted"] == 0
    assert again["reason"] == "ST_BATCH_REPEAT"


def test_settlement_json_conflict_returns_409(client_dirty):
    body = {"source": "unit-a", "lines": [
        {"period": "2026-09", "channel": "douyin_feed",
         "settled_spend": "1200.00", "rebate": "36.00", "status": "confirmed"}]}
    assert client_dirty.post("/api/settlements", json=body).status_code == 200
    clash = {"source": "unit-b", "lines": [
        {"period": "2026-09", "channel": "douyin_feed",
         "settled_spend": "1300.00", "rebate": "39.00", "status": "confirmed"}]}
    response = client_dirty.post("/api/settlements", json=clash)
    assert response.status_code == 409
    assert response.json()["error"] == "E_SETTLEMENT_CONFLICT"


def test_budget_without_evidence_returns_409_and_keeps_plan(client_dirty):
    ok = client_dirty.post("/api/budgets", json={
        "plan_version": 1, "total_yuan": 60000, "cost_basis": "observed"})
    assert ok.status_code == 200
    response = client_dirty.post("/api/budgets", json={
        "plan_version": 2, "total_yuan": 60000, "cost_basis": "settled"})
    assert response.status_code == 409
    assert response.json()["error"] == "E_BUDGET_EVIDENCE_MISSING"
    plans = client_dirty.get("/api/budgets").json()
    assert [item["plan_version"] for item in plans["plans"]] == [1]
    assert plans["next_plan_version"] == 2


def test_budget_responds_with_rebate_evidence(client_dirty):
    client_dirty.post("/api/settlements/samples")
    payload = client_dirty.post("/api/budgets", json={
        "plan_version": 1, "total_yuan": 60000, "cost_basis": "auto"}).json()
    assert payload["basis"] == "settled"
    assert payload["evidence"]["contract_id"] == "CT-2026-Q4"
    assert payload["evidence"]["batches"] >= 2
    for row in payload["rows"]:
        assert "confirmed_rebate_cents" in row
        assert "pending_rebate_cents" in row
        assert row["cost_basis"] == "settled"


def test_settlement_page_shows_contract_tiers_and_ledger(client_dirty):
    client_dirty.post("/api/settlements/samples")
    client_dirty.post("/api/budgets", json={"plan_version": 1, "total_yuan": 60000})
    page = client_dirty.get("/settlements")
    assert page.status_code == 200
    body = page.text
    assert 'data-page="settlements"' in body
    assert "CT-2026-Q4" in body
    assert "累计已结算" in body
    assert "待确认返点" in body
    budgets = client_dirty.get("/budgets").text
    assert "返点后成本" in budgets
    assert "成本口径" in budgets


def test_pages_render_without_settlement_data(client_dirty):
    assert client_dirty.get("/settlements").status_code == 200
    assert client_dirty.get("/budgets").status_code == 200


def test_settlement_view_reconciles_against_attribution(client_dirty):
    client_dirty.post("/api/settlements/samples")
    view = client_dirty.get("/api/settlements").json()
    assert view["ledger"]["totals"]["gmv_cents"] == DIRTY["gmv_cents"]
    assert view["basis"]["missing_channels"] == []
