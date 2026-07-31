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

## 115 影视库整理进度

当前发布版本为 `4e46e85`，已部署到 `192.168.6.236:8115`。只读索引、组织计划/执行、
真实移动/重命名、永久删除和受管 STRM 播放入口已接入；整理移动/重命名和永久删除使用
固定测试 CID 完成真实闭环，播放直链完成 `HEAD`、单段 `Range GET` 验收。

STRM 全量、增量和清理代码已部署并开启。受控视频夹具已完成完整扫描后全量生成、重命名
后的增量对账、删除后的失效清理，且 `.strm` 内容只包含稳定播放入口。TgtoDrive 仍保持禁用。

TgtoDrive 当前没有验证通过的稳定提交/状态 HTTP 契约，`config/tgto-contract.json` 明确为 `supported:false`。因此生产部署会禁用真实推送并返回 `503 push_unsupported`，不会猜测接口、写 TgtoDrive 数据库或调用内部 `.pyc`。

## 部署

本地开发工作区为 macOS 桌面目录 `/Users/apple/Desktop/115ts`。以下部署主机、容器和
systemd 路径是远程运行环境，不是本地仓库路径；不要把它们替换成桌面路径，也不要把
本地工作区描述成已部署版本。

要求：Docker Engine、Docker Compose，以及已存在的外部网络 `pansou_default`。服务器已确认 `pansou-app` 与 `TgtoDrive` 都连接到该网络。

1. 从示例创建 `.env`，并建立 `secrets` 目录。
2. 生成四个只读 Secret 文件：

```bash
mkdir -p secrets
./.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > secrets/encryption_key
printf '%s' '你的 TMDB API Key' > secrets/tmdb_api_key
./.venv/bin/python -c "from pwdlib import PasswordHash; print(PasswordHash.recommended().hash(input('Web password: ')), end='')" > secrets/web_password_hash
./.venv/bin/python -c "from pwdlib import PasswordHash; print(PasswordHash.recommended().hash(input('Userscript token: ')), end='')" > secrets/script_token_hash
```

3. 检查并启动：

```bash
docker compose config
docker compose up -d --build
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
应用也不会启动真实 worker 或删除入口。当前生产实际状态以 `GET /api/v1/health` 为准：
整理计划/执行、移动/重命名、永久删除、STRM 全量/增量/清理、播放及播放契约均已开启。
STRM 增量接口使用完整扫描差异，失效清理会校验受管文件内容。

使用 systemd 部署时，`watch-assistant.service` 可独立重启。qBittorrent sidecar 的升级或重启必须作为独立维护操作执行；不得通过重启应用隐式管理 qBittorrent 的生命周期。

## HTTPS

推荐使用 Tailscale Serve：

```bash
tailscale serve --bg http://127.0.0.1:8000
```

通过 Tailscale HTTPS 时将 `.env` 的 `COOKIE_SECURE` 设为 `true`。仅在可信局域网内使用 HTTP；HTTP 会让会话流量保持未加密。

## 备份

`scripts/backup_db.ps1` 使用 SQLite 在线备份 API 在 `/data/backups` 中创建一致快照，并只保留最近 7 份。可通过 Windows 任务计划或 cron 每天执行：

当前 macOS 工作区未安装 `pwsh`；该命令仅适用于已安装 PowerShell 7 的维护机，不代表
当前本地环境已经可以执行备份。

```bash
pwsh ./scripts/backup_db.ps1
```

## 本地验证

当前工作区尚未提交 `.venv`。准备好当前工作区的虚拟环境后，在
`/Users/apple/Desktop/115ts` 执行：

```bash
cd /Users/apple/Desktop/115ts
./.venv/bin/python -m pytest -q
./.venv/bin/python -m ruff check src tests scripts
pnpm --dir frontend test -- --run
pnpm --dir frontend run build
```

## 验收记录

2026-07-25（后端分支）：

- Python：`141 passed, 1 skipped`；跳过项是明确的 TgtoDrive `supported:false` 实际契约测试。
- Ruff：通过。
- Vitest：`8 passed`。
- Playwright：桌面与移动端 `2 passed`。
- Vite：成功生成 Web 资源和 `watch-assistant.user.js`。
- Compose：在 `192.168.6.236` 的 Docker Compose v5.1.2 上执行 `config --quiet` 通过。
- 浏览器：1440×900 与 390×844 均无横向溢出，控制台无错误。
- PanSou：只读搜索返回 `code=0`，确认 `data.merged_by_type` 含磁力结果。
- TgtoDrive：没有稳定 submit/status 契约，未提交测试磁力或 115 分享，真实推送保持禁用。
- 镜像构建：服务器访问 `registry-1.docker.io:443` 超时，无法拉取 `node:24-alpine` 和 `python:3.12-slim`，因此没有部署镜像摘要。
