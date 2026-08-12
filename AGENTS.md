# Watch Assistant 开发指引

## 项目定位与现状

Watch Assistant 是轻量自托管影视资源管理系统，后端为 Python/FastAPI/
SQLAlchemy/SQLite，前端为 Vue 3/TypeScript/Vite。

- 已实现并以 `README.md` 为准：TMDB 发现、季度资料、PanSou 聚合搜索、磁力内容检测、
  质量/语义筛选、P115 Cookie readiness 与受限适配、任务状态机、SQLite、Web 会话、
  设置中心、中文界面和本地日志。
- 磁力和 115 分享任务只能通过独立的 p115 gateway；分享能力和所有 115 写入均须按
  独立契约、功能开关、计划/确认、幂等和审计门禁执行，门禁未满足时必须 fail-closed。
- 规划但尚未实现或尚未验收：115 影视库自动整理、STRM 全量/增量同步、订阅追更、
  库存去重、字幕、Agent CLI/MCP、PWA 等。以 `docs/requirements/` 的状态为准，
  不得描述为已上线。

## 核心产品主线

- 资源发现：TMDB -> PanSou/多来源搜索 -> 内容检测 -> 质量筛选。
- 115 入库：推送/转存 -> 状态确认 -> 自动整理 -> 库存更新。
- 媒体输出：整理完成 -> 增量检测 -> STRM 更新 -> 元数据同步 -> 失效清理。
- 管理闭环：任务中心 -> 中文日志 -> 通知 -> CLI/MCP/PWA。

后 3 条中的整理、库存、STRM、通知、CLI/MCP/PWA 主要属于规划需求，实施前先核对
对应 REQ 的状态和依赖。

## 目录与需求管理

- 正式需求总索引：`docs/requirements/README.md`。
- 独立需求目录：`docs/requirements/REQ-NNN-short-name/README.md`；一个需求一个目录。
- 实施计划：`docs/project-plans/`；技术契约和验收证据可放在需求目录的 `contracts/`、
  `evidence/`。
- 需求状态、优先级、依赖只以总索引为准。`REQ-013 Emby/Jellyfin/Plex 联动`暂不纳入，
  编号保留，禁止复用或创建同编号目录。
- 115 整理和 STRM 的分期、安全门禁与任务依赖见
  `docs/project-plans/2026-07-26-115-library-organization-strm-implementation-plan.md`。

## 部署环境

- 部署主机：`192.168.6.236`。不得把密码、Cookie、Token、API Key 或 Secret 写入代码、
  文档、日志、命令输出或测试 fixture。
- **实测运行模式（2026-08 核对）**：服务器以 **systemd** 运行（非 Compose）；
  `/etc/systemd/system/watch-assistant.service` 关键配置：
  - `ExecStart=/opt/watch-assistant/venv/bin/uvicorn watch_assistant.app:app --host 0.0.0.0 --port 8115 --workers 1`
  - `WorkingDirectory=/opt/watch-assistant/current`
  - `Environment=PYTHONPATH=/opt/watch-assistant/current/src`（代码总从 current/src 加载）
  - `Environment=FRONTEND_DIST_DIR=/opt/watch-assistant/current/frontend/dist`
  - `EnvironmentFile=/etc/watch-assistant.env` 与 `-/var/lib/watch-assistant/release.env`
- 发布目录布局：`/opt/watch-assistant/current` → 符号链接 →
  `releases/watch-assistant-<hash7>`（发布包名 `watch-assistant-<hash7>-<date>.tar.gz`，
  包内顶层目录同名，解压到 `releases/` 即可）；上传包先放 `incoming/`。
  `release.env` 记录 `WATCH_ASSISTANT_RELEASE=<full-sha>`，与包内 `VERSION` 的
  `commit=` 必须一致，否则 `postdeploy_release_check` 报 `release_env_mismatch`。
- 部署后校验：
  `ssh root@192.168.6.236 '/opt/watch-assistant/venv/bin/python /opt/watch-assistant/current/scripts/postdeploy_release_check.py --version-file /opt/watch-assistant/current/VERSION --health-url http://127.0.0.1:8115/api/v1/health'`
  输出 `POSTDEPLOY_RELEASE_CHECK=ok` 才算通过。
- 其他实测事实：健康检查 `http://127.0.0.1:8115/api/v1/health` 返回 `release` 字段；
  `PROWLARR_SLOW_INDEXER_IDS=10`（18 已禁用，勿加回）；服务器 venv 内已卸载
  `watch-assistant` 安装副本（依赖 PYTHONPATH，勿再 `pip install -e .` 进 venv）；
  macOS 打包的 tar 在服务器解压会报 `LIBARCHIVE.xattr.com.apple.provenance`
  警告，用 `tar --warning=no-unknown-keyword -xzf` 忽略。
- Compose 定义（`docker-compose.yml`）仍保留供容器化部署：内部监听 `8000`，
  外部网络 `pansou_default`，数据卷 `${DATA_DIR:-./data}` 挂载 `/data`；
  仅当服务器实际切回 Compose 时才适用。
- `P115_ENABLED`、目标目录和能力开关均需显式配置；p115 写入契约未验证时禁止真实写入。

## 本地开发环境（macOS 开发机）

- Python 虚拟环境在项目根 `.venv/`（`/Users/apple/Watch-Assistant/.venv/bin/python`）；
  测试/ruff 一律用它，不用系统 Python。
- 常用命令：
  ```bash
  .venv/bin/python -m pytest tests/unit tests/integration -q          # 单测+集成
  .venv/bin/python -m pytest tests/contracts -q                       # 契约测试
  .venv/bin/python -m ruff check src tests                            # lint
  npm --prefix frontend test -- --run                                  # vitest
  npm --prefix frontend run build                                      # 前端构建
  ```
- 真实部署验收（Playwright，直连 192.168.6.236:8115，不启本地服务）：
  ```bash
  WA_E2E_USER=<账号> WA_E2E_PASSWORD=<密码> npx playwright test -c playwright.deploy.config.ts
  ```
  凭据只从环境变量注入，严禁写入源码/报告/截图；`e2e/live/**` 用例默认配置不运行。
- 测试凭据可放 `$CLAUDE_JOB_DIR/tmp/wa-test.env`（`export WA_E2E_USER=...` 等，
  运行时 `source` 注入），该目录随任务清理、不入库。

常用命令（不包含凭据）：

```bash
# 服务器 systemd：检查、启动、停止、日志和健康（当前实际运行模式）
ssh root@192.168.6.236 'systemctl status watch-assistant.service --no-pager'
ssh root@192.168.6.236 'systemctl start watch-assistant.service'
ssh root@192.168.6.236 'systemctl stop watch-assistant.service'
ssh root@192.168.6.236 'journalctl -u watch-assistant.service -n 200 --no-pager'
ssh root@192.168.6.236 'curl -fsS http://127.0.0.1:8115/api/v1/health'

# 发布流程（构建 → 上传 → 切换 → 重启 → 校验）
# 1) 在 codex/publish-main 上、工作树干净、远程已同步,以完整 SHA 构建
bash scripts/build_release.sh "$(git rev-parse HEAD)"
# 2) 上传并解压（包内顶层目录名与包名同名）:
scp -q release-archive/<date>/watch-assistant-<hash7>-<date>.tar.gz \
  root@192.168.6.236:/opt/watch-assistant/incoming/
ssh root@192.168.6.236 'tar --warning=no-unknown-keyword -xzf \
  /opt/watch-assistant/incoming/watch-assistant-<hash7>-<date>.tar.gz \
  -C /opt/watch-assistant/releases/'
# 3) 切换符号链接、更新 release.env、重启、校验:
ssh root@192.168.6.236 'ln -sfn /opt/watch-assistant/releases/watch-assistant-<hash7> \
  /opt/watch-assistant/current && \
  printf "WATCH_ASSISTANT_RELEASE=<full-sha>\n" > /var/lib/watch-assistant/release.env && \
  systemctl restart watch-assistant.service'
# 4) postdeploy 校验见上文命令,输出 POSTDEPLOY_RELEASE_CHECK=ok
# 5) 部署后验收:cd frontend && npx playwright test -c playwright.deploy.config.ts

# Compose（备用部署定义,当前服务器未使用）
docker compose config
docker compose up -d --build
pwsh ./scripts/backup_db.ps1
```

备份前确认实际运行模式：Compose 使用 `scripts/backup_db.ps1`（容器 `/data/backups`，
保留 7 份）；systemd SQLite 使用 `scripts/systemd_backup.py`（详见 `README.md` 的
systemd 部署章节）：
`/opt/watch-assistant/venv/bin/python /opt/watch-assistant/current/scripts/systemd_backup.py create \
  --database /var/lib/watch-assistant/watch-assistant.db \
  --output-dir /var/lib/watch-assistant/backups \
  --version-file /opt/watch-assistant/current/VERSION`
恢复默认只预览（`restore-preview`），不会被发布脚本隐式执行。

## 强制安全规则

- 不猜测第三方 API，不调用内部 `.pyc`，不直接修改第三方数据库。
- 115 移动、重命名、隔离、洗版、清理和删除必须先完成契约验证、计划预览、人工确认、
  幂等和审计。`uncertain` 必须先只读核对远端，禁止直接重复提交。
- 永久删除默认关闭，优先隔离和可恢复操作；清理只可处理系统受管清单，扫描不完整时禁止
  删除；不得修改配置范围以外的 115 目录。
- 禁止在代码、日志、文档、响应、截图和测试输出中暴露 Cookie、Token、磁力、分享密码、
  pickcode、完整播放 token 或真实直链。
- STRM 必须使用稳定 Watch Assistant 播放入口和非敏感标识，不能嵌入 Cookie、完整 token
  或时效直链。

## 开发规范

- 先读相关 REQ、实施计划和现有实现；仅做最小范围修改。保留用户已有改动，不执行
  `reset --hard`、强制 checkout 或其他破坏性 Git 操作。
- 业务 API、Web、CLI、MCP 共用权限、幂等和状态机规则；新增功能要补日志事件、测试和
  中文错误映射。
- 用户可见的提示、错误、状态和日志用中文；稳定机器码可用英文。
- 115 新能力采用独立 gateway、索引/计划/执行状态和 feature flag；不要把现有磁力推送
  `Task` 表或适配器扩展成通用文件系统。
- SQLite 迁移必须向前、幂等、可由发布前备份回滚；前端列表采用分页/游标，不一次返回整库。

## 工程纪律与发布治理

以下规则由项目健康度审计导出，违反即导致分支失控和发布循环失效：

1. **一功能一分支**：72 小时内合并基线或删除；返工必须基于最新基线，禁止创建
   `clean2`、`current2`、`squashed2` 等变体。
2. **未合并 = 未完成**：任务完成的标志是合入基线，不是代码写完；合并后立即删枝。
3. **提交先于发布**：禁止从未提交改动打包；发布包名必须包含
   `watch-assistant-<hash7>-<date>.tar.gz`。
4. **发布基线唯一**：唯一发布分支是 `codex/publish-main`。发布前必须核对该分支的
   实际最新提交和部署状态，不在本文件硬编码可能过期的提交号。
5. **凭据不进仓**：Secret 只放在受保护的位置（服务器 `/etc/watch-assistant/` 环境文件与
   `p115-cookie`；测试凭据 `$CLAUDE_JOB_DIR/tmp/wa-test.env`）；不得进入代码、
   文档、日志、测试 fixture、截图或发布包。
6. **Python 环境**：worktree 内使用 `.venv/bin/python`，禁止使用系统 Python、
   `uv sync` 或 PowerShell 修改环境；全量测试分 unit/integration/contracts 三批运行，
   单条全量超过 120 秒按超时处理。
7. **验收脚本化**：合并前 `scripts/verify.sh` 必须通过，不以截图代替验收；离线测试是
   合并门禁，live 测试单独按计划运行。

## 多会话协作

- 项目管理 Agent 负责需求拆解、任务派发、依赖和风险门禁、集成顺序、验收与发布决策；
  应直接向合适的 Codex 会话派发任务，不要求用户手动转发。
- 执行会话负责限定范围内的实现或验证，完成后必须回报提交/文件、测试证据、实际调用范围、
  未验证项和剩余风险；不得自行扩大需求范围或宣称未验收能力已上线。
- 项目管理 Agent 收到回报后负责复核、决定下一步或重新派发。涉及真实 115 写操作、删除、
  部署、密钥或外部副作用时，仍须遵守本文件的安全门禁和用户授权边界。
- 会话不会自动跨线程推送最终消息；项目管理 Agent 每次派发后必须主动使用线程状态/等待
  接口跟踪任务，读取完成报告并汇总给用户，不得要求用户手动转发执行结果。

## 验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests scripts
npm --prefix frontend test -- --run
npm --prefix frontend run build
npm --prefix frontend run test:e2e
docker compose config
```

按改动风险补充对应 API/迁移/任务状态机测试、Playwright 桌面和移动回归。真实 115、
媒体服务器或写操作验收必须使用专用低风险夹具、功能开关和回滚记录。

## 本地验收环境事实

- 部署主机：`192.168.6.236`。
- 持久 iPad Cookie：服务器 `/etc/watch-assistant/p115-cookie`（config.py 默认值）。
- 测试目录 CID：`3482085898508567892`，干净且只有一个 wav 文件。
- 固定 `p115client` 版本：`0.0.9.6.5.1`。
- `_p115client_timeout_executor` 对 `errno=990009` 使用 3 秒重试。
- C03 live 文件分页大小必须保持 `VERIFIED_FS_FILES_PAGE_SIZE=1`；fixture probe 必须先
receipt 再 verify；live runner 保持 990009 重试。
