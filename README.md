# Watch Assistant

轻量自托管的个人观影辅助工具。它展示 TMDB 热门电影并支持片名搜索，进入详情后聚合 PanSou 的磁力与 115 分享结果，并把敏感链接加密保存在本机 SQLite 中。

## 当前能力

- 独立 Vue Web 工作台与 TMDB 用户脚本。
- 首页展示 TMDB 当前热门电影，支持按片名搜索并点击进入资源聚合详情。
- PanSou 双查询、去重、30 分钟新鲜缓存和 24 小时故障回退。
- Web 密码会话、用户脚本独立 Bearer Token、CSRF 与限流。
- SQLite WAL、Fernet 敏感字段加密和保留期清理。
- 持久任务状态机与崩溃后的 `uncertain` 防重复提交。

TgtoDrive 当前没有验证通过的稳定提交/状态 HTTP 契约，`config/tgto-contract.json` 明确为 `supported:false`。因此生产部署会禁用真实推送并返回 `503 push_unsupported`，不会猜测接口、写 TgtoDrive 数据库或调用内部 `.pyc`。

## 部署

要求：Docker Engine、Docker Compose，以及已存在的外部网络 `pansou_default`。服务器已确认 `pansou-app` 与 `TgtoDrive` 都连接到该网络。

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
docker compose config
docker compose up -d --build
curl http://127.0.0.1:8000/api/v1/health
```

访问 `http://服务器地址:8115/`。构建后的用户脚本位于容器内 Web 根目录，可从 `http://服务器地址:8115/watch-assistant.user.js` 获取。

服务器的 `8000` 端口已被占用时，可按 `.env.example` 使用 `8115`。若 Docker Hub 不可达，可以使用 `deploy/watch-assistant.service` 以 Python venv 运行；对应环境变量模板是 `deploy/watch-assistant.env.example`。

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

## 本地验证

```powershell
python -m pytest -q
python -m ruff check src tests scripts
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

## 验收记录

2026-07-24：

- Python：`74 passed, 1 skipped`；跳过项是明确的 TgtoDrive `supported:false` 实际契约测试。
- Ruff：通过。
- Vitest：`6 passed`。
- Playwright：桌面与移动端 `2 passed`。
- Vite：成功生成 Web 资源和 `watch-assistant.user.js`。
- Compose：在 `192.168.6.236` 的 Docker Compose v5.1.2 上执行 `config --quiet` 通过。
- 浏览器：1440×900 与 390×844 均无横向溢出，控制台无错误。
- PanSou：只读搜索返回 `code=0`，确认 `data.merged_by_type` 含磁力结果。
- TgtoDrive：没有稳定 submit/status 契约，未提交测试磁力或 115 分享，真实推送保持禁用。
- 镜像构建：服务器访问 `registry-1.docker.io:443` 超时，无法拉取 `node:24-alpine` 和 `python:3.12-slim`，因此没有部署镜像摘要。
