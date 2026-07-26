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
- `DirectoryPage` 和 `ScanResult` 同时表达 `complete`、`partial`、`cancelled`。
- 扫描发现异常、重复页、空页或页数漂移时返回 `partial`；取消继续传播。
- DTO 的 `repr`、fake 调用记录和错误码不包含名称、完整路径、pickcode 或异常正文。

## p115client 候选能力矩阵

| 能力 | 固定版本候选方法/签名 | 当前状态 | 需要的输入 | 预期输出 | 副作用 | 下一步探针 | 未知点 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| list directory | `P115Client.fs_files(payload=0, /, base_url='https://webapi.115.com', *, async_=False, **request_kwargs)` | unverified；DTO 为 offline_only | `cid`、`limit`、`offset`，可选 `show_dir` | 列表、页游标/总数候选 | 可能记录打开时间（库默认 `record_open_time=1`） | 固定目录、只读分页、核对重复/漏项 | `page/page_count` 与 `count/offset` 的真实组合、最大安全页大小 |
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

## 固定版本源码观察

本地源码仅用于记录方法名和 `inspect.signature`，没有执行任何客户端实例化。
`fs_files` 的源码文档明确使用 `cid`、`limit`、`offset`；`fs_info` 接受
`file_id` 或 path。源码默认 Web API 地址为 `https://webapi.115.com`，但这不是
本任务的网络验证结论。

## 后续门禁

真实探针必须单独获得 Cookie 安全授权，并把所有响应字段、ID、路径、pickcode、
文件名和 URL 做脱敏后才能运行。C03 写契约、C04 稳定性、C05 直链和
HEAD/GET/Range 在各自通过前，不得打开对应 capability 或接入业务 worker。
