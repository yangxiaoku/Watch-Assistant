# STRM 只读实时临时输出契约

`scripts/p115_strm_readonly_live_runner.py` 是独立的低风险验收入口，不接入应用
API、worker 或发布流程。入口默认关闭，只有设置
`WATCH_ASSISTANT_P115_STRM_READONLY_LIVE=1`（CLI 使用 `--live`）后才会继续。

## 边界

- 只接受稳定的数字根目录 ID，并把它作为唯一授权根目录。
- 远端 transport 只暴露 `fs_files` 和 `fs_info`；`fs_files_app`、`fs_info_app`
  仅由固定只读 transport 在只读端点降级时内部使用。
- 远端递归扫描固定分页大小为 1，必须得到 `complete=true`、完成状态和快照版本。
- 全量 STRM 输出和增量对账都写入 `TemporaryDirectory` 下的本地 SQLite 与 STRM
  根目录。增量调用固定使用 `retire_removed=false`，不执行本地清理。
- 入口不导入重命名、移动、恢复、隔离、删除、播放或 cleanup apply 能力。

临时根目录在进程结束时删除；`--output` 只用于保存脱敏 JSON 报告，不是 STRM
输出目录。

## 报告证据

成功报告必须包含 `complete`、`root_identity_verified`、递归 `scope`、两次扫描的
分页和条目统计、全量/增量统计、`output_root_is_temporary`、
`database_is_temporary`，以及 `write_calls=0` 和 `remote_write_calls=0`。
报告只允许输出稳定 ID 和计数，不输出 Cookie、Token、pickcode、文件名或完整远端
路径。失败统一为 `status=blocked`，附稳定机器码和中文错误说明，并保持
`fail_closed=true`。
