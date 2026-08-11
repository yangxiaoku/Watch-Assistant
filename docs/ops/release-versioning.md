# 发布物版本规范

发布包只能从干净且已提交的 Git 工作树生成，并且必须显式传入与 `HEAD` 完全相同的 40 位
Git SHA：`scripts/build_release.sh <full-sha>`。脚本会从该 SHA 的临时源码树执行
`npm ci` 和生产前端构建，不复用工作树的 `frontend/dist`。包名为
`watch-assistant-<hash7>-<yyyymmdd-HHMM>.tar.gz`，其中 hash 是指定完整 SHA 的短前缀。

包内根目录包含 `VERSION` 和 `release-manifest.json`。前者记录完整 `commit`、UTC
`build_time` 和 `branch`，后者记录完整 commit、源码归档 SHA-256 和前端产物 SHA-256。
`scripts/verify_release_artifact.sh` 会检查归档内容、完整 SHA、两个摘要、`SHA256SUMS`，
并用 worktree `.venv` 执行独立启动 smoke。默认产物目录是 `release-archive/<UTC 日期>/`；
该目录只用于本地或发布流水线归档，不提交到 Git。

构建和校验脚本拒绝未提交修改、未跟踪文件、系统 Python fallback、SHA 不匹配或历史包冒充
当前发布物；重新打包必须产生新的 commit 绑定文件名。

发布工具前置条件：Linux/Git Bash 环境需要 `git`、`tar`、`sha256sum`、`stat`、`awk`、`grep`、`cmp`
和 `npm`，并且 worktree 内必须存在合规 Python 环境 `.venv/bin/python`。`build_release.sh` 和
`verify_release_artifact.sh` 会在
manifest 创建、读取、字段校验和 startup smoke 前强制选择上述 `.venv` Python，缺少时直接失败，
不会回退系统 Python。manifest 使用 Python 标准库 JSON helper 生成和校验，不要求安装 `jq`。
