# REQ-009 内容检测跨任务关联证据

## 实现范围

- 内容检测批次响应返回可选 `workflow_id`，创建时校验并保存已有 workflow 关联。
- 未完成批次通过 worker 更新 `inspection` 阶段；正常完成同步为 `succeeded`。
- 仅缓存命中的完成、部分结果和依赖不可用会在批次事务内同步稳定终态和原因码。
- 详情读取会保留批次与 workflow 的关联，服务重启恢复不会重新生成关联。

## 安全边界

- 本切片只处理本地批次与 workflow 状态，不输出磁力链接、Cookie、Token、pickcode、完整播放
  token 或真实直链。
- qBittorrent 和 115 的真实写入能力、STRM 播放能力不因该关联而开启。

## 验收命令

```text
./.venv/bin/python -m pytest -q tests/integration/test_inspection_api.py
./.venv/bin/python -m ruff check \
  src/watch_assistant/services/inspection.py src/watch_assistant/schemas.py \
  tests/integration/test_inspection_api.py
git diff --check
```

结果：专项测试 `23 passed`，Ruff 通过，`git diff --check` 通过。
