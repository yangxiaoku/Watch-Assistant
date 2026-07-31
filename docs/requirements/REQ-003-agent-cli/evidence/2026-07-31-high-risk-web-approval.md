# REQ-003 高风险 Web 批准证据

日期：2026-07-31

## 本次范围

- 整理计划按 `action_count` 与 `ORGANIZATION_HIGH_RISK_ACTION_THRESHOLD` 比较，默认阈值为 10。
- 超过阈值的计划必须先由 Web 会话创建并批准专属 workflow。
- 整理操作服务端在排队前再次校验批准 workflow、approval 阶段、计划身份和批准状态。
- Bearer Agent 调用 workflow 批准接口会被拒绝；CLI/MCP 不能绕过 Web 批准门禁。
- 阈值默认只影响本地排队门禁，不开启任何生产 115 写入。

## 验证

```text
tests/integration/test_organization_operations_api.py::test_high_risk_operation_requires_web_approval PASSED
tests/integration/test_workflows_api.py PASSED
tests/integration/test_organization_plan_api.py PASSED
```

专项结果：`13 passed`。

关键断言：

1. 11 个整理动作在没有批准 workflow 时返回 `high_risk_approval_required`。
2. Bearer Agent 批准请求返回 `web_approval_required`，不改变 approval 阶段。
3. Web 批准后，带正确 `workflow_id` 的整理操作才会排队。
4. workflow 的 approval 阶段绑定内部计划 ID，不能借用其他计划的批准。

## 未覆盖

生产媒体库、真实 115 写入、生产 STRM 和外部媒体服务器仍按 `BLOCKERS.md` 保持关闭；本证据不代表生产能力已上线。
