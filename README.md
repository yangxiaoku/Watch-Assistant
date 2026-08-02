# Watch Assistant

轻量自托管的个人观影辅助工具。它展示 TMDB 热门电影和电视剧并支持片名搜索，进入详情后聚合 PanSou 的磁力与 115 分享结果，并把敏感链接加密保存在本机 SQLite 中。

## 当前能力

- 独立 Vue Web 工作台与 TMDB 用户脚本。
- 首页展示 TMDB 电影和电视剧榜单，支持分类浏览、分页和混合片名搜索。
- PanSou 同时查询中文名、中文名加年份、英文名和英文名加年份，完整合并并按 infohash 去重。
- 磁力 BTIH 与名称资料校验、缺失 `dn` 自动补全、语义排序、备用标题回退和部分上游失败保护。
- 校验后稳定排序并只缓存/返回最高 30 条磁力；115 分享不计入上限，结果附带三项 0-100 评分。
- 电视剧详情包含季度元数据，指定季度追加四个季度查询并使用独立 v4 缓存；预热只处理全部季度。
- 24 小时新鲜缓存、7 天故障回退，以及香港时间每天零点对七个首页榜单进行预热和失败重试。
- 持续复查暂时无资源的电影或电视剧，并根据语义误匹配与 115 失效记录降低不可靠来源排序。
- Web 密码会话、用户脚本独立 Bearer Token、CSRF 与限流。
- SQLite WAL、Fernet 敏感字段加密和保留期清理。
- 持久任务状态机与崩溃后的 `uncertain` 防重复提交。

## 115 影视库整理与 STRM 状态

REQ-001（115 影视库自动整理）和 REQ-002（STRM 全量与增量同步）当前均为“待验收”：
受管夹具和离线阶段证据已具备，但生产范围、生产媒体库和外部播放/联动证据仍未完成，
相关代码和开关不代表已上线。生产能力、实际版本和部署方式必须以发布前的只读核对以及
`/api/v1/health` 返回为准；本文不硬编码生产 commit 或开关状态。

磁力和 115 分享任务统一通过独立的 p115 gateway；P115 Cookie 只来自服务端配置的只读
Cookie 文件或应用内托管设备。分享推送、影视库整理、删除和 STRM 写入继续受独立契约、
功能开关、计划/确认、幂等和审计门禁约束，未验收时保持关闭。

## 当前发布与验收边界

- 唯一发布分支是 `codex/publish-main`。发布前先执行 `git fetch origin`，再用
  `git rev-parse origin/codex/publish-main` 获取实际最新基线；本文不固定可能过期的 commit。
- 当前发布基线已合入 [PR #30](https://github.com/yangxiaoku/Watch-Assistant/pull/30) 的 STRM/任务租约
  门禁和中文工作台调整；对应离线证据可从
  [工作台单测](frontend/tests/LibraryWorkbenchView.spec.ts)、[组织工作台单测](frontend/tests/OrganizationWorkbenchView.spec.ts)
  和 [工作台 E2E](frontend/e2e/organization-workbench.spec.ts) 复核。后续工作台调整也已由
  [PR #32](https://github.com/yangxiaoku/Watch-Assistant/pull/32) 合入，证据见
  [工作台 E2E](frontend/e2e/workbench-360.spec.ts)、[工作流导航测试](frontend/tests/coreWorkflowNavigation.spec.ts)
  和 [前端 API 测试](frontend/tests/api.spec.ts)。这些是代码和离线/受管夹具证据，不证明已生成发布包、
  已部署或已完成生产验收。
- Prowlarr 的离线 readiness 已通过 [PR #19](https://github.com/yangxiaoku/Watch-Assistant/pull/19) 和
  [PR #29](https://github.com/yangxiaoku/Watch-Assistant/pull/29)，契约证据见
  [Prowlarr 契约测试](tests/contracts/test_prowlarr_contract.py)。真实来源仍被 Internet Archive
  上游 timeout 阻断，当前不能表述为已配置可搜索的真实来源。
- p115 远程可用性证据已由 [PR #31](https://github.com/yangxiaoku/Watch-Assistant/pull/31) 合入当前基线，
  对应回归见 [p115 适配器测试](tests/integration/test_p115_adapter.py)、[任务 API 测试](tests/integration/test_tasks_api.py)
  和 [任务状态单测](tests/unit/test_tasks.py)。这些是 fail-closed 代码/离线证据；本轮未执行 live 或生产部署，
  因此不能把 p115 远程可用性或发布写成已完成。
- STRM manifest 与空目录清理加固已由 [PR #33](https://github.com/yangxiaoku/Watch-Assistant/pull/33) 合入，
  证据见 [STRM manifest 单测](tests/unit/test_strm_manifest.py)、[空目录计划单测](tests/unit/test_empty_directory_cleanup_plan.py)
  和 [空目录清理契约测试](tests/contracts/test_empty_directory_cleanup_contract.py)。这只表示当前基线具备
  更严格的离线 readiness，不表示生产 STRM、生产清理或永久删除已开启。
- 发布 manifest、来源和 systemd 回退门禁已由 [PR #14](https://github.com/yangxiaoku/Watch-Assistant/pull/14)、
  [PR #15](https://github.com/yangxiaoku/Watch-Assistant/pull/15)、[PR #16](https://github.com/yangxiaoku/Watch-Assistant/pull/16)、
  [PR #22](https://github.com/yangxiaoku/Watch-Assistant/pull/22) 和 [PR #23](https://github.com/yangxiaoku/Watch-Assistant/pull/23)
  合入；[发布 manifest 测试](tests/unit/test_release_manifest.py)、[部署脚本测试](tests/unit/test_release_deployment_scripts.py)
  和 [发布物 smoke 测试](tests/integration/test_release_archive_smoke.py) 只证明门禁覆盖，
  不证明当前生产版本或线上健康状态。

## 部署

要求：Docker Engine、Docker Compose，以及已存在的外部网络 `pansou_default`。应用只通过
该网络访问 PanSou；qBittorrent 和 115 按各自适配器配置，不随应用容器重启。

1. 从示例创建 `.env`，并建立 `secrets` 目录。
2. 生成四个只读 Secret 文件：

```powershell
New-Item -ItemType Directory -Force secrets
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" | Set-Content -NoNewline secrets/encryption_key
Set-Content -NoNewline secrets/tmdb_api_key "你的 TMDB API Key"
python -c "from pwdlib import PasswordHash; print(PasswordHash.recommended().hash(input('Web password: ')))" | Set-Content -NoNewline secrets/web_password_hash
python -c "from pwdlib import PasswordHash; print(PasswordHash.recommended().hash(input('Userscript token: ')))" | Set-Content -NoNewline secrets/script_token_hash
```

3. 检查并启动：

```powershell
WATCH_ASSISTANT_RELEASE="$(git rev-parse HEAD)" docker compose config
WATCH_ASSISTANT_RELEASE="$(git rev-parse HEAD)" docker compose up -d --build
curl http://127.0.0.1:8000/api/v1/health
```

访问 `http://服务器地址:8115/`。构建后的用户脚本位于容器内 Web 根目录，可从 `http://服务器地址:8115/watch-assistant.user.js` 获取。

缓存预热默认启用，时区为 `Asia/Hong_Kong`。可通过 `CACHE_WARM_ENABLED=false` 临时关闭，或用 `CACHE_WARM_TIMEZONE` 调整零点所在时区。PanSou 请求并发默认 6、媒体预热并发默认 3，可分别通过 `PANSOU_MAX_CONCURRENCY` 和 `CACHE_WARM_CONCURRENCY` 调整。

磁力内容检测默认使用 8 路并发、单条 30 秒超时、0.75 秒 qB 轮询和 10 秒单次 API 请求超时。可通过 `INSPECTION_CONCURRENCY`（1-16）、`INSPECTION_ITEM_TIMEOUT_SECONDS`（5-120）、`INSPECTION_POLL_INTERVAL_SECONDS`（0.25-5）和 `INSPECTION_REQUEST_TIMEOUT_SECONDS`（1-30）调整；qB 客户端仍只使用隔离 sidecar 的元数据停止策略。

认证后的维护接口包括 `GET /api/v1/cache/status`、`POST /api/v1/cache/retry`、`GET /api/v1/watchlist` 和 `GET /api/v1/sources/reliability`。它们用于查看预热状态、重试失败媒体、检查无资源观察列表和来源可靠性，不返回资源链接或密码。

服务器的 `8000` 端口已被占用时，可按 `.env.example` 使用 `8115`。若 Docker Hub 不可达，可以使用 `deploy/watch-assistant.service` 以 Python venv 运行；对应环境变量模板是 `deploy/watch-assistant.env.example`。

115 真实操作采用独立的 fail-closed 开关。整理计划、移动/重命名写入、永久删除和 STRM
生成/增量/清理/播放分别受 `ORGANIZATION_PLAN_ENABLED`、
`ORGANIZATION_EXECUTION_ENABLED`、`ORGANIZATION_WRITE_ENABLED`、
`PERMANENT_DELETE_ENABLED`、`STRM_FULL_ENABLED`、`STRM_INCREMENTAL_ENABLED`、
`STRM_CLEANUP_ENABLED` 和 `STRM_PLAYBACK_ENABLED` 控制；所有开关默认关闭。
真实移动/重命名还必须有 `ORGANIZATION_WRITE_CONTRACT_VERIFIED=true`，删除还必须有
`PERMANENT_DELETE_CONTRACT_VERIFIED=true`。契约未验收时，即使功能开关被误设为 true，
应用也不会启动真实 worker 或删除入口。发布时构建参数 `WATCH_ASSISTANT_RELEASE` 注入镜像
环境；健康接口和设置页使用同一版本值。缺少构建注入时安全回退为 `unknown`，不能据此判断
生产版本；Compose/Docker 发布不会接受缺少或非 full SHA 的构建参数。

使用 systemd 部署时，`watch-assistant.service` 可独立重启。qBittorrent sidecar 的升级或重启必须作为独立维护操作执行；不得通过重启应用隐式管理 qBittorrent 的生命周期。

### systemd 发布目录与切换顺序

使用发布包前，Linux/Git Bash 环境必须具备 `git`、`tar`、`sha256sum`、`stat`、`awk`、`grep`、`cmp`
和 `npm`，并在 worktree 创建 `.venv/bin/python`；Windows worktree 使用
`.venv/Scripts/python.exe`。发布脚本会拒绝缺少该 worktree Python 的环境，不会回退系统 Python。
发布 manifest 的 JSON 创建、读取和字段校验由 Python 标准库 helper 完成，因此不需要安装 `jq`。

systemd 发布必须使用 `/opt/watch-assistant/releases` 作为 release 根目录。可以先将压缩包
解压到同一文件系统内的隐藏 staging 目录，例如
`/opt/watch-assistant/releases/.watch-assistant-<hash7>.staging`；该目录在 staging 阶段可以是
`0700`。staging 目录不能被 `current` 或 systemd 服务引用，也不能直接作为最终 release 使用。

在同一文件系统内先将 staging 原子 rename 为未被使用的最终目录：

```bash
mv -T "$STAGING_ROOT" "$RELEASE_ROOT"
```

完成原子 rename 后，必须对最终 release 目录运行 prepare 门禁；最终目录的
顶层权限会被规范化为 `0755`，关键文件、VERSION、范围和符号链接逃逸也会一并校验：

```bash
RELEASES_ROOT=/opt/watch-assistant/releases
EXPECTED_RELEASE=<full-git-sha>
RELEASE_ROOT="$RELEASES_ROOT/watch-assistant-${EXPECTED_RELEASE:0:7}"
"$RELEASE_ROOT/scripts/systemd_release_prepare.py" \
  --release-root "$RELEASE_ROOT" \
  --expected-release "$EXPECTED_RELEASE" \
  --allowed-releases-root "$RELEASES_ROOT" \
  --service-user watch-assistant
```

prepare 成功后，发布脚本会记录旧 `current`、旧 release 元数据和新 manifest 摘要，再将 `current`
与 `release.env` 原子切换。prepare 失败时禁止切换，禁止手工递归 chmod、跳过校验或直接重启
服务。健康、release 精确匹配或部署诊断失败时，脚本会自动恢复旧 `current` 和 `release.env`，
执行 `daemon-reload` 后只重启 `watch-assistant.service`：

```bash
"$RELEASE_ROOT/scripts/deploy_systemd_release.sh" \
  "$RELEASE_ROOT" "$EXPECTED_RELEASE"
```

人工回退必须使用明确确认命令，且不会由应用 Web 进程执行：

```bash
WATCH_ASSISTANT_DIAGNOSTICS_TOKEN="<agent-token>" \
  "$RELEASE_ROOT/scripts/deploy_systemd_release.sh" rollback --confirm
```

prepare 工具只修改 release 顶层目录权限，不触碰 `data`、`backup`、`release.env`、
`/var/lib/watch-assistant` 或 `/etc` 下的文件。

## HTTPS

推荐使用 Tailscale Serve：

```bash
tailscale serve --bg http://127.0.0.1:8000
```

通过 Tailscale HTTPS 时将 `.env` 的 `COOKIE_SECURE` 设为 `true`。仅在可信局域网内使用 HTTP；HTTP 会让会话流量保持未加密。

## 备份

`scripts/backup_db.ps1` 使用 SQLite 在线备份 API 在 `/data/backups` 中创建一致快照，并只保留最近 7 份。可通过 Windows 任务计划或 cron 每天执行：

```powershell
pwsh ./scripts/backup_db.ps1
```

systemd 部署使用独立工具写入成对的 SQLite/manifest 文件，完成 SHA-256 和 `integrity_check`
后才发布备份；恢复默认只有预览，不会被发布脚本隐式执行：

```bash
/opt/watch-assistant/venv/bin/python /opt/watch-assistant/current/scripts/systemd_backup.py \
  create --database /var/lib/watch-assistant/watch-assistant.db \
  --output-dir /var/lib/watch-assistant/backups \
  --version-file /opt/watch-assistant/current/VERSION
/opt/watch-assistant/venv/bin/python /opt/watch-assistant/current/scripts/systemd_backup.py \
  restore-preview --manifest /var/lib/watch-assistant/backups/<backup>.json
```

## 本地验证

```bash
./.venv/bin/python -m pytest -q tests/unit
./.venv/bin/python -m pytest -q tests/contracts
./.venv/bin/python -m ruff check src tests scripts
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

## 验收记录

以下记录按日期保存，属于历史验收证据，不代表当前部署版本或线上开关状态。当前 Git 发布
基线和实际部署核对结果以 `PROGRESS.md` 为准。

2026-08-02（集成基线文档复核）：

- 本次复核锁定 `codex/integration-20260802` 的 `2c342d9` 作为审查快照；该集成分支不是发布基线，
  本次也未生成发布包、执行部署或进行真实 115/媒体服务器验收。
- 复核期间集成 ref 已继续前进到 `3afc8d3`，新增的 STRM manifest/cleanup 与 task worker lease
  提交当时不在本次文档分支中；随后随 PR #30 合入发布基线，但仍只表示 fail-closed readiness，
  不是生产部署或需求完成证据。
- 最近集成提交补强了可恢复扫描的目录范围、完整扫描和持久游标/分页门禁，以及 STRM dirty
  操作的租约互斥和恢复边界；对应回归覆盖见
  [扫描范围恢复测试](tests/integration/test_library_scan_scope_recovery.py)、
  [扫描操作测试](tests/unit/test_library_scan_operations.py) 和
  [STRM 操作测试](tests/unit/test_strm_operations.py)。这些改动只提高 fail-closed readiness，
  不改变 REQ-001/REQ-002 的“待验收”状态。

2026-07-25（后端分支）：

- Python：该历史记录对应旧版外部推送链；当前版本已移除旧链路，推送只保留独立 p115 gateway。
- Ruff：通过。
- Vitest：`8 passed`。
- Playwright：桌面与移动端 `2 passed`。
- Vite：成功生成 Web 资源和 `watch-assistant.user.js`。
- Compose：在 `192.168.6.236` 的 Docker Compose v5.1.2 上执行 `config --quiet` 通过。
- 浏览器：1440×900 与 390×844 均无横向溢出，控制台无错误。
- PanSou：只读搜索返回 `code=0`，确认 `data.merged_by_type` 含磁力结果。
- 115：当前版本的磁力/分享入口统一使用独立 p115 gateway；真实写入仍须通过对应契约验收和功能门禁。
- 镜像构建：服务器访问 `registry-1.docker.io:443` 超时，无法拉取 `node:24-alpine` 和 `python:3.12-slim`，因此没有部署镜像摘要。
