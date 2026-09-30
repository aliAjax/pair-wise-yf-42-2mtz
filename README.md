# 动物园谱系与繁育协调

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8308`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8308
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `animal`：个体谱系；`pairing`：配对建议；`transfer`：机构和运输记录。
- 运输记录可用 `pairing_id` 关联配对，运输流程由此与配对批准衔接：授权/发运/到达都会校验批准是否仍有效。

## 协调语义

- **乐观批准**：`approve` 必须带 `expected_version`。两人同时基于同一版本批准时只有一人成功，后到者收到 `409 ConflictError`，响应体 `details` 给出 `expected_version`/`current_version`。
- **批准快照**：批准时记录双方状态、谱系字段与亲缘系数（`approval_basis`）。
- **自动失效**：个体被隔离、标记死亡或谱系（`update_pedigree`）更新后，所有引用它且尚未执行的 `approved` 配对自动变为 `invalidated`，`data.invalidated` 保存人可读原因与代码（如 `animal_status_changed`、`pedigree_changed`、`inbreeding_exceeded`）。已完成（`completed`）的配对与运输步骤保留不动。
- **运输/完成联动**：对失效配对执行 `complete` 或关联运输的 `authorize`/`ship`/`arrive` 返回 `422 ApprovalInvalidatedError`，`details.reason` 说明失效原因；已完成的步骤不回滚，解除原因后用 `reapprove` 重新批准即可从当前步骤继续。
- **步骤台账与幂等重试**：配对完成、运输授权/发运/到达登记在 `process_steps`。失败后重试已完成步骤不会重复登记后代或重复占用个体（占用字段为 `animal.data.occupied_by`）。动作请求可带 `idempotency_key`（请求体或 `Idempotency-Key` 头）。

### 配对/运输动作

- `pairing`：`approve`（需 `sire_id`、`dam_id`，可带 `approvals`）、`reject`、`reapprove`（从 `invalidated` 恢复并刷新快照）、`complete`（`offspring_ids` 或 `offspring` 规格；后者会自动登记后代个体）。
- `transfer`：`authorize`（`permit_id`）、`ship`（`transport_id`，占用个体）、`arrive`（`arrival_date`，释放占用）。
- `animal`：`mark_deceased`、`quarantine_animal`、`release_quarantine`、`update_pedigree`（`sire_id`/`dam_id`，状态不变）。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

谱系系数是简化亲缘规则，不替代专业谱系软件、遗传咨询或法定动物运输许可。
