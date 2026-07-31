# REQ-009 资源搜索跨任务关联证据

## 实现范围

- 资源搜索请求接受可选 `workflow_id`，服务端验证工作流存在后保存到
  `resource_search_jobs`。
- 资源搜索任务的创建、恢复、运行和终态保存会同步 workflow 的 `discovery` 阶段，
  子任务类型为 `resource_search`，子任务 ID 为持久搜索任务 ID。
- 同一搜索任务已经关联其他 workflow 时返回 `workflow_id_conflict`，不会覆盖原关联。
- 迁移 `052_resource_search_workflow` 对已有数据库幂等增加字段和索引。

## 安全边界

- 该切片只关联本地搜索任务，不读取或写入 Cookie、Token、pickcode、磁力链接、完整播放
  token 或真实直链。
- workflow 关联不改变搜索缓存、资源加密存储或任何 115 写入门禁。

## 验收命令

```text
./.venv/bin/python -m pytest -q \
  tests/unit/test_migrations.py tests/integration/test_search_api.py \
  tests/contracts/test_api_error_catalog.py tests/contracts/test_event_catalog.py

./.venv/bin/python -m ruff check \
  src/watch_assistant/services/search.py src/watch_assistant/api/search.py \
  src/watch_assistant/migrations.py src/watch_assistant/schemas.py \
  src/watch_assistant/models.py src/watch_assistant/services/api_errors.py \
  tests/unit/test_migrations.py tests/integration/test_search_api.py

git diff --check
```

结果：专项测试 `56 passed`，Ruff 通过，`git diff --check` 通过。

生产 115 整理、STRM、外部搜索服务和完整跨平台 workflow 验收仍按需求阻断记录保持门控。
