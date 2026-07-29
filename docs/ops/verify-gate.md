# 验收门禁

`scripts/verify.sh` 是发布前的离线门禁，按顺序执行：

1. Python 全量 pytest。
2. `ruff check src tests scripts`。
3. 前端 Vitest。
4. 前端 Vite production build。

任何阶段失败都会以非零状态退出并标出阶段名。全部通过后，日志和提交号会写入被 Git
忽略的 `evidence/<short-hash>-<UTC 时间>/`。

115 API live 验证不属于离线门禁。使用 `scripts/verify-live.sh` 单独运行；它依赖
`C:\Users\98275\.115ts-secrets\.p115-cookie` 和内网主机 `192.168.6.236`，建议每日运行一次。

固定约束仍然适用：C03 live 文件分页大小保持为 1，fixture probe 必须先取得 receipt 再
verify，990009 由既有 runner 的 3 秒重试处理。
