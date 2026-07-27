# P115 Library Contract Notes

本文件对应 Phase 0-A 的 C01/C02 离线部分。固定依赖为
`p115client==0.0.9.6.5.1`；本提交没有读取 Cookie、创建 `P115Client` 或发起
网络请求。除 `offline_only` 的 fake/DTO 验证外，所有真实能力均为
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

## p115client 候选能力矩阵

| 能力 | 固定版本候选方法/签名 | 当前状态 | 需要的输入 | 预期输出 | 副作用 | 下一步探针 | 未知点 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| list directory | `P115Client.fs_files(payload=0, /, base_url='https://webapi.115.com', *, async_=False, **request_kwargs)` | unverified；DTO 为 offline_only | `cid`、`limit`、`offset`，可选 `show_dir` | 列表、页游标/总数候选 | 可能记录打开时间（库默认 `record_open_time=1`） | 固定目录、只读分页、核对重复/漏项 | `fs_files` 的 `offset/limit/count` 到分页 DTO 的映射、`page/page_count` 语义和最大安全页大小 |
| file detail | `P115Client.fs_info(payload, /, base_url='https://webapi.115.com', *, async_=False, **request_kwargs)` | unverified；DTO 为 offline_only | `file_id` 或 path | 文件详情候选：ID、父目录、大小、mtime、pickcode | 只读；目录查询可能计算统计 | 只读文件详情字段脱敏记录 | `fid/cid`、时间和 pickcode 字段的真实类型 |
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

真实 C03 验收仍需要用户批准的专用非生产临时目录、可验证的前后置条件和精确
清理清单，必须按“建目录 -> 上传/复制 fixture -> 重命名 -> 移动 -> 隔离 ->
恢复 -> 只读核对 -> 精确清理”顺序单独授权。任何 timeout/uncertain 只允许
只读核对并停止后续写入；不得使用正式影视文件。永久删除、业务 worker、API、
前端和 capability 本轮均未接入。

### C03 本地生命周期探针

`src/watch_assistant/adapters/p115_c03_fixture_probe.py` 与
`scripts/p115_c03_fixture_probe.py` 只提供注入式边界和 offline fake；模块不读取
Cookie、不创建 `P115Client`、不发网络。探针只接受非零十进制 `parent_id`，拒绝 URL、
路径和任意目录输入。每轮固定执行 4 次 `mkdir`、5 次隔离/恢复组合操作及 1 次
`fs_delete` 回收，最多 10 次写调用和 11 次只读核对；冲突、批量观测本轮未覆盖且预算为 0。

运行前必须同时设置：
`WATCH_ASSISTANT_P115_C03_WRITE=1`、
`WATCH_ASSISTANT_P115_C03_MANAGED_FIXTURE=1`、
`WATCH_ASSISTANT_P115_C03_CLEANUP_PLAN=1`。注入边界的 `live=True` 还必须设置
`WATCH_ASSISTANT_P115_C03_LIVE=1`；CLI 仅运行 offline fake，不提供 live transport。
永久删除门禁没有入口，回收只使用已冻结的 `fs_delete(fid)` 候选形态。

根名由随机 nonce 生成，公开报告只保留短指纹、阶段、调用计数和固定错误码，不输出
ID、名称、路径、Cookie、pickcode、URL 或第三方异常正文。每次写入后都按稳定
`file_id + parent_id + name` 做只读核对；任一失败、超时、取消或无法确认均报告
`uncertain`，不自动重试。只有再次精确核对本轮创建的受管根后，才允许一次回收，
并在回收后只读确认根不存在。该探针仍是 fixture-only 证据，不打开业务 capability。

## 固定版本源码观察

本地源码仅用于记录方法名和 `inspect.signature`，没有执行任何客户端实例化。
`fs_files` 的源码文档明确使用 `cid`、`limit`、`offset`；`fs_info` 接受
`file_id` 或 path。源码默认 Web API 地址为 `https://webapi.115.com`，但这不是
本任务的网络验证结论。

## 后续门禁

真实探针必须单独获得 Cookie 安全授权，并把所有响应字段、ID、路径、pickcode、
文件名和 URL 做脱敏后才能运行。C03 写契约、C04 稳定性、C05 直链和
HEAD/GET/Range 在各自通过前，不得打开对应 capability 或接入业务 worker。
