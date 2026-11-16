# 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/import` | 运行一次，body `{"dataset":"dirty"}` |
| GET | `/api/runs` | 运行列表 |
| GET | `/api/runs/{run_id}` | 运行详情（含四项账目） |
| GET | `/api/runs/{run_id}/log` | 日志，`after=<seq>` 增量拉取 |
| GET | `/api/contract` | 字段契约、窗口、模型、渠道 |
| GET | `/api/quarantine` | 隔离行，`code=` 过滤 |
| GET | `/api/quarantine.csv` | 隔离区 CSV |
| GET | `/api/attribution` | 归因结果，`model=` 五选一 |
| POST | `/api/budgets` | 预算重排，body `{plan_version,total_yuan,model,cost_basis}` |
| GET | `/api/budgets` | 方案历史与下一版号 |
| POST | `/api/confidence` | 重采样区间，body `{model,resamples,seed,alpha_pct}` |
| GET | `/api/confidence` | 同上，参数走查询串 |
| GET | `/api/settlements` | 返点台账：合同阶梯、逐渠道花费与档位、批次、规划口径；`model=` 选归因模型 |
| POST | `/api/settlements` | 导入结算批次，body `{source,batch_id?,lines:[{period,channel,settled_spend,rebate,status}]}` |
| POST | `/api/settlements/samples` | 导入 `sample_data/settlement/` 下的样例批次（同批幂等） |
| GET | `/api/rebate-contract` | 返点合同：渠道阶梯、门槛、费率 |
| GET | `/api/batches` | 批次列表 |
| GET | `/api/batches/{run_id}` | 批次详情 |
| GET | `/api/export.csv` | 证据行 CSV，`kind=touch|order|credit|rebate` |
| GET | `/api/export.json` | 整体 JSON（含 `settlement` 与 `rebates`） |
| GET | `/api/manifest` | 运行清单（含复算 run_id） |
| GET | `/healthz` | 存活检查 |

## 失败码

| HTTP | 错误码 | 场景 |
|---|---|---|
| 400 | `E_BAD_REQUEST` | 未知模型、未知导出类型、版本号跳跃、重采样次数越界、结算行不合法 |
| 404 | `E_RUN_NOT_FOUND` | run_id 不存在 |
| 404 | `E_DATASET_NOT_FOUND` | 数据集不存在 |
| 409 | `E_STALE_PLAN_VERSION` | 旧方案版本提交 |
| 409 | `E_SETTLEMENT_CONFLICT` | 同渠道同账期已有正式结算，整批拒绝 |
| 409 | `E_BUDGET_EVIDENCE_MISSING` | 要求 `settled` 口径但缺少已核销证据，未生成新版本 |
| 422 | `E_BUDGET_INFEASIBLE` | 总额低于下限之和或高于上限之和 |

## 结算批次幂等

- `batch_id = "st-" + sha256(source + ":" + digest)[:16]`，摘要覆盖账期、渠道、已结算花费、
  媒体返点与状态；同一内容重试返回 `{repeat:true, accepted:0, reason:"ST_BATCH_REPEAT"}`。
- 整批先校验后写入：任一行字段不合法返回 400，旧台账不动。
- 同渠道同账期已有 `confirmed` 行时返回 409，`reason=ST_DUPLICATE_PERIOD`。
- `pending` 行可以多次导入（不同账期），但永远不会进入可核销收益与预算分母。
