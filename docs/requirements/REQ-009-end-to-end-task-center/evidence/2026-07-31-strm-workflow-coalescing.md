# REQ-009 STRM 子任务关联证据

日期：2026-07-31

## 本次范围

- 同一媒体库目录的多个整理完成事件在领取前会绑定同一个 dirty generation lease。
- 领取时已存在的事件共享 lease token；领取后新增的事件保留到下一代，不会被提前消费。
- STRM worker 从同一 lease 收集全部关联 workflow，并将相同的 `strm_dirty_generation` 子任务状态
  扇出到每个 workflow。
- 完成和重试按 lease token 一起收敛已领取事件，避免 coalescing 后旧事件永久处于 pending。

## 验证

```text
tests/unit/test_directory_dirty_worker.py::test_dirty_worker_fans_out_coalesced_generation_to_all_workflows PASSED
tests/unit/test_directory_dirty_worker.py 全部通过
tests/unit/test_organization_outbox.py 全部通过
```

专项结果：`17 passed`。

关键断言：两个独立 workflow 触发同一目录变更后，单次 worker 运行使两个 STRM 阶段均为
`succeeded`，拥有相同 generation 子任务 ID，且该目录的两个 dirty 事件均为 `consumed`。

## 未覆盖

生产媒体库、生产 STRM 播放入口、真实媒体服务器和完整通知矩阵仍按 `BLOCKERS.md` 保持阻断；
本证据不代表生产能力已上线。
