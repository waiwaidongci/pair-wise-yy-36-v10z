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
- `GET /api/items/{id}/readings`
- `POST /api/items/{id}/readings`，登记采样时刻、浓度、许可限值和报告编号
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：operator, compliance_officer, director, viewer。按浓度与许可限值计算超标倍数，异常读数先进入评估；关闭前必须没有未完成整改项。

## 超标跟踪

- `POST /api/items/{id}/readings`请求体：`sampled_at`（ISO 8601，支持时区）、`concentration`、`limit_value`、`report_no`。同一事件下`report_no`重复提交时沿用首次结果（响应带`duplicate:true`，HTTP 200），不重复建复查。
- 浓度超过许可限值即自动创建未结复查；之后最早的达标读数结清此前所有未结复查。未结复查存在时关闭事件返回409并保持原状态。
- 同一事件采样时刻30天窗口内第3次超标，事件自动升为重大（`major`）；此后读数回落也不降级，版本照常递增。
- 列表与详情额外返回`exceedance_count_30d`（近30天超标次数）、`current_level`（当前等级）、`next_deadline`（最近复查期限）。
- 判定集中在`src/rules.py`、存储在`src/repository.py`、编排在`src/service.py`、接口在`src/http_api.py`。旧版SQLite库首次打开自动升级，新增`readings`与`reviews`表，审计链与`expected_version`并发控制不变。


## 测试

```bash
python3 -m unittest discover -s tests -v
```
