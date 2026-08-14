## What

**跨机 spawn 结构性修复（Task #1236 follow-up，#1818 2026-08-13 23:52 报）**：runner 的 ops server 以最小权限 `ava_runner` 角色运行、按设计不能 INSERT agents/agents_meta——所以旧的跨机 spawn 流程（runner 侧 `spawn_agent_op` 建行）在 #2599 落地后**全挂**（#405 在 mba runner 撞到，任何非本地 target 的 POST /api/agents 500）。本 PR 把 spawn 拆成两阶段：

- **create 在 gateway**：`ops.agent_spawn.create_agent_row`（in-process 主角色）INSERT agents + agents_meta（status='allocated'），解析 fork checkpoint、fork 的 pre-launch prompt 也在这里 INSERT；返回 `(new_id, birth_config)` 供 launch op 携带。
- **launch 留 runner**：新 op kind `spawn-launch`（`LaunchAgentRequest{agent_id, config, birth_config, prompt, prompt_source, label}`）——runner 只做：model-config 校验、detached 子进程 launch、schedule_launch_confirm、plain-spawn 首 prompt 投递 + InboundArrived（全在 ava_runner 权限内）。旧 `spawn` op kind、`spawn_agent` / `spawn_agent_op` 已删除。
- 所有 spawn 入口统一走 `gateway.routers.agents.create_and_launch_agent`（POST /api/agents、guide/packages/schedules draft 路由、MCP spawn tool），preflight / 建行 / launch 行为一致。
- `_NON_IDEMPOTENT_KINDS` 与 `OpKind` 改为以 `spawn-launch` 为键；混版窗口内旧 kind 明确报 unknown-op，不会半建行。

## File-tree diff（★ = critical path）

```
ops/agent_spawn.py              ★ spawn_agent → create_agent_row（去 launch tail，主角色建行）
ops/ops_lifecycle.py            ★ spawn_agent_op 删除 → launch_agent_op（runner 侧 launch）
ops/rpc_schemas.py              ★ OpKind + LaunchAgentRequest；SpawnAgentRequest 仅作 REST body
services/agent_ops/daemon.py    ★ dispatch case "spawn-launch" → launch_agent_op
gateway/routers/agents.py       ★ create_and_launch_agent 抽取（preflight→建行→forward）
gateway/routers/agents_forward.py ★ _forward_spawn_to_remote 改收 LaunchAgentRequest
gateway/routers/guide.py        draft 路由走 create_and_launch_agent
gateway/routers/packages.py     draft 路由走 create_and_launch_agent
gateway/routers/schedules.py    draft 路由走 create_and_launch_agent
gateway/mcp_endpoint.py         MCP spawn tool 走 create_and_launch_agent
ops/cluster_rpc.py              _NON_IDEMPOTENT_KINDS += spawn-launch
ops/agent_launch.py / agent_wake.py / agents.py   注释与再导出更新
frontend/src/lib/types-generated.ts / openapi.json   codegen 刷新（schema docstring 变更）
tests/…                         测试适配新 split（conftest in-process launch 替身、
                                daemon/operations/agents_internals/spawn_forward 重写、
                                13 个测试文件的 spawn_agent() setup → tests.conftest 共享 helper）
```

## Data flow

`POST /api/agents`（SDK/前端/脚本）→ gateway `create_and_launch_agent`：
1. `_spawn_preflight_blocking`（注册表 agent-runner 能力 + preset fold + model-config 校验）
2. `_spawn_prechecks_blocking`（fork checkpoint 解析）
3. `create_agent_row`（in-process INSERT agents/agents_meta，fork prompt pre-launch 落库）
4. `dispatch_to_machine(target, kind="spawn-launch", LaunchAgentRequest)` → runner ops daemon → `launch_agent_op`（校验 → `_launch_agent_process`（config/birth_config 走 env）→ schedule_launch_confirm → plain-spawn 首 prompt INSERT + InboundArrived publish）→ 返回 `SpawnedAgent(id)`。

跨机时 gateway 与 runner 之间的边界 = 建行（gateway 主角色）与 launch（runner 角色）之间；单机是 localhost 特例，同一代码路径。

## Verification

- 本地：ruff / pyright（pre-commit 同款全仓 0 error）/ vitest 2482 / eslint / tsc 全绿
- pytest：gateway+ops+services/agent_ops+ava+agent+services+integration ≈ 5500 用例全过
- e2e：跑中（见结论）

## Not tested

- 真实双机（gateway 在 machine-1、runner 在 mba）的跨机 spawn——部署后由 #1818 在 mba 实测（#1269 路径复验）
- staging 反向验证（#2599 的做法）——按 #1818 部署节奏执行
