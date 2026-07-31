# REQ-017 本地恢复安全证据

日期：2026-07-31

本证据只覆盖临时 SQLite fixture，不代表生产部署已完成恢复验收。

## 已验证范围

- 恢复前 SHA-256、SQLite 完整性和迁移兼容性校验。
- 恢复前快照、临时文件、原子替换、sidecar 清理和失败回滚。
- 应用数据库恢复后的外键、任务、订阅、整理、库存和 STRM 引用校验，以及业务摘要比对。
- Web 二次批准与离线 `--approval-id` 联动；批准摘要、双人身份、有效期、维护代次和一次性 journal。
- 备份历史、校验状态、保留序号、显式确认删除和唯一备份保护。
- 默认关闭的受控目录加密副本、manifest 摘要、独立恢复密钥和 stdin 解密恢复。

## 离线命令

```text
PYTHONPATH=src .venv/bin/python -m pytest -q tests/unit tests/contracts
PYTHONPATH=src .venv/bin/python -m pytest -q tests/integration/test_backups_api.py tests/integration/test_restore_backup_script.py
PYTHONPATH=src .venv/bin/python -m ruff check src tests scripts
git diff --check
```

结果：unit `576 passed`；contracts `168 passed`；integration `336 passed, 1 skipped`；
备份恢复专项 `25 passed`；前端 Vitest `120 passed`；Playwright 桌面/移动回归 `149 passed`；
生产构建、Ruff、编译和空白检查通过。本轮前端门禁通过 Codex 隔离 Node runtime 执行，系统
PATH 本身没有 Node/npm。

本机使用隔离 `.venv` 验证；项目锁定的 `cryptography==49.0.0` 在当前 x86_64 macOS
环境没有可用 wheel，源码构建缺少 OpenSSL/pkg-config，因此回归使用兼容的
`cryptography==48.0.1`。其余项目依赖按锁定版本安装。

## 尚未覆盖

- `192.168.6.236` 真实停机恢复、回滚、重启和恢复后业务核对。
- 受控异地目标目录的部署挂载、权限和长期保留演练。
- `192.168.6.236` 真实停机恢复、回滚、重启和恢复后业务核对不在本证据范围内。
- 受控异地目标目录的部署挂载、权限和长期保留演练不在本证据范围内。
- 上述前端验证记录不代表真实部署验收。
