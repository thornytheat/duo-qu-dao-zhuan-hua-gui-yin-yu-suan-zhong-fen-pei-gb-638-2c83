# 样例数据

- `dirty/`：触点 28 行、订单 7 行、别名 6 行，含 GB18030 编码、UTF-8 BOM、CRLF 混用、重复表头、
  空行、列数不齐、末行截断、时区混写、迟到入库、编号重复冲突、空渠道、未知渠道、
  千分位、全角数字、万元单位、空值哨兵、跨设备别名与自引用。
- `clean/`：6 条触点 3 张订单，走同一条代码路径。
- `header_only/`、`blank/`、`empty/`：三种空态。
- `budget_plan.json`：预算总额与各渠道上下限。
- `rebate_contract.json`：渠道累计花费阶梯返点合同（`contract_id=CT-2026-Q4`）。
- `settlement/`：媒体月结批次。
  - `2026-09.csv`：六渠道首期正式结算，多数渠道停在第一档。
  - `2026-10.csv`：第二期正式结算，`wechat_moments`、`baidu_search`、`tmall_ads`、
    `private_group` 跨档，返点按整组回溯计算。
  - `2026-11-pending.csv`：第三期待确认，只展示预估返点，不进入收益与预算。
