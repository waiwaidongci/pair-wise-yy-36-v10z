# 企业排污许可与超标处置

汇总监测和工况，判断排放超标并跟踪复测、整改、执法与复查。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8313
```

默认端口为`8313`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/readings`，登记采样（采样时刻、浓度、限值、报告编号）
- `GET /api/items/{id}/readings`
- `GET /api/items/{id}/reviews`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：operator, compliance_officer, director, viewer。按浓度与许可限值计算超标倍数，异常读数先进入评估；关闭前必须没有未完成整改项。

## 超标跟踪

- 登记采样读数需提交`sampled_at`、`concentration`、`limit_value`、`report_no`；同一报告编号重复提交沿用首次结果（返回200，不再产生复查或审计）。
- 浓度超出限值即建未结复查；之后采样时刻不早于超标读数的最早达标读数自动结清全部未结复查。
- 同一事件30天内累计3次超标自动升为`major`，读数回落不降级。
- 存在未结复查时关闭事件返回409并保持原状态。
- 列表与详情返回`exceedance_count_30d`（30天超标次数）、`current_level`（当前等级）、`nearest_deadline`（最近复查期限）和`open_reviews`。
- 现有数据库启动时自动升级（新增readings、reviews表），权限、版本控制与审计链保持不变。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
