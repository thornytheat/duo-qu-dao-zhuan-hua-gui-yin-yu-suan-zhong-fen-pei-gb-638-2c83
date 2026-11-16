"""M8 返点结算：合同阶梯、整组回溯、批次幂等、待确认不进入收益口径。"""
from __future__ import annotations

import pytest

from app import errors
from app.modules.m8_settlement import contract as contract_rules
from app.modules.m8_settlement import imports, ledger
from app.modules.m8_settlement.contract import ChannelContract, Tier

CHANNELS = ("douyin_feed", "xhs_seed", "wechat_moments", "baidu_search",
            "tmall_ads", "private_group")


def _contract() -> ChannelContract:
    return ChannelContract(channel="c", tiers=(
        Tier(index=0, min_spend_cents=0, rate_bp=0),
        Tier(index=1, min_spend_cents=100_000, rate_bp=300),
        Tier(index=2, min_spend_cents=300_000, rate_bp=600),
    ))


def test_tier_crossing_is_retroactive_over_whole_group():
    contract = _contract()
    # 累计 90,000 分不在 3% 档；加到 110,000 分后整组按 3% 计，不是只对超出部分
    result = contract.rebate_for(90_000, 20_000)
    assert result["tier"].index == 1
    assert result["rebate_cents"] == 110_000 * 300 // 10_000 == 3300
    assert result["tier_up"] is True


def test_incremental_rebates_sum_to_one_shot_on_cumulative():
    contract = _contract()
    # 逐期增量相加 = 按最终档位对全部累计算一次，差额恒为 0
    periods = [40_000, 70_000, 120_000, 90_000, 200_000]
    cumulative = 0
    total = 0
    for period in periods:
        total += contract.rebate_for(cumulative, period)["rebate_cents"]
        cumulative += period
    assert total == contract.rebate_at(cumulative) == 520_000 * 600 // 10_000


def test_rate_must_be_monotonic_and_start_at_zero(tmp_path):
    path = tmp_path / "rebate_contract.json"
    path.write_text(
        '{"contract_id":"X","channels":{"douyin_feed":{"tiers":['
        '{"min_spend_yuan":100,"rate_pct":3}]}}}', encoding="utf-8")
    settings = type("S", (), {"sample_dir": tmp_path})()
    with pytest.raises(errors.BadRequest):
        contract_rules.load(settings, ("douyin_feed",))
    path.write_text(
        '{"contract_id":"X","channels":{"douyin_feed":{"tiers":['
        '{"min_spend_yuan":0,"rate_pct":5},'
        '{"min_spend_yuan":100,"rate_pct":3}]}}}', encoding="utf-8")
    with pytest.raises(errors.BadRequest):
        contract_rules.load(settings, ("douyin_feed",))


def test_missing_contract_falls_back_to_zero_rate(tmp_path):
    settings = type("S", (), {"sample_dir": tmp_path})()
    loaded = contract_rules.load(settings, ("douyin_feed",))
    assert loaded["configured"] is False
    assert loaded["channels"]["douyin_feed"].rebate_at(10_000_000) == 0


def _line(period: str, channel: str, spend: int, rebate: int, status: str,
          batch_id: str = "st-a") -> dict:
    return {"batch_id": batch_id, "period": period, "channel": channel,
            "settled_spend_cents": spend, "rebate_cents": rebate,
            "reported_rebate_cents": rebate, "rebate_status": status}


def test_pending_rebate_never_enters_effective_cost():
    entries = [
        _line("2026-09", "c", 90_000, 0, "confirmed"),
        _line("2026-10", "c", 120_000, 6300, "confirmed"),
        _line("2026-11", "c", 500_000, 30_000, "pending", batch_id="st-b"),
    ]
    item = ledger.channel_ledger("c", entries, _contract())
    assert item["settled_spend_cents"] == 210_000
    assert item["confirmed_rebate_cents"] == 210_000 * 300 // 10_000
    # 待确认只出现在展示字段，规划口径不扣它
    assert item["pending_rebate_cents"] == 30_000
    basis = ledger.plan_basis(
        {"channels": [item]}, ("c",), {"c": {"cost_cents": 1}})
    assert basis["rows"]["c"]["effective_cost_cents"] == 210_000 - 6300
    assert basis["rows"]["c"]["pending_rebate_cents"] == 30_000


def test_confirmed_line_supersedes_pending_of_same_period():
    entries = [
        _line("2026-10", "c", 120_000, 3600, "confirmed", batch_id="st-a"),
        _line("2026-10", "c", 120_000, 3600, "pending", batch_id="st-b"),
    ]
    item = ledger.channel_ledger("c", entries, _contract())
    assert item["pending_lines"] == 0
    assert item["superseded_pending_lines"] == 1
    assert item["pending_rebate_cents"] == 0
    assert item["confirmed_rebate_cents"] == 3600


def test_contract_rebate_mismatch_is_flagged():
    entries = [_line("2026-10", "c", 120_000, 9999, "confirmed")]
    item = ledger.channel_ledger("c", entries, _contract())
    line = item["lines"][0]
    assert line["mismatch"] is True
    assert line["contract_rebate_cents"] == 3600
    assert line["rebate_cents"] == 9999   # 以媒体正式结算为准，但留痕差异


def _settings(tmp_path):
    return type("S", (), {"sample_dir": tmp_path})()


def _write(path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def _seed_contract(tmp_path):
    _write(tmp_path / "rebate_contract.json",
           '{"contract_id":"CT-X","channels":{'
           '"douyin_feed":{"tiers":[{"min_spend_yuan":0,"rate_pct":0},'
           '{"min_spend_yuan":1000,"rate_pct":3}]},'
           '"xhs_seed":{"tiers":[{"min_spend_yuan":0,"rate_pct":0}]},'
           '"wechat_moments":{"tiers":[{"min_spend_yuan":0,"rate_pct":0}]},'
           '"baidu_search":{"tiers":[{"min_spend_yuan":0,"rate_pct":0}]},'
           '"tmall_ads":{"tiers":[{"min_spend_yuan":0,"rate_pct":0}]},'
           '"private_group":{"tiers":[{"min_spend_yuan":0,"rate_pct":0}]}}}')


def test_same_batch_retry_does_not_double_count(tmp_path, store):
    _seed_contract(tmp_path)
    path = tmp_path / "settlement"
    path.mkdir()
    _write(path / "2026-09.csv",
           "period,channel,settled_spend,rebate,status\n"
           "2026-09,douyin_feed,1200.00,36.00,confirmed\n")
    settings = _settings(tmp_path)
    first = imports.import_file(settings, store, path / "2026-09.csv")
    second = imports.import_file(settings, store, path / "2026-09.csv")
    assert first["repeat"] is False
    assert first["accepted"] == 1
    assert second["repeat"] is True
    assert second["reason"] == "ST_BATCH_REPEAT"
    assert second["accepted"] == 0
    assert len(store.settlement_lines()) == 1
    counts = store.settlement_counts()
    assert counts["batches"] == 1
    assert counts["confirmed"]["rebate_cents"] == 3600


def test_conflicting_confirmed_period_is_rejected_atomically(tmp_path, store):
    _seed_contract(tmp_path)
    path = tmp_path / "settlement"
    path.mkdir()
    _write(path / "a.csv",
           "period,channel,settled_spend,rebate,status\n"
           "2026-09,douyin_feed,1200.00,36.00,confirmed\n")
    _write(path / "b.csv",
           "period,channel,settled_spend,rebate,status\n"
           "2026-09,douyin_feed,1300.00,39.00,confirmed\n")
    settings = _settings(tmp_path)
    imports.import_file(settings, store, path / "a.csv")
    with pytest.raises(errors.SettlementConflict):
        imports.import_file(settings, store, path / "b.csv")
    # 整批拒绝，旧台账原样保留
    assert store.settlement_counts()["batches"] == 1
    assert len(store.settlement_lines()) == 1


def test_invalid_line_rejects_whole_batch(tmp_path, store):
    _seed_contract(tmp_path)
    path = tmp_path / "settlement"
    path.mkdir()
    _write(path / "bad.csv",
           "period,channel,settled_spend,rebate,status\n"
           "2026-09,douyin_feed,1200.00,36.00,confirmed\n"
           "2026-09,bogus,900.00,0,confirmed\n")
    settings = _settings(tmp_path)
    with pytest.raises(errors.BadRequest):
        imports.import_file(settings, store, path / "bad.csv")
    assert store.settlement_counts()["batches"] == 0


@pytest.mark.parametrize("period", ["2026/09", "2026-13", "", "九月"])
def test_bad_period_is_rejected(tmp_path, store, period):
    _seed_contract(tmp_path)
    path = tmp_path / "settlement"
    path.mkdir()
    _write(path / "bad.csv",
           "period,channel,settled_spend,rebate,status\n"
           f"{period},douyin_feed,1200.00,36.00,confirmed\n")
    settings = _settings(tmp_path)
    with pytest.raises(errors.BadRequest):
        imports.import_file(settings, store, path / "bad.csv")


def test_ledger_recomputes_when_older_period_arrives_later(tmp_path, store):
    _seed_contract(tmp_path)
    settings = _settings(tmp_path)
    ledger_contract = contract_rules.load(settings, CHANNELS)
    entries = [
        _line("2026-10", "douyin_feed", 120_000, 3600, "confirmed", batch_id="st-oct"),
        _line("2026-09", "douyin_feed", 110_000, 3300, "confirmed", batch_id="st-sep"),
    ]
    item = ledger.channel_ledger("douyin_feed", entries,
                                 ledger_contract["channels"]["douyin_feed"])
    # 后补上月：按账期排序重算，9 月 11 万分先跨档，10 月增量 = 23 万分*3% − 11 万分*3%
    assert [line["period"] for line in item["lines"]] == ["2026-09", "2026-10"]
    assert item["lines"][0]["cumulative_after_cents"] == 110_000
    assert item["lines"][0]["tier_index"] == 1
    assert item["lines"][1]["cumulative_after_cents"] == 230_000
    assert item["settled_spend_cents"] == 230_000
    assert item["confirmed_rebate_cents"] == 230_000 * 300 // 10_000


def test_sample_settlement_files_import_and_repeat(tmp_path, store):
    from app import config

    settings = config.Settings(db_path=tmp_path / "db.sqlite",
                               sample_dir=config.ROOT / "sample_data")
    first = imports.import_samples(settings, store)
    assert first["files"] >= 2
    assert first["imported"] == first["files"]
    second = imports.import_samples(settings, store)
    assert second["imported"] == 0
    assert second["repeated"] == first["files"]
    counts = store.settlement_counts()
    assert counts["batches"] == first["files"]
    assert counts["pending"]["lines"] >= 1
    assert counts["confirmed"]["rebate_cents"] > 0
