# REQ-010 媒体库范围配置契约

## 目的

库存推送门禁只允许使用管理员明确声明、并与服务端 `P115_TARGET_CID` 一致的 115 根目录。配置接口本身不会声明扫描完成，也不会执行任何 115 写操作。

## 接口

- `PUT /api/v1/libraries/{library_id}/configuration`
  - Web 会话或带 `settings:write` Scope 的 Agent Token。
  - 新建时 `revision` 必须为 `0`。
  - `root_directory_id` 必须是非零数字，并且等于服务端 `P115_TARGET_CID`。
  - 新建范围保存为 `scope_verified=false`、`enabled=false`。
  - 目录范围变化会自动清除旧验证并禁用范围。
- `POST /api/v1/libraries/{library_id}/verify-scope`
  - Web 会话或带 `settings:write` Scope 的 Agent Token。
  - 通过只读 P115 gateway 读取根目录第 1 页并验证分页状态。
  - 只有读取结果完整时才设置 `scope_verified=true`、`enabled=true`。

## 安全边界

- 测试 CID 不会被接口接受为生产目标，除非它本身就是服务端显式配置的目标 CID。
- 验证成功不等于库存新鲜；没有完整、成功的库存扫描时，`InventoryPushGuard` 仍返回 fail-closed。
- 配置和范围验证分别写入 `library.configuration.changed`、`library.scope.verified` 审计事件。
- revision 不匹配返回 `library_configuration_conflict`，不会覆盖当前配置。

