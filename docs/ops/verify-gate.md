# 验收门禁

当前本地工作区为 macOS 桌面目录 `/Users/apple/Desktop/115ts`。运行门禁前必须确认命令
从该目录执行，并使用当前工作区的 `./.venv/bin/python`；不要使用另一工作树的 Python
环境。

`scripts/verify.sh` 是发布前的离线门禁，按顺序执行：

1. 当前工作区虚拟环境中的 Python 全量 pytest。
2. `./.venv/bin/python -m ruff check src tests scripts`。
3. 前端 Vitest。
4. 前端 Vite production build。

当前工作区未包含 `.venv`、Docker CLI 或 PowerShell。缺少这些工具时，应记录为环境准备
问题，不能把系统 Python、未验证的外部虚拟环境或截图结果当作完整门禁证据。

任何阶段失败都会以非零状态退出并标出阶段名。全部通过后，日志和提交号会写入被 Git
忽略的 `evidence/<short-hash>-<UTC 时间>/`。

115 API live 验证不属于离线门禁。使用 `scripts/verify-live.sh` 单独运行；它依赖
`C:\Users\98275\.115ts-secrets\.p115-cookie` 和内网主机 `192.168.6.236`，建议每日运行一次。

固定约束仍然适用：C03 live 文件分页大小保持为 1，fixture probe 必须先取得 receipt 再
verify，990009 由既有 runner 的 3 秒重试处理。
