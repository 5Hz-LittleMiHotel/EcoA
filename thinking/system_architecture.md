# EcoA 系统架构演进记录

日期：2026-04-29

本文记录本次对话中围绕 EcoA 编码智能体架构所做的讨论、修改、踩坑、疑问与结论。重点不是逐行代码说明，而是把系统设计的逻辑脉络保存下来，方便后续继续演进。

## 1. 初始目标

用户一开始希望把现有 EcoA 从单一 ReAct agent，改造成如下结构：

```text
简单任务
  -> ReAct

复杂任务
  -> Plan-and-Execute
      -> strong LLM 规划
      -> weak LLM 执行每个小步骤

高要求任务
  -> Plan-and-Execute
  -> task-level Reflection
      -> strong LLM 审查
```

同时要求 `.env` 明确区分强模型和弱模型，例如：

```text
STRONG_MODEL_ID
STRONG_ANTHROPIC_BASE_URL
WEAK_MODEL_ID
WEAK_ANTHROPIC_BASE_URL
```

## 2. 初始代码观察

阅读 `ecoa/` 后发现，当前项目不是前端 React，而是 Python CLI agent 框架。这里的 “react” 应理解为 ReAct 工具调用循环。

初始结构大致是：

```text
cli.py
  -> agent_loop()
      -> single MODEL_ID
      -> SYSTEM prompt
      -> full tool registry

teammates.py
  -> teammate loop
      -> same MODEL_ID
      -> teammate tools

tool_registry.py
  -> 所有 lead 工具集中注册
```

早期问题：

- `config.py` 只有一个 `MODEL_ID`。
- `agent_loop` 绑定单一 `client/MODEL/SYSTEM`。
- lead agent 同时拥有执行、团队协调、任务管理、worktree 管理等权力。
- Plan-and-Execute 还不存在明确的 orchestrator。

## 3. 第一轮架构修改：抽出 ReAct Executor

第一轮改造思路是：

```text
agent.py
  -> run_react_loop(messages, model_profile, system_prompt, tools, handlers)

orchestrator.py
  -> 判断 react / plan_execute / reflection

planner.py
  -> strong LLM 产出结构化 plan

reflection.py
  -> strong LLM 做任务级审查
```

对应修改：

- `config.py` 新增 `ModelProfile`。
- 新增 `STRONG_PROFILE` 和 `WEAK_PROFILE`。
- `agent.py` 抽出 `run_react_loop(...)`。
- 新增 `planner.py`。
- 新增 `reflection.py`。
- 新增 `orchestrator.py`。
- `cli.py` 普通用户输入改为进入 `orchestrate_task(history)`。
- `compression.py` 改为使用 strong profile。
- `teammates.py` 默认使用 weak profile。

第一轮流程：

```text
user input
  -> orchestrator
      -> simple: weak ReAct
      -> complex: strong planner -> weak ReAct per step
      -> high requirement: strong reflection
```

## 4. 踩坑一：Plan-and-Execute 输出被截断

测试 prompt：

```text
请分析 ecoa/agent.py、ecoa/orchestrator.py、ecoa/planner.py、ecoa/reflection.py 之间的调用关系，并生成一份架构说明，直接输出不要写入文件。这个任务需要多步完成，但不要修改代码。
```

现象：

- Planner 正常生成多个步骤。
- Step Executor 也执行了最后的 “生成架构说明”。
- 但最终输出只显示：

```text
- step 6: Generate architecture description -> Based on all prior analysis, here is the architectural description:
```

原因：

`orchestrator._format_final_response()` 对每个 step 只取了第一行：

```python
first_line = result.splitlines()[0]
```

这导致真正的架构说明正文被丢掉。

修复：

- Planner step 支持 `deliverable` 字段。
- `PLANNER_SYSTEM` 要求分析、说明、文档类最终答案 step 设置 `deliverable=true`。
- `_format_final_response()` 优先完整输出 `deliverable=true` 的 step。
- 如果 planner 没标记，则完整输出最后一个非 repair step。
- 执行摘要放到正文后面。

修复后的输出结构：

```text
<完整最终交付物>

---
Completed via plan_execute.
Steps executed: N.
- step 1 ...
- step 2 ...
```

## 5. 路由优化：从关键词到混合意图识别

早期 `choose_route()` 主要靠关键词：

```text
architecture / 架构 / 实现 / 测试 / production / review ...
```

问题：

```text
where are your architectural description?
```

因为包含 `architectural`，可能误判为复杂架构任务，重新进入 Plan-and-Execute。

第一步优化：

- 增加 `followup` 判断。
- 增加 `direct react` 判断。
- 区分只读分析和修改意图。
- 识别显式 `plan-and-execute`。
- 识别 reflection 风险词。

进一步讨论后，用户问：

> 所以你的意图识别，是靠关键字匹配进行的？

回应：

是。规则可解释、便宜、快，但语义能力弱。更生产化的做法是混合路由：

```text
高置信规则
  -> 直接路由

低置信 / 冲突 / unknown
  -> 调用 strong LLM router
```

随后实现混合路由：

- `ROUTER_SYSTEM` 新增到 `prompts.py`。
- `choose_route(..., use_llm_router=True)` 在需要时调用 strong LLM。
- 路由输出包含：

```json
{
  "mode": "react | plan_execute",
  "reflection_required": true,
  "intent": "followup | analysis | modify | review | unknown",
  "confidence": "low | medium | high",
  "reason": "..."
}
```

另一个细节坑：

短英文关键词如 `it`、`add` 如果用普通 substring，可能误命中其他单词。

例如：

```text
add in address
test in latest
```

所以英文关键词改成词边界匹配：

```python
\badd\b
```

中文关键词仍使用包含匹配，因为中文没有天然空格词边界。

## 6. Prompt 身份问题：模型是否需要知道 EcoA 是谁

用户注意到：

> 在 PLANNER_SYSTEM 和 REFLECTION_SYSTEM 中显式写了 “You are EcoA's ...”，但是它们真的知道 EcoA 是谁吗？

回应：

不天然知道。除非 prompt 或输入上下文说明，否则 “EcoA” 只是一个名字标签。

讨论过几种方案：

1. 加共享 `ECOA_CONTEXT`。
2. 在每次调用时传动态 runtime context。
3. 建 `context.py`。
4. 使用结构化 schema，而不是解释系统身份。
5. few-shot examples。
6. 代码级 contract 强校验。
7. 配置化 policy。

最终倾向：

Planner、Reflection、Router 未必需要知道 “EcoA 是谁”。它们真正需要的是职责、输入输出格式、约束。品牌名如果不提供可执行信息，可以删除或淡化。

用户随后将 prompt 做了调整：

- `LEAD_SYSTEM` 中加入 `EcoA`。
- `PLANNER_SYSTEM` 中也将 Planner 命名为 EcoA。

## 7. Lead Agent 的职责疑问

用户问：

> 什么时候会用到 Lead Agent？

当时的结构里：

- `react` 路线使用 `SYSTEM = LEAD_SYSTEM`。
- 按 Enter 处理 lead inbox 时调用 `agent_loop(history)`，也使用 `LEAD_SYSTEM`。

因此 Lead Agent 当时承担：

- 简单任务的 direct ReAct 执行。
- lead inbox / teammate 协调 / plan approval 协议处理。

但随后用户指出一个更深的问题：

> Lead Agent 占用了一部分 Orchestrator 的角色。

这个判断是后续重构的关键。

## 8. 架构边界问题：权力应该在代码中，而不是 LLM 工具调用中

用户提出：

> weak ReAct Executor 不应该被赋予 spawn_teammate 的能力，而应该由 Orchestrator 派生 teammate，或者执行 weak ReAct Executor。

这推动了第二轮核心重构。

问题根源：

旧结构中有三套指挥权重叠：

```text
Lead Agent
  可以 spawn teammate / task / worktree / inbox protocol

Orchestrator
  可以 route / plan / reflection

Weak ReAct Executor
  因为拿完整工具集，也能 spawn teammate / task / worktree
```

用户进一步补充：

> Lead Agent 的人工交互型团队协调权利和 task_get 权利应该交给 Orchestrator，Lead Agent 仅仅是一个 Weak ReAct Executor。

达成的新原则：

```text
LLM 可以建议调度。
Python Orchestrator 执行调度。
```

或者说：

```text
把权力攥在代码上，而不是让 LLM 随意调用。
```

这样更严谨、可控、可测试，也更容易出问题后溯源。

## 9. 第二轮架构修改：Orchestrator 收回组织级权力

第二轮修改重点：

### 9.1 拆分工具权限

`tool_registry.py` 新增：

```python
EXECUTOR_TOOLS
EXECUTOR_TOOL_HANDLERS
```

Direct Executor 和 Step Executor 只允许：

```text
bash
read_file
write_file
edit_file
todo
load_skill
compact
background_run
check_background
```

不再允许：

```text
spawn_teammate
send_message
read_inbox
task_get
task_create
task_update
worktree_*
plan_approval
```

### 9.2 Lead Agent 改为 Direct Executor

`prompts.py` 中：

- 新增或改名为 `DIRECT_EXECUTOR_SYSTEM`。
- `LEAD_SYSTEM = DIRECT_EXECUTOR_SYSTEM` 只作为兼容别名。
- `SYSTEM = DIRECT_EXECUTOR_SYSTEM`。

Prompt 明确：

```text
Direct Executor 不管理 teammates、task board、worktrees、inbox protocol、plan approval。
这些组织级动作属于 Python orchestrator。
```

### 9.3 Planner 输出声明式 step

Planner step schema 扩展：

```json
{
  "id": "1",
  "type": "react_step | spawn_teammate | send_message | wait_teammate | task_get | task_create | task_update | task_list | worktree_create | worktree_status | worktree_run | worktree_keep | worktree_remove | worktree_list",
  "goal": "...",
  "instructions": "...",
  "success_criteria": "...",
  "risk": "low | medium | high",
  "deliverable": true,
  "args": {}
}
```

Planner 仍不执行动作，只产出声明式 plan。

### 9.4 Orchestrator Step Dispatcher

`orchestrator.py` 新增：

```python
_run_orchestrator_step(step)
_execute_plan_step(...)
```

其作用：

- 如果 step 是 `react_step`，交给 weak Step Executor。
- 如果 step 是组织级 action，由 Python 代码直接执行。

支持的组织级 action：

```text
spawn_teammate
send_message
wait_teammate
task_get
task_create
task_update
task_list
worktree_create
worktree_status
worktree_run
worktree_keep
worktree_remove
worktree_list
```

对应底层调用：

```python
TEAM.spawn(...)
BUS.send(...)
BUS.read_inbox("lead")
TASKS.create/update/get/list_all(...)
WORKTREES.create/status/run/keep/remove/list_all(...)
```

### 9.5 Inbox 由 Orchestrator 接管

按 Enter 处理 inbox 时，不再调用 Lead Agent。

`cli.py` 改为：

```python
process_orchestrator_inbox(history)
```

底层 mailbox 名字仍叫 `"lead"`，但语义已经变成：

```text
Orchestrator 的 inbox
```

保留 `"lead"` 是为了兼容已有 teammate / BUS / protocol 代码。

## 10. Orchestrator、Step Executor、Dispatcher 的边界

用户问：

> 有了 Step Executor，为什么还需要 Orchestrator Step Dispatcher？

答案：

它们处理的是两类不同的 step。

### Step Executor

处理工程执行类任务：

```text
读代码
写代码
跑命令
分析错误
修复实现
总结结果
```

它需要 LLM ReAct，因为需要理解代码和根据反馈调整。

典型 step：

```json
{
  "type": "react_step",
  "goal": "Update router tests",
  "instructions": "Inspect current tests, add coverage, run syntax check"
}
```

### Orchestrator Step Dispatcher

处理组织级动作：

```text
派生 teammate
发消息
等 inbox
维护 task
维护 worktree 生命周期
```

这些动作不应该由 LLM 自己拿工具调用，而应由 Python 代码执行。

典型 step：

```json
{
  "type": "spawn_teammate",
  "args": {
    "name": "tester",
    "role": "test engineer",
    "prompt": "Verify router behavior"
  }
}
```

总结：

```text
做工程活 -> Step Executor
做调度活 -> Orchestrator Action Dispatcher
```

用户指出：

> “派生或管理 teammate、负责和 teammate 沟通”不是和 Orchestrator 直接可执行的组织级动作重合了吗？

回应：

是的，完全重合。它们不是两套职责，而是同一件事的两个层级：

```text
Orchestrator 直接可执行的组织级动作
  = 能力集合

Orchestrator Step Dispatcher
  = 执行这些能力的代码入口
```

更好的命名可能是：

```text
Orchestrator Action Dispatcher
Control Plane Dispatcher
```

当前名字还可以继续优化。

## 11. 当前架构总结

当前架构：

```text
CLI
  -> Orchestrator
      -> Router
          -> rules first
          -> strong LLM router on low-confidence/conflict

      -> Direct Executor
          -> simple react route
          -> weak LLM
          -> restricted tools

      -> Planner
          -> strong LLM
          -> declarative plan

      -> Plan Step Loop
          -> react_step
              -> Step Executor
              -> weak LLM
              -> restricted tools

          -> organization action step
              -> Orchestrator Action Dispatcher
              -> Python direct call

      -> Reflection
          -> strong LLM
          -> pass / revise / block
```

角色分工：

| 角色 | 是否 LLM | 主要职责 | 工具/能力 |
|---|---:|---|---|
| Orchestrator | 否 | 路由、调度、执行组织级动作、汇总结果 | Python 直接调用 TEAM/BUS/TASKS/WORKTREES |
| Router | 是，strong，按需 | 判断 react / plan_execute / reflection | 无工具，只输出 JSON |
| Planner | 是，strong | 生成声明式 plan | 无工具，只输出 JSON |
| Direct Executor | 是，weak | 简单任务直接执行 | 受限执行工具 |
| Step Executor | 是，weak | 执行单个 `react_step` | 受限执行工具 |
| Reflection | 是，strong | 任务级审查 | 无工具，只输出 JSON |
| Teammate | 是，weak | 被 Orchestrator 派生的持久子智能体 | 目前仍有较多独立工具，后续可继续治理 |

受限执行工具：

| 工具 | 能力 |
|---|---|
| `bash` | 在当前 workspace 运行 shell 命令 |
| `read_file` | 读取文件 |
| `write_file` | 写入文件 |
| `edit_file` | 精确替换文件内容 |
| `todo` | 维护当前执行上下文的待办 |
| `load_skill` | 加载本地 skill 内容 |
| `compact` | 请求上下文压缩 |
| `background_run` | 后台运行长命令 |
| `check_background` | 检查后台命令状态 |

Orchestrator 组织级能力：

| 动作 | 能力 |
|---|---|
| `spawn_teammate` | 派生持久 teammate |
| `send_message` | 给 teammate 发普通消息 |
| `wait_teammate` | 等待 teammate 向 orchestrator inbox 发消息 |
| `task_get` | 读取任务详情 |
| `task_create` | 创建任务 |
| `task_update` | 更新任务状态、owner 或依赖 |
| `task_list` | 列出任务 |
| `worktree_create` | 创建 worktree |
| `worktree_status` | 查看 worktree 状态 |
| `worktree_run` | 在 worktree 中运行命令 |
| `worktree_keep` | 标记保留 worktree |
| `worktree_remove` | 移除 worktree |
| `worktree_list` | 列出 worktree |

## 12. 当前保留问题

### 12.1 `"lead"` mailbox 命名

底层 mailbox 仍叫：

```text
lead
```

但语义已经是：

```text
orchestrator inbox
```

后续可以迁移为：

```python
ORCHESTRATOR_INBOX = "orchestrator"
LEGACY_LEAD_INBOX = "lead"
```

### 12.2 Teammate 权限仍较完整

Direct Executor 和 Step Executor 已经收窄权限，但 teammate 仍有较多工具，包括：

```text
bash
read_file
write_file
edit_file
send_message
read_inbox
idle
task_get
shutdown_response
plan_approval
claim_task
```

后续可以继续治理 teammate 权限，尤其是是否允许 teammate 自主 claim task、提交 plan approval、写文件等。

### 12.3 Plan validation 还不够强

目前边界主要依赖 `step.type`。

后续应增加代码级校验：

- `react_step.instructions` 不允许包含组织级动作请求。
- 组织级 step 必须有完整 `args`。
- 非法 step type 直接 reject 或转成 revise。
- `worktree_run` 是否允许写入型命令需要策略。
- 高风险组织级动作是否需要 confirmation 或 reflection。

### 12.4 `worktree_run` 是边界灰区

它是组织级入口，但运行命令可能修改文件。

更严谨的设计可能是：

- `worktree_run` 只允许只读命令或测试命令。
- 真正代码修改仍由 Step Executor 执行，只是运行在指定 worktree 上。

## 13. 设计原则沉淀

本次对话中形成的核心原则：

```text
1. LLM 可以建议，但组织级权力应由 Python 代码执行。
2. Planner 只产出声明式计划，不执行计划。
3. Executor 只做工程执行，不做调度。
4. Orchestrator 是 control plane。
5. Step Executor 是 execution plane。
6. Reflection 不直接修改代码，只给 verdict 和 repair steps。
7. 简单任务可以 direct ReAct，复杂任务进入 Plan-and-Execute。
8. 高风险或生产级任务必须 Reflection。
9. 路由采用 rules-first + LLM-on-uncertainty 的混合策略。
10. 能用代码约束的地方，不依赖 prompt 自觉。
```

这一版架构仍在演进中，但已经从 “LLM 拿完整工具自由发挥” 转向 “Python orchestrator 掌握调度权，LLM 负责规划、执行单步、审查”。
