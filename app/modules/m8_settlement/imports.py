"""结算批次导入：媒体月结文件 → 逐渠道行 → 原子落库。

三条不变量：
- 批次键由文件内容决定，同一份月结单重试命中同一批次，返点不会重复累加；
- 整批先校验后写入，任何一行不合法都整批拒绝，旧台账原样保留；
- 同一渠道同一账期的正式结算只能有一份，重复账期整批拒绝并给出原因码。
"""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime, timezone
from pathlib import Path

from ... import errors
from ...manifest import file_digest
from ..m1_ingest import decode
from ..m2_window import coerce
from ..m2_window.contract import CHANNEL_CODES
from . import contract as contract_rules
from .ledger import CONFIRMED, PENDING

COLUMNS = ("period", "channel", "settled_spend", "rebate", "status")
SETTLEMENT_DIR = "settlement"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def directory(settings) -> Path:
    return Path(settings.sample_dir) / SETTLEMENT_DIR


def sample_files(settings) -> list[Path]:
    base = directory(settings)
    if not base.is_dir():
        return []
    return sorted(path for path in base.iterdir()
                  if path.suffix.lower() == ".csv" and path.is_file())


def _parse_period(raw: str) -> str:
    text = (raw or "").strip()
    try:
        moment = datetime.strptime(text, "%Y-%m")
    except ValueError:
        raise errors.BadRequest(f"账期必须为 YYYY-MM：{text or '(空)'}",
                                {"period": text})
    return moment.strftime("%Y-%m")


def _parse_status(raw: str) -> str:
    text = (raw or "").strip().lower()
    if text in {"confirmed", "settled", "已确认", "已结算"}:
        return CONFIRMED
    if text in {"pending", "待确认", "待结算"}:
        return PENDING
    raise errors.BadRequest(f"返点状态未知：{raw or '(空)'}",
                            {"allowed": [CONFIRMED, PENDING], "status": raw})


def parse_csv(text: str) -> list[dict]:
    reader = csv.reader(io.StringIO(text, newline=""))
    rows = list(reader)
    if not rows:
        raise errors.BadRequest("结算文件为空")
    header = [cell.strip().lstrip("﻿") for cell in rows[0]]
    missing = [name for name in COLUMNS if name not in header]
    if missing:
        raise errors.BadRequest(f"结算文件缺少列：{','.join(missing)}",
                                {"missing": missing, "header": header})
    index = {name: header.index(name) for name in COLUMNS}
    lines: list[dict] = []
    for row_no, cells in enumerate(rows[1:], start=1):
        if not any(cell.strip() for cell in cells):
            continue
        if len(cells) != len(header):
            raise errors.BadRequest(
                f"结算文件第 {row_no} 行列数与表头不一致",
                {"line": row_no, "expected": len(header), "actual": len(cells)})
        lines.append({name: cells[index[name]] for name in COLUMNS})
    if not lines:
        raise errors.BadRequest("结算文件没有数据行")
    return lines


def validate(lines: list[dict], contract: dict) -> list[dict]:
    """逐行归一为整数分；任何一行不合法都抛错，调用方整批放弃。"""
    if len(lines) > 5000:
        raise errors.BadRequest("单个结算批次行数超过 5000", {"lines": len(lines)})
    result = []
    seen: set[tuple[str, str]] = set()
    for row_no, raw in enumerate(lines, start=1):
        period = _parse_period(raw.get("period"))
        channel = (raw.get("channel") or "").strip().lower()
        if channel not in CHANNEL_CODES:
            raise errors.BadRequest(f"结算行 {row_no} 渠道未知：{channel or '(空)'}",
                                    {"line": row_no, "channel": channel,
                                     "allowed": list(CHANNEL_CODES)})
        status = _parse_status(raw.get("status"))
        spend = coerce.parse_amount(raw.get("settled_spend"))
        if spend["status"] != "ok" or int(spend["cents"] or 0) <= 0:
            raise errors.BadRequest(
                f"结算行 {row_no} 已结算花费无效：{raw.get('settled_spend') or '(空)'}",
                {"line": row_no, "field": "settled_spend"})
        rebate = coerce.parse_amount(raw.get("rebate"))
        if status == CONFIRMED and rebate["status"] != "ok":
            raise errors.BadRequest(
                f"结算行 {row_no} 已确认返点缺失：{raw.get('rebate') or '(空)'}",
                {"line": row_no, "field": "rebate"})
        reported = int(rebate["cents"]) if rebate["status"] == "ok" else None
        if reported is not None and reported < 0:
            raise errors.BadRequest(f"结算行 {row_no} 返点为负", {"line": row_no})
        key = (period, channel)
        if key in seen:
            raise errors.BadRequest(f"同一批次内账期 {period} 渠道 {channel} 重复",
                                    {"line": row_no, "period": period,
                                     "channel": channel})
        seen.add(key)
        item = contract["channels"][channel].rebate_for(0, int(spend["cents"]))
        result.append({
            "period": period,
            "channel": channel,
            "settled_spend_cents": int(spend["cents"]),
            "rebate_cents": int(reported if reported is not None
                                else item["rebate_cents"]),
            "reported_rebate_cents": reported,
            "rebate_status": status,
            "tier_index": item["tier"].index,
            "tier_rate_bp": item["tier"].rate_bp,
            "tier_min_spend_cents": item["tier"].min_spend_cents,
        })
    return result


def _batch_identity(source: str, digest: str, lines: list[dict]) -> dict:
    return {
        "batch_id": "st-" + hashlib.sha256(
            (source + ":" + digest).encode("utf-8")).hexdigest()[:16],
        "digest": digest,
        "source": source,
        "period": min(line["period"] for line in lines),
        "line_count": len(lines),
        "received_at": _now(),
    }


def _existing_confirmed(store) -> dict[tuple[str, str], str]:
    """已入库的正式结算：{(账期, 渠道): 批次号}，用于拦截重复账期。"""
    result: dict[tuple[str, str], str] = {}
    for line in store.settlement_lines():
        if line["rebate_status"] == CONFIRMED:
            result[(line["period"], line["channel"])] = line["batch_id"]
    return result


def _conflicts(lines: list[dict], existing: dict) -> list[dict]:
    return [{"period": line["period"], "channel": line["channel"]}
            for line in lines
            if line["rebate_status"] == CONFIRMED
            and (line["period"], line["channel"]) in existing]


def import_payload(settings, store, payload: dict) -> dict:
    """JSON 批次入口：与 CSV 走同一套校验与去重。"""
    contract = contract_rules.load(settings, CHANNEL_CODES)
    raw_lines = payload.get("lines") or []
    if not isinstance(raw_lines, list) or not raw_lines:
        raise errors.BadRequest("结算批次至少需要一行")
    lines = validate(raw_lines, contract)
    source = str(payload.get("source") or "json")
    # 摘要必须覆盖全部会影响台账的字段，否则改了返点金额重试会被误判为同一批
    digest = str(payload.get("digest") or hashlib.sha256(
        repr(sorted((line["period"], line["channel"],
                     line["settled_spend_cents"], line["rebate_cents"],
                     line["rebate_status"], line["reported_rebate_cents"])
                    for line in lines)).encode("utf-8")).hexdigest())
    return _store(settings, store, contract, source, digest, lines,
                  explicit_batch_id=payload.get("batch_id"))


def import_file(settings, store, path: Path) -> dict:
    contract = contract_rules.load(settings, CHANNEL_CODES)
    decoded = decode.read_text(path)
    lines = validate(parse_csv(decoded["text"]), contract)
    return _store(settings, store, contract, path.name,
                  file_digest(path), lines)


def import_samples(settings, store) -> dict:
    files = sample_files(settings)
    if not files:
        raise errors.BadRequest("没有结算样例文件",
                                {"dir": str(directory(settings))})
    results = [import_file(settings, store, path) for path in files]
    return {"files": len(files), "imported": sum(1 for item in results if not item["repeat"]),
            "repeated": sum(1 for item in results if item["repeat"]),
            "batches": results}


def _store(settings, store, contract: dict, source: str, digest: str,
           lines: list[dict], explicit_batch_id: str | None = None) -> dict:
    identity = _batch_identity(source, digest, lines)
    if explicit_batch_id:
        identity["batch_id"] = str(explicit_batch_id)
    if store.get_settlement_batch(identity["batch_id"]) is not None:
        return {"batch_id": identity["batch_id"], "repeat": True,
                "reason": "ST_BATCH_REPEAT",
                "lines": len(lines), "accepted": 0}
    conflicts = _conflicts(lines, _existing_confirmed(store))
    if conflicts:
        raise errors.SettlementConflict(
            "同一渠道账期已有正式结算，整批拒绝",
            {"conflicts": conflicts, "source": source,
             "reason": "ST_DUPLICATE_PERIOD"})
    confirmed = [line for line in lines if line["rebate_status"] == CONFIRMED]
    pending = [line for line in lines if line["rebate_status"] == PENDING]
    batch = {
        **identity,
        "contract_id": contract["contract_id"],
        "contract_digest": contract["digest"],
        "confirmed_lines": len(confirmed),
        "pending_lines": len(pending),
        "settled_spend_cents": sum(line["settled_spend_cents"] for line in lines),
        "confirmed_rebate_cents": sum(line["rebate_cents"] for line in confirmed),
        "pending_rebate_cents": sum(line["rebate_cents"] for line in pending),
        "detail": {"periods": sorted({line["period"] for line in lines}),
                   "channels": sorted({line["channel"] for line in lines})},
    }
    stored_lines = [{**line, "batch_id": identity["batch_id"],
                     "detail": {"source": source,
                                "contract_id": contract["contract_id"]}}
                    for line in lines]
    created = store.save_settlement_batch(batch, stored_lines)
    if not created:
        return {"batch_id": identity["batch_id"], "repeat": True,
                "reason": "ST_BATCH_REPEAT", "lines": len(lines), "accepted": 0}
    reasons = []
    if pending:
        reasons.append("ST_PENDING_EXCLUDED")
    # 媒体返点与合同应计的差异要等账期全部就位后按累计档位重算，由台账在读取时判定，
    # 这里不按单期花费猜档位，避免先导入的月份被误判。
    return {"batch_id": identity["batch_id"], "repeat": False,
            "period": identity["period"], "lines": len(lines),
            "confirmed_lines": len(confirmed), "pending_lines": len(pending),
            "settled_spend_cents": batch["settled_spend_cents"],
            "confirmed_rebate_cents": batch["confirmed_rebate_cents"],
            "pending_rebate_cents": batch["pending_rebate_cents"],
            "accepted": len(lines), "reasons": reasons}
