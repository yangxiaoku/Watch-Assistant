# P115 Library Contract Notes

本文件对应 Phase 0-A 的 C01/C02 契约。固定依赖为
`p115client==0.0.9.6.5.1`；除下文明确记录的有限 C02 只读样本外，所有真实能力均为
`unverified`，不能作为生产能力声明。

## 本地冻结的业务边界

`P115LibraryGateway` 只暴露三个读方法：目录分页、文件详情、目录详情。
第三方响应只能通过 `parse_library_entry` / `parse_directory_page` 转换为
`LibraryEntry` 和 `DirectoryPage`，原始 mapping 不穿透到业务层。

- `file_id` / `directory_id` 是候选稳定身份；`path` 仅展示用途。
- `pickcode` 可为空，仅作为播放属性候选，不作为移动或复制后的稳定主键。
- `DirectoryPage` 的 `scan_complete=None` 表示上游没有声明整次扫描完成；扫描器只接受已验证
  `page_count` 末页或显式 `has_more`/`next_page`/`terminal` 作为终止依据。
- 多个分页信号必须彼此一致；矛盾信号一律返回 `partial/pagination_unverified`。
- 当前 `page`/`next_page` 是连续页号，`next_page` 必须等于当前页加一，不是 opaque cursor；
  `pages_read` 表示实际取得并处理的页数。
- `DirectoryPage` 和 `ScanResult` 同时表达 `complete`、`partial`、`cancelled`。
- 扫描发现异常、重复页、空页、页数或 total 漂移时返回 `partial`；取消继续传播。
- `scan_directory` 当前仅用于有界离线探针。Phase 1 生产索引器必须逐页持久化，不能把整库加载到内存。
- DTO 的 `repr`、fake 调用记录和错误码不包含名称、完整路径、pickcode 或异常正文。

## I02/I03 离线索引边界

`LibraryIndexService` 只接受已启用、`scope_verified` 且 root ID 精确匹配的本地
`MediaLibrary`。它通过注入的只读 `P115LibraryGateway` 逐页读取并在每页事务中保存
checkpoint；取消、分页异常、范围外条目和远端失败都会保留已提交进度并标记
`complete=false`。同一幂等键恢复同一 scan run；不同幂等键对相同完整快照只产生观察，
稳定 file/directory ID 用作身份，路径变化只记录为变更属性。

该索引没有删除候选字段的生成能力，也没有写 gateway、清理、API、worker 或 feature
flag 接线。`complete=false` 的结果以及所有结果的 `deletion_candidates` 均为空；本地
fake 是本阶段唯一可执行 transport，真实服务器、Cookie、P115Client 和外部调用仍为 0。

## Phase 2 整理 transport 离线边界

`adapters/p115_organization_transport.py` 仅提供注入
`OrganizationExecutorTransport` 的内存 fake。构造时必须冻结受管目录 allowlist，以及
每个计划成员的稳定对象 ID、源/目标父目录和源/目标名称；未确认 scope、范围外调用、
远端状态漂移和未知结果均 fail-closed。`move`、`rename` 不重试，timeout 与取消继续传播，
公开 `repr` 和调用记录只含操作名与计数。

工厂默认 `live=false`，且显式拒绝 `live=true`。该模块没有 Cookie、`P115Client`、网络、
delete、quarantine、API、worker 或 feature flag 接线；真实整理 transport 仍未实现。

## p115client 候选能力矩阵

| 能力 | 固定版本候选方法/签名 | 当前状态 | 需要的输入 | 预期输出 | 副作用 | 下一步探针 | 未知点 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| list directory | `P115Client.fs_files(payload=0, /, base_url='https://webapi.115.com', *, async_=False, **request_kwargs)` | `verified_read_only`，仅一个受管小目录的单页样本 | `cid`、`limit`、`offset`，可选 `show_dir` | 列表、页游标/总数候选 | 可能记录打开时间（库默认 `record_open_time=1`） | 小目录连续分页复验 | 大目录、最大安全页大小与快照语义 |
| file detail | `P115Client.fs_info(payload, /, base_url='https://webapi.115.com', *, async_=False, **request_kwargs)` | `verified_read_only`，仅一个已观察文件详情样本 | `fid` | 文件详情候选：类型、名称、大小、mtime、pickcode | 只读；目录查询可能计算统计 | 目录详情与时间语义脱敏验证 | 目录详情、时间精度和 pickcode 稳定性 |
| directory detail | `P115Client.fs_info(payload, /, base_url='https://webapi.115.com', *, async_=False, **request_kwargs)` | unverified；DTO 为 offline_only | `file_id` 或 path | 目录详情和统计候选 | 只读；可能触发目录统计计算 | 小目录详情读取，确认统计是否稳定 | 大目录耗时、统计字段是否分页一致 |
| pickcode | `P115Client.fs_info` 响应候选字段；`P115Client.download_url(pickcode, strict=True, user_agent=None, app='os_windows', *, async_=False, **request_kwargs)` 为播放候选 | unverified | 文件 ID/pickcode | 可空 pickcode；直链候选 | 直链可能有时效 | 不输出 URL 的字段类型探针 | 移动/重命名/复制后的稳定性和过期语义 |
| move | `P115Client.fs_move(payload, /, pid=0, base_url='https://webapi.115.com', *, async_=False, **request_kwargs)` | unverified，禁止调用 | 文件/目录 ID、目标父目录 | 未冻结 | 写入云端目录 | C03 专用临时夹具 | 超时幂等、部分成功、ID/pickcode 变化 |
| rename | `P115Client.fs_rename(payload, /, base_url='https://webapi.115.com', *, async_=False, **request_kwargs)` | unverified，禁止调用 | ID 与新名称 | 未冻结 | 修改名称 | C03 专用临时夹具 | 冲突、扩展名规则、超时核对 |
| mkdir | `P115Client.fs_mkdir(payload, /, pid=0, base_url='https://webapi.115.com', *, async_=False, **request_kwargs)` | unverified，禁止调用 | 名称、父目录 ID | 未冻结 | 新目录 ID 候选 | 创建目录 | C03 专用临时夹具 | 重复名称和并发语义 |
| quarantine/restore | 未找到固定版本明确的高层专用方法 | unverified，禁止调用 | 待冻结 | 未冻结 | 预计移动/改名组合写入 | 先冻结 C03 移动契约 | 是否存在官方隔离 API、回滚边界 |
| recycle/delete | `P115Client.fs_delete(payload, /, base_url='https://webapi.115.com', *, async_=False, **request_kwargs)` | unverified，禁止调用 | 文件/目录 ID | 未冻结 | 删除或进入回收站语义未确认 | C03 只使用可丢弃夹具 | 回收站与永久删除 flag、精确清理 |
| direct link | `P115Client.download_url(pickcode, strict=True, user_agent=None, app='os_windows', *, async_=False, **request_kwargs)` | unverified，禁止调用 | pickcode | 临时 URL 候选 | 可能产生签名 URL | C05 脱敏探针 | URL TTL、Cookie 失效、错误字段 |
| HEAD/GET/Range | p115client 未冻结媒体服务器代理方法；`download_url` 只提供 URL 候选 | unverified，禁止调用 | 直链、请求方法和 Range | HTTP 状态/头部候选 | 可能消耗直链配额 | C05 使用本地 HTTP 夹具和真实直链脱敏验证 | 302/307、HEAD、Range、并发和缓存语义 |

## C03 写契约离线预备

`adapters/p115_library_write_contract.py` 是独立的离线边界，不扩展只读的
`P115LibraryGateway`，也不导入或创建 `P115Client`。它只冻结固定版源码的候选
请求形态：

| 操作 | 固定版候选方法 | 脱敏 payload 形态 | 当前状态 |
| --- | --- | --- | --- |
| mkdir | `fs_mkdir(payload, pid=0)` | `file_name`、`pid` | unverified；offline_only |
| move | `fs_move(payload, pid=0)` | `file_ids`、`to_cid` | unverified；offline_only |
| rename | `fs_rename(payload)` | `file_id`、`file_name` | unverified；offline_only |
| quarantine | move + rename 组合 | 两个独立步骤 | unverified；offline_only |
| restore | move + rename 组合 | 两个独立步骤 | unverified；offline_only |
| recycle | `fs_delete(payload)`（open API 候选） | `file_id` | unverified；offline_only |
| delete | `fs_delete(payload)`（web API 候选） | `file_id` | disabled；永久删除关闭 |

所有写操作必须同时满足 `write_enabled`、用户明确批准、专用可丢弃 fixture
范围和可精确执行的清理计划；默认全部为 false。永久删除另有独立门禁，当前
始终关闭。离线 fake 只记录操作名和 payload 字段名，不保留任何 ID、名称、路径、
Cookie 或异常正文。

结果状态固定为 `success`、`failed`、`uncertain`：远端明确失败映射为
`failed/remote_failed`，超时映射为 `uncertain/timeout` 且必须进行后置核对，
`CancelledError` 继续传播，不自动重试。`WritePostcondition` 只描述待核对的
身份、父目录、名称和存在性，核对结果为 `satisfied`、`not_satisfied` 或
`unverified`，不承诺任何真实字段值。

超出已记录受管夹具的 C03 验收仍需要用户批准的专用非生产临时目录、可验证的前后置条件和精确
清理清单，必须按“建目录 -> 上传/复制 fixture -> 重命名 -> 移动 -> 隔离 ->
恢复 -> 只读核对 -> 精确清理”顺序单独授权。任何 timeout/uncertain 只允许
只读核对并停止后续写入；不得使用正式影视文件。永久删除、业务 worker、API、
前端和 capability 本轮均未接入。

### C03 本地生命周期探针

`src/watch_assistant/adapters/p115_c03_fixture_probe.py` 与
`scripts/p115_c03_fixture_probe.py` 只提供注入式边界和 offline fake；模块不读取
Cookie、不创建 `P115Client`、不发网络。探针只接受非零十进制 `parent_id`，拒绝 URL、
路径和任意目录输入。每轮固定执行 4 次 `mkdir`、5 次隔离/恢复组合操作及 1 次
`fs_delete` 回收，最多 10 次写调用、60 次只读调用（其中列表观察最多 15 次、每次最多 4 页）；冲突、批量观测本轮未覆盖且预算为 0。

运行前必须同时设置：
`WATCH_ASSISTANT_P115_C03_WRITE=1`、
`WATCH_ASSISTANT_P115_C03_MANAGED_FIXTURE=1`、
`WATCH_ASSISTANT_P115_C03_CLEANUP_PLAN=1`。注入边界的 `live=True` 还必须设置
`WATCH_ASSISTANT_P115_C03_LIVE=1`；`p115_c03_fixture_probe.py` 仅运行 offline fake，
live transport 仅由下述独立 runner 提供。
永久删除门禁没有入口，回收只使用已冻结的 `fs_delete(fid)` 候选形态。

根名由随机 nonce 生成，公开报告只保留短指纹、阶段、调用计数和固定错误码，不输出
ID、名称、路径、Cookie、pickcode、URL 或第三方异常正文。每次写入后都按稳定
`file_id + parent_id + name` 做只读核对；任一失败、超时、取消或无法确认均报告
`uncertain`，不自动重试。只有再次精确核对本轮创建的受管根后，才允许一次回收，
并在回收前逐层列举 root/source/quarantine/artifact，确认目录完整且子项集合只含
本轮受管对象；任何 uncertain、外来/缺失对象、分页不完整或列举失败都不回收。
回收后再只读确认根不存在；回收调用一旦发出，若回收失败、取消或之后的清单核对失败，
公开 `cleanup` 必须是 `uncertain`，不能误报为 `not_attempted_uncertain`，且不重试。
该探针仍是 fixture-only 证据，不打开业务 capability。

2026-07-27 使用四项显式 gate 的一次性 live runner 在受管夹具上复验成功：10 次
写操作均获确认，15 次列表观察共读取 31 页，临时根回收后确认不存在。真实响应中
`fs_mkdir` 的成功 `errno` 是空字符串，且目录 `fs_info` 不回显稳定 ID 或父 ID；
transport 仅接受前者这一已验证成功形态，并改用 `fs_files` 的稳定 ID、父 ID、名称和
目录标记核对写后状态。该证据仍只适用于受管夹具，业务写能力、STRM、洗版和永久删除
继续关闭。

2026-07-28 修复文件与目录的身份、父目录字段映射后，使用同一受管夹具重新完成一次
live 验收：10 次写操作均获确认，15 次列表观察共读取 28 页，临时根回收后经独立
只读清单确认不存在。此前一次运行在首次 `mkdir` 后因分页 DTO 对已有文件的错误拒绝
进入 `uncertain`；只读确认唯一空受管根后才执行一次精确回收，未重试该运行的后续写。
这只验证 C03 专用夹具生命周期，不打开业务整理、STRM、替换或永久删除能力。

`src/watch_assistant/adapters/p115_c03_live_transport.py` 和
`scripts/p115_c03_live_runner.py` 是独立的一次性 live 入口。runner 必须同时收到
`--live`、非零数字 `--parent-id`、显式 `--cookie-path`，并满足上述三个 C03 gate
及 `WATCH_ASSISTANT_P115_C03_LIVE=1`；之后才读取内存 cookie、校验
`p115client==0.0.9.6.5.1` 并以 `P115Client(cookie, console_qrcode=False)` 创建一次客户端。
它不读取 server config，也不接入 app/worker/capability。

live transport 仅调用 `fs_mkdir`、`fs_move`、`fs_rename`、`fs_delete`、`fs_info` 和
固定 `limit=1` 的 `fs_files`。`fs_info` 缺少响应 ID 或父 ID 时保持未确认，不能以
请求 ID 补齐身份；C03 probe 的每次写后核对和回收后根不存在核对均改用父目录的
`fs_files` 列表，从 `cid`、`pid`、`n`、`fc` 精确确认 ID、父级、名称和目录类型。
每个 `list_children` 最多 4 个连续页，并在 `C03DirectoryListing.page_calls` 中记录
实际页数；分页信号缺失/矛盾、`count/offset/limit` 不一致、重复条目、非终止空页、
超过 4 页或 DTO 字段无法确认均为 `complete=false`，因此不会回收。probe 固定最多
15 次列表观察，并在每次调用前按 4 页预留只读预算，最大 `read_calls` 为 60；公开
计数只报告实际页数。真实响应只在 transport 内立即归一化为 C03 DTO，不保存或输出
原响应、ID、名称、路径、pickcode、Cookie 或异常正文。第三方 `state`/`data`、详情
别名和分页字段的跨场景稳定性仍未冻结。

## C02 只读目录 gateway 离线适配

`adapters/p115_library_gateway.py` 与 `adapters/p115_library_transport.py` 是尚未接入
app、worker 或 feature flag 的受限只读适配层。只允许 `fs_files` 与 `fs_info`；默认
transport 仅在实际调用前校验固定版 `p115client` 并创建客户端。它不记录凭据，也不暴露
远端异常正文。

- 适配器只接受非零数字目录 ID 和 `page_size=1`。这是当前唯一有真实证据的
  `limit=1` 两页样本边界，不能放宽为任意目录或页大小。
- payload 固定为 `cid/limit=1/offset/record_open_time=0/show_dir=1`。响应必须返回
  一致的整数 `offset/limit/count`；缺少或矛盾任一项均为
  `pagination_unverified`，不会生成不完整页面。
- 只有 `offset + entries == count` 才合成终止信号；否则只生成下一连续页提示，扫描器
  必须继续读取。任何中断或下一页失败仍由上层保持 `partial`，不可据此删除、清理或
  打开 read capability。
- 调用范围由非根目录 allowlist 限制；只有指定根目录或本次列表已观察到的子目录可继续
  `fs_files` 扫描，`fs_info` 只接受已观察到的文件或子目录（或调用方显式提供的稳定 ID）。
  范围外请求在读取凭据或创建客户端前拒绝。列表和详情中的 pickcode、路径均被主动丢弃，
  DTO 的 `repr` 和边界错误只包含稳定机器码。
- 每次调用把单调时钟的剩余时间传至固定版 documented request hook，并固定
  `retries=False`。无法建立该边界时返回 `blocked_environment`，而不降级为无超时调用。
- `fs_info` 仅用于将已授权的文件或目录详情归一化为本地 DTO；没有下载、移动、重命名、
  mkdir、删除、播放或 STRM 方法。
- 固定版客户端的详情请求按真实只读验收使用文件 `fid` 与目录 `cid`；不使用展示层的
  `file_id` 作为远端 payload 键。
- 详情成功响应可能不回显对象或父目录身份。仅当该对象刚由受管 `fs_files` 结果观察到，且
  详情没有任何冲突身份字段时，gateway 才复用观察到的身份；无法解释的可选时间字段不写入
  DTO。任何显式身份冲突仍返回 `detail_unverified`。
- 2026-08-14 真实 115 复验结论（推送可用性观察失败的根因修复）：
  - 云下载推送的磁力会在目标目录创建**同名目录**（内含文件）。`fs_files`/
    `fs_files_app` 的 show_dir=1 列表即被列目录的**完整直接子项**（目录 + 文件，
    含新推送尚未完成索引的文件）；记录对文件与目录完全同形（`fc` 恒为 0 或
    "0"、无 `is_dir`），**`fc`/`file_category` 语义随接口漂移（legacy 文件索引
    文件 fc=1、app 列表文件 fc="0"），不能作为判据**。
  - 文件/目录只能以 `fs_info` 判型：响应的 **`count`（目录的子树文件数，
    字符串）>0 或 `folder_count`>0 即目录**，否则为文件。纯文件目录的
    `folder_count`=0、目录也可能有非零 `play_long`（如磁力目录聚合时长），
    `folder_count`/`play_long` 单独都不可靠；`count` 是实测最稳定的目录信号
    （对目录稳定返回正整数，对文件为 0；未完成同步的磁力目录 count 为 0，会
    被暂判为文件，下次扫描随索引更新修正）。
  - show_dir=0 列表是**递归全树文件**（含嵌套子目录中的文件），与目录列表语义
    不同，不能与目录索引合并（会造成重复与错误归属）；列表以 show_dir=1 为主。
  - 目录列表的 file-style 记录无 pid（`cid` 为直接父级 id），父级即被列出的
    目录，由 gateway 注入；folder-style 记录 `cid` 为自身 webapi id、`pid` 为
    父级。文件 id 取 `fid`，缺失时取 webapi id（`cid`/`fid`）。
  - 详情（`fs_info`=proapi `open/folder/get_info`，legacy/proapi 相同形态）**不回显
    对象自身 ID**，但文件详情带 `paths` 父链（末端即直接父目录）。文件详情在无
    观察条目时，以 `paths` 父链落在授权目录内为锚验证身份（请求 ID 是 fid 回显，
    父链是真实校验），并把父目录（id+名）登记为已观察目录；随后的目录详情经该
    登记验证身份，并与 `file_name` 交叉核对。目录详情携带 `count`/`folder_count`
    与 `expect_directory` 冲突时失败关闭；列表条目无法判型时同样失败关闭。
  - 该修复使推送任务的 `availability_observer_unavailable` 可消除：`clouddownload`
    的 `file_id`（webapi id）直接可用 `fs_info({"fid": ...})` 验证，`paths` 父链即
    目标目录；树扫描可正常完成（page_size=1 亦稳定），媒体库库存完整后可放行推送。

2026-07-28 使用受管非根小目录完成一次 C02 真实只读验收：`fs_files=1`、
`fs_info(file)=1`，公开结果为 `success/complete=true`、一页、一条目；没有自动重试。
凭据、目录身份、文件名、路径、pickcode 和原始响应均未记录。本证据仅将该单页列表和
已观察文件详情标为 `verified_read_only`；目录详情、多页/大目录、并发、远端总数语义、
pickcode、直链及所有写能力仍为 `unverified`。所有 115 写入、STRM、替换和清理能力继续关闭。

`scripts/p115_library_readonly_live_runner.py` 是单次 C02 验收入口，不接入应用。只有同时
提供 `--live`、`WATCH_ASSISTANT_P115_LIBRARY_READONLY_LIVE=1`、非根目录 ID 和 Cookie
文件路径时才可能读取远端；默认以及任一前置失败均为零外部调用。每次最多读取两个连续
`fs_files` 页面，并且最多读取一个已发现文件和一个已发现子目录的 `fs_info`；没有重试、
写入、下载、播放或 STRM 操作。公开 JSON 只含状态、计数和错误码。
## C05 播放/动态直链离线预备

`adapters/p115_playback_contract.py` 是 `offline_only` 的 C05 合同基础，
不导入 `P115Client`、`CookieProvider` 或任何 HTTP 客户端，也没有 API 路由、
STRM 写入、传输实现或持久化。动态链接只能作为 `DynamicLinkOutcome.url` 的
短暂内存值存在；DTO 的 `repr`、错误码和 fake 调用记录均不包含 URL、Cookie、
pickcode、manifest ID 或 token 类值。

- `PlaybackGate` 默认 `enabled=false`、`contract_verified=false`；因此默认
  结果为 `disabled/playback_disabled`。即使显式开启而未完成真实合同验证，仍为
  `unverified/contract_unverified`。
- 输入只接受受管 STRM 产生的 `strm_` opaque ID、`HEAD` 或 `GET`，以及至多一个
  严格的 `bytes` Range。格式外 ID 拒绝为 `invalid_manifest_id`；格式正确但不在
  fake 受管集合中的 ID 返回 `not_found/manifest_out_of_scope`。
- 转发策略已冻结为：`HEAD` 绝不转发 Range；`GET` 只转发一个已验证的 byte range。
  这只是本地合同，不是 115、CDN 或媒体服务器的真实行为证据。
- 超时固定映射为 `uncertain/timeout`，不重试；`CancelledError` 必须继续传播；
  其他异常固定为 `failed/remote_failed`，不得保留异常正文。

真实 `download_url`、链接 TTL、重定向状态、Cookie 失效、HEAD/GET/Range 服务端
语义、限流与媒体服务器兼容性仍是 `unverified`。C05 未通过专门授权的脱敏真实
探针前，播放 capability 必须保持关闭。

## 固定版本源码观察

本地源码仅用于记录方法名和 `inspect.signature`，没有执行任何客户端实例化。
`fs_files` 的源码文档明确使用 `cid`、`limit`、`offset`；`fs_info` 接受
`file_id` 或 path。源码默认 Web API 地址为 `https://webapi.115.com`，但这不是
本任务的网络验证结论。C03 runner 复用固定版 documented `request` hook，将每次
共享 deadline 的剩余时间作为底层 urllib3 `timeout`，并固定 `retries=False`；该
调用级传递、停止和脱敏边界已有离线 fake 契约。它仍不是实时验收证据：真实运行仍
必须满足一次性授权、受管夹具、四项 gate、零重试和精确清理保护，且本轮未访问
服务器、真实 115、Cookie 或外部 HTTP。

固定版 `p115client` 的公开源码把缺失 `state` 视为成功，并直接读取二维码 token 的
`data.uid/time/sign`；`qrcode` 缺失时使用 `uid` 构造扫码地址。离线 C03 iPad 登录
边界据此只接受缺失或明确成功的状态、上述三个严格字段及可选二维码字段；显式失败、
固定版已知错误码别名、矛盾状态、非法 Unicode、未知类型和缺少关键字段均拒绝，DTO
和 repr 不呈现认证字段。

## 后续门禁

真实探针必须单独获得 Cookie 安全授权，并把所有响应字段、ID、路径、pickcode、
文件名和 URL 做脱敏后才能运行。C03 写契约、C04 稳定性、C05 直链和
HEAD/GET/Range 在各自通过前，不得打开对应 capability 或接入业务 worker。
