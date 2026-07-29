# Watch Assistant 开发指引

## 项目定位与现状

Watch Assistant 是轻量自托管影视资源管理系统，后端为 Python/FastAPI/
SQLAlchemy/SQLite，前端为 Vue 3/TypeScript/Vite。

- 已实现并以 `README.md` 为准：TMDB 发现、季度资料、PanSou 聚合搜索、磁力内容检测、
  质量/语义筛选、P115 Cookie readiness 与受限适配、任务状态机、SQLite、Web 会话、
  设置中心、中文界面和本地日志。
- TgtoDrive 没有经过验证的稳定提交/状态契约；真实推送必须保持禁用，返回
  `push_unsupported`，不得猜测接口、调用内部 `.pyc` 或修改其数据库。
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
- 主要部署定义：`docker-compose.yml`。外部 Docker 网络为 `pansou_default`
  （可由 `EXTERNAL_NETWORK` 覆盖）；PanSou、TgtoDrive、qBittorrent、115 均是外部依赖
  或适配器，不随普通应用操作重启。
- Compose 容器内部监听 `8000`，健康检查为
  `http://127.0.0.1:8000/api/v1/health`；宿主机端口由 `WATCH_ASSISTANT_PORT` 控制，
  `.env.example` 为 `8115`。数据卷为 `${DATA_DIR:-./data}` 挂载到容器 `/data`。
- systemd 备用部署见 `deploy/watch-assistant.service`：工作目录
  `/opt/watch-assistant/current`，监听 `8115`，状态目录 `/var/lib/watch-assistant`，
  环境文件 `/etc/watch-assistant.env`。服务器实际 Docker/Compose 是否在运行、实际
  `DATA_DIR` 和反向代理/Tailscale 暴露方式均须在发布前核对；不能猜测。
- `P115_ENABLED`、目标目录和能力开关均需显式配置；TgtoDrive 契约未验证时禁止真实推送。

常用命令（不包含凭据）：

```bash
# Docker Compose：检查、启动、停止、日志、健康和备份
docker compose config
docker compose up -d --build
docker compose stop watch-assistant
docker compose logs -f --tail=200 watch-assistant
# `.env.example` 使用 8115；实际端口以部署 `.env` 为准
curl -fsS http://127.0.0.1:8115/api/v1/health
pwsh ./scripts/backup_db.ps1

# 服务器 systemd：检查、启动、停止、日志和健康
ssh root@192.168.6.236 'systemctl status watch-assistant.service --no-pager'
ssh root@192.168.6.236 'systemctl start watch-assistant.service'
ssh root@192.168.6.236 'systemctl stop watch-assistant.service'
ssh root@192.168.6.236 'journalctl -u watch-assistant.service -n 200 --no-pager'
ssh root@192.168.6.236 'curl -fsS http://127.0.0.1:8115/api/v1/health'
```

备份前确认实际运行模式：Compose 使用 `scripts/backup_db.ps1`（容器 `/data/backups`，
保留 7 份）；systemd SQLite 的备份路径和运行命令待确认，不能套用 Compose 脚本。

## 强制安全规则

- 不猜测第三方 API；不调用内部 `.pyc`；不直接修改 TgtoDrive 数据库。
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

## 工程纪律（2026-07-29 健康检查补充）

以下规则由项目健康度审计导出，违反即导致分支失控和发布循环失效：

1. **一功能一分支**：72h 内合并基线或删除；返工 rebase 最新基线，禁止开 clean2/current2/squashed2 变体。
2. **未合并 = 未完成**：任务完成的标志是合入基线，不是"代码写完了"。合并后立即删枝。
3. **提交先于发布**：禁止从未提交改动打 tar.gz；包名必须含 commit hash（`watch-assistant-<hash7>-<date>.tar.gz`）。
4. **发布基线唯一**：当前 `codex/publish-main`；禁止两个 publish 分支并存，分叉时立即裁决并线。
5. **凭据不进仓**：secrets 只放 `C:\Users\98275\.115ts-secrets\`；.gitignore 已覆盖 `*cookie*`、`authorization*.json`、`p115-prod-*.json`、`*.tar.gz` 等。
6. **Python 环境**：worktree 内用 `.venv/Scripts/python.exe`，禁止系统 python（74 import error）、禁止 `uv sync`（删依赖）、禁止 PowerShell。全量 956 tests 分三批跑（unit/integration/contracts），单条全量超 120s 必超时。
7. **验收脚本化**：合并前 `scripts/verify.sh` 全绿，不靠截图。离线测试 = 合并门禁，live 测试 = 每日单独跑。

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
python -m pytest -q
python -m ruff check src tests scripts
npm --prefix frontend test -- --run
npm --prefix frontend run build
npm --prefix frontend run test:e2e
docker compose config
```

按改动风险补充对应 API/迁移/任务状态机测试、Playwright 桌面和移动回归。真实 115、
媒体服务器或写操作验收必须使用专用低风险夹具、功能开关和回滚记录。

## 本地验收环境事实

- 部署主机：`192.168.6.236`。
- 持久 iPad Cookie：`C:\Users\98275\.115ts-secrets\.p115-cookie`。
- 测试目录 CID：`3482085898508567892`，干净且只有一个 wav 文件。
- 固定 `p115client` 版本：`0.0.9.6.5.1`。
- `_p115client_timeout_executor` 对 `errno=990009` 使用 3 秒重试。
- C03 live 文件分页大小必须保持 `VERIFIED_FS_FILES_PAGE_SIZE=1`；fixture probe 必须先
receipt 再 verify；live runner 保持 990009 重试。

## 工程纪律

- 一功能一分支：72 小时内合并基线或删除；返工必须基于最新基线，禁止重复的 clean/current/squashed 变体。
- 未合并即未完成；提交先于发布，发布包名必须包含 commit hash。
- 发布基线只允许 `codex/publish-main`；凭据只放在 `C:\Users\98275\.115ts-secrets\`。
- 合并前运行 `scripts/verify.sh`；离线测试是门禁，live 测试单独每日运行。

唯一生产发布基线 = `codex/publish-main` @ `a2bdfc8`，日期 2026-07-29；后续 `8bfa5e7` 仅更新发布说明。
