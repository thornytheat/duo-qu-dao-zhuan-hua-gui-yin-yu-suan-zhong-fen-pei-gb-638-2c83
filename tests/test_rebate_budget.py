"""M5 返点联动：新版本按已核销证据生成，失败保留旧版，总额不被未结算返点改写。"""
from __future__ import annotations

from app import errors
from app.modules.m5_budgets import service as m5_service
from app.modules.m8_settlement import imports


def _import(settings, store):
    return imports.import_samples(settings, store)


def test_auto_basis_uses_settled_rebates_when_available(settings, store, run_id):
    _import(settings, store)
    payload = m5_service.reallocate(settings, store, run_id, 1, 6_000_000,
                                    "time_decay", "auto")
    assert payload["basis"] == "settled"
    assert payload["basis_reason"] == "BD_SETTLED_BASIS"
    assert payload["evidence"]["confirmed_rebate_cents"] > 0
    assert payload["evidence"]["pending_rebate_cents"] > 0
    # 每个渠道的返点后成本 = 累计已结算 − 已核销返点
    for row in payload["rows"]:
        assert row["cost_basis"] == "settled"
        assert row["effective_cost_cents"] == (row["settled_spend_cents"]
                                               - row["confirmed_rebate_cents"])
        # 待确认返点单列，绝不进成本
        assert row["pending_rebate_cents"] >= 0
    assert sum(row["spend_new_cents"] for row in payload["rows"]) == 6_000_000


def test_settled_basis_changes_roi_versus_observed(settings, store, run_id):
    _import(settings, store)
    settled = m5_service.reallocate(settings, store, run_id, 1, 6_000_000,
                                    "time_decay", "settled")
    observed = m5_service.reallocate(settings, store, run_id, 2, 6_000_000,
                                     "time_decay", "observed")
    settled_roi = {row["channel"]: row["base_roi"] for row in settled["rows"]}
    observed_roi = {row["channel"]: row["base_roi"] for row in observed["rows"]}
    assert settled_roi != observed_roi
    # 结算口径的分母是累计花费，跨多月，和单轮观察花费不可直接比大小；
    # 能断言的是：同一渠道返点后 ROI 高于返点前 ROI（分母变小）。
    by_channel = {row["channel"]: row for row in settled["rows"]}
    for code, row in by_channel.items():
        gmv = row["gmv_cents"]
        raw = gmv / row["settled_spend_cents"]
        assert float(row["base_roi"]) >= raw - 1e-9
    # 有返点的渠道严格变好
    assert any(float(by_channel[code]["base_roi"])
               > by_channel[code]["gmv_cents"] / by_channel[code]["settled_spend_cents"]
               for code in by_channel if by_channel[code]["confirmed_rebate_cents"])


def test_missing_evidence_keeps_old_version(settings, store, run_id):
    first = m5_service.reallocate(settings, store, run_id, 1, 6_000_000,
                                  "time_decay", "observed")
    with_store = store.budgets(run_id)
    try:
        m5_service.reallocate(settings, store, run_id, 2, 6_000_000,
                              "time_decay", "settled")
    except errors.BudgetEvidenceMissing as exc:
        assert exc.code == "E_BUDGET_EVIDENCE_MISSING"
    else:  # pragma: no cover - 没有结算时必然抛错
        raise AssertionError("缺少已核销证据时不应生成新版本")
    after = store.budgets(run_id)
    # 失败不写新版本，旧版原样保留
    assert [item["plan_version"] for item in after] == [1]
    assert after[0]["payload"] == with_store[0]["payload"] == first
    assert store.next_plan_version(run_id) == 2


def test_allocatable_total_is_not_rewritten_by_rebates(settings, store, run_id):
    _import(settings, store)
    payload = m5_service.reallocate(settings, store, run_id, 1, 6_000_000,
                                    "time_decay", "auto")
    assert payload["total_cents"] == 6_000_000
    assert sum(row["spend_new_cents"] for row in payload["rows"]) == 6_000_000
    # 返点只影响 ROI 分母，不把待确认返点当可用预算
    assert payload["evidence"]["allocatable_total_cents"] == 6_000_000


def test_pending_only_channel_stays_observed(settings, store, run_id):
    # 只导入待确认行，没有任何已核销证据
    imports.import_payload(settings, store, {
        "source": "pending-only",
        "lines": [{"period": "2026-11", "channel": "douyin_feed",
                   "settled_spend": "1000.00", "rebate": "30.00",
                   "status": "pending"}],
    })
    with_error = None
    try:
        m5_service.reallocate(settings, store, run_id, 1, 6_000_000,
                              "time_decay", "settled")
    except errors.BudgetEvidenceMissing as exc:
        with_error = exc
    assert with_error is not None
    payload = m5_service.reallocate(settings, store, run_id, 1, 6_000_000,
                                    "time_decay", "auto")
    assert payload["basis"] == "observed"
    assert all(row["cost_basis"] == "observed" for row in payload["rows"])
    assert payload["evidence"]["pending_rebate_cents"] == 3000


def test_prior_plan_version_and_budget_delta_recorded(settings, store, run_id):
    _import(settings, store)
    m5_service.reallocate(settings, store, run_id, 1, 6_000_000, "time_decay", "observed")
    second = m5_service.reallocate(settings, store, run_id, 2, 6_000_000,
                                   "time_decay", "auto")
    assert second["prior_plan_version"] == 1
    for row in second["rows"]:
        assert row["budget_before_cents"] is not None
        assert row["budget_delta_cents"] == (row["spend_new_cents"]
                                             - row["budget_before_cents"])


def test_duplicate_plan_version_is_rejected_not_overwritten(settings, store, run_id):
    first = m5_service.reallocate(settings, store, run_id, 1, 6_000_000,
                                  "time_decay", "observed")
    # 同一版本再提交：版本锁先挡下，已执行方案不被覆盖
    try:
        m5_service.reallocate(settings, store, run_id, 1, 5_000_000,
                              "time_decay", "observed")
    except errors.StaleVersion as exc:
        assert exc.status == 409
    else:  # pragma: no cover
        raise AssertionError("同一版本不应被覆盖")
    budgets = store.budgets(run_id)
    assert len(budgets) == 1
    assert budgets[0]["total_cents"] == 6_000_000
    assert budgets[0]["payload"] == first


def test_store_rejects_same_version_even_without_service_guard(settings, store, run_id):
    import sqlite3

    store.save_budget(run_id, 1, 6_000_000, {"rows": []}, "2026-11-16T00:00:00Z")
    # 落库层的唯一约束是最后一道闸：即使绕过 service 也写不进第二遍
    try:
        store.save_budget(run_id, 1, 1, {"rows": []}, "2026-11-16T00:00:01Z")
    except sqlite3.IntegrityError:
        pass
    else:  # pragma: no cover
        raise AssertionError("同一版本不应被覆盖")
    budgets = store.budgets(run_id)
    assert len(budgets) == 1
    assert budgets[0]["total_cents"] == 6_000_000
