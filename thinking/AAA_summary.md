项目简介：Easy Code Agent，EcoA，符合环保思想，以及Claude code的设计理念：减少token浪费，使用不同参数量的模型完成不同内容；顺从大模型概率生成的逻辑，制定出合适的马具：合身是原则，少即是多。

## 项目结构

目前结构是：

按**代码真实调用层级**来，不再混口径。每一层都标了：**架构角色、能调用的函数/对象、不能直接调用什么**。
```text
EcoA.py
  -> CLI
      -> Orchestrator
          -> Router
              -> Direct ReAct Executor
                  -> Skills
                  -> Context Compression

              -> Plan-and-Execute
                  -> Planner
                  -> Step Dispatcher
                      -> Step Executor
                          -> Skills
                          -> Context Compression
                      -> Orchestrator Action Dispatcher
                          -> Teammate Actor System
                          -> Shared State / Blackboard
                  -> Reflection / Reviewer
```

```text
[0] Program Entry
架构角色：程序入口
代码：
  EcoA.py
    -> cli.main()


[1] CLI Layer
架构角色：用户交互入口 / command shell
函数：
  cli.main()

能调用：
  普通输入:
    -> orchestrate_task(history)

  空回车:
    -> _process_lead_inbox(history)
        -> process_orchestrator_inbox(history)
            -> BUS.read_inbox("lead")

  /team:
    -> TEAM.list_all()

  /inbox:
    -> BUS.read_inbox("lead")

  /tasks:
    -> TASKS.list_all()


[2] Orchestrator
架构角色：中心化 control plane / 顶层调度器
函数：
  orchestrate_task(history)

能调用：
  -> choose_route(...)
  -> run_react_loop(...)                 # Direct ReAct 路径
  -> plan_task(...)                      # Plan-and-Execute 路径
  -> _execute_plan_step(...)             # 执行 plan steps
  -> reflect_task(...)                   # Reflection / Reviewer
  -> _format_final_response(...)


[3] Router
架构角色：意图路由器 / decision module
函数：
  choose_route(...)
    -> _choose_route_by_rules(...)
    -> _route_with_llm(...)              # 仅 rules 不确定时
        -> strong model
        -> ROUTER_SYSTEM

输出：
  {
    mode: "react" | "plan_execute",
    reflection_required: bool,
    intent: ...
  }

注意：
  Router 只做决策，不直接执行 Executor / Planner。


[4A] Direct ReAct Executor
架构角色：简单任务执行器 / weak ReAct executor
入口：
  orchestrate_task()
    -> run_react_loop(
         system_prompt=DIRECT_EXECUTOR_SYSTEM,
         tools=EXECUTOR_TOOLS
       )

能调用的工具：
  bash
  read_file
  write_file
  edit_file
  todo
  load_skill
  compact
  background_run
  check_background

内部支持：
  -> micro_compact(...)
  -> auto_compact(...)
  -> BG.drain_notifications()
  -> EXECUTOR_TOOL_HANDLERS[tool_name](...)

不能直接调用：
  TEAM.spawn()
  TASKS.create()
  WORKTREES.create()
  BUS.send()
  plan_approval


[4B] Plan-and-Execute
架构角色：复杂任务执行框架
入口：
  orchestrate_task()
    -> plan_task(...)
    -> for step in plan["steps"]:
         _execute_plan_step(...)
    -> optional reflect_task(...)


[5B-1] Planner
架构角色：strong planning model / 只生成计划，不执行
函数：
  plan_task(...)
    -> strong model
    -> PLANNER_SYSTEM
    -> _normalize_plan(...)

输出 step 类型：
  react_step

  organization-level step:
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


[5B-2] Step Dispatcher
架构角色：plan step 分发器
函数：
  _execute_plan_step(...)

逻辑：
  if step.type == "react_step" or repair:
      -> _run_react_step(...)
  else:
      -> _run_orchestrator_step(...)


[6B-1] Step Executor
架构角色：单步 worker / weak ReAct executor
函数：
  _run_react_step(...)
    -> run_react_loop(
         system_prompt=REACT_EXECUTOR_SYSTEM,
         tools=EXECUTOR_TOOLS
       )

能调用：
  bash / read_file / write_file / edit_file
  todo / load_skill / compact
  background_run / check_background

输入记忆：
  original_user_request
  overall_plan
  current_step
  prior_results

注意：
  这里的 Step Executor 就相当于 Plan-and-Execute 里的 worker。


[6B-2] Orchestrator Action Dispatcher
架构角色：组织级动作执行器 / Python deterministic dispatcher
函数：
  _run_orchestrator_step(step)

能调用：
  step.type == spawn_teammate:
    -> TEAM.spawn(...)
        -> TeammateManager.spawn(...)
            -> threading.Thread(...)
            -> _teammate_loop(...)

  step.type == send_message:
    -> BUS.send("lead", to, content, "message")
    -> TEAM.wake(to)

  step.type == wait_teammate:
    -> _wait_for_teammate(...)
        -> BUS.read_inbox("lead")
        -> BUS.requeue_inbox("lead", unmatched)

  step.type == task_get:
    -> TASKS.get(...)

  step.type == task_create:
    -> TASKS.create(...)

  step.type == task_update:
    -> TASKS.update(...)

  step.type == task_list:
    -> TASKS.list_all()

  step.type == worktree_create:
    -> WORKTREES.create(...)

  step.type == worktree_status:
    -> WORKTREES.status(...)

  step.type == worktree_run:
    -> WORKTREES.run(...)

  step.type == worktree_keep:
    -> WORKTREES.keep(...)

  step.type == worktree_remove:
    -> WORKTREES.remove(...)

  step.type == worktree_list:
    -> WORKTREES.list_all()

注意：
  它不是 agent。
  它不是 worker。
  它是 Orchestrator 在 plan step 中执行组织级动作的 Python 分发器。


[7] Reflection / Reviewer
架构角色：strong task-level reviewer
函数：
  reflect_task(...)
    -> strong model
    -> REFLECTION_SYSTEM

输出：
  verdict: pass | revise | block
  issues: [...]
  repair_steps: [...]

如果 revise:
  orchestrate_task()
    -> _execute_plan_step(..., repair=True)
    -> reflect_task(...) again


[8] Teammate Actor System
架构角色：持久化 teammate actors / 半自主子智能体系统
对象：
  TEAM = TeammateManager(...)

入口来自：
  Orchestrator Action Dispatcher
    -> TEAM.spawn(...)
    -> TEAM.wake(...)

teammate 线程：
  TeammateManager.spawn(...)
    -> threading.Thread(target=_teammate_loop)

teammate loop 能调用：
  _teammate_loop(...)
    -> weak model
    -> _teammate_tools()
    -> _exec(...)

teammate 工具：
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

teammate 状态：
  working
  idle
  waiting_approval
  shutdown

teammate 协议：
  plan_approval
    -> BUS.send(..., "plan_approval_request")
  shutdown_response
    -> BUS.send(..., "shutdown_response")
  idle
    -> poll inbox
    -> scan_unclaimed_tasks()
    -> claim_task(...)


[9] Shared State / Blackboard
架构角色：共享状态层 / blackboard-like memory
对象和文件：
  BUS = MessageBus(...)
    -> .team/inbox/*.jsonl

  TASKS = TaskManager(...)
    -> .tasks/task_*.json

  TEAM config
    -> .team/config.json

  WORKTREES = WorktreeManager(...)
    -> .worktrees/index.json

  EVENTS = EventBus(...)
    -> .worktrees/events.jsonl

  transcripts
    -> .transcripts/transcript_*.jsonl


[10] Skill Progressive Disclosure
架构角色：按需知识披露系统
对象：
  SKILL_LOADER = SkillLoader(SKILLS_DIR)

启动时：
  SkillLoader._load_all()
    -> 扫描 skills/**/SKILL.md
    -> 只把 name / description / tags 暴露给 prompt

Executor 调用时：
  load_skill tool
    -> SKILL_LOADER.get_content(name)

能使用它的层：
  Direct ReAct Executor
  Step Executor

当前 teammate：
  没有 load_skill 工具


[11] Context Compression
架构角色：上下文压缩 / conversation memory compaction
入口：
  run_react_loop()

自动触发：
  micro_compact(messages)
  if estimate_tokens(messages) > THRESHOLD:
      -> auto_compact(messages)

手动触发：
  compact tool
    -> auto_compact(messages, focus=...)

压缩流程：
  auto_compact(...)
    -> 保存 transcript
    -> build_compaction_records(...)
    -> apply_rule_scores(...)
    -> score_records_with_weak_llm(...)
    -> select_recent_records(...)
    -> select_important_records(...)
    -> summarize_with_strong_llm(...)

能使用它的层：
  Direct ReAct Executor
  Step Executor

当前 teammate：
  没有走 run_react_loop，所以没有这套 compression。
```

一句话定版：

> **EcoA 是中心化 Orchestrator 架构：CLI 把用户请求交给 Orchestrator，Orchestrator 先路由；简单任务走 Direct ReAct Executor，复杂任务走 Plan-and-Execute；Plan-and-Execute 中 Planner 生成计划，Step Dispatcher 把 `react_step` 交给 Step Executor，把组织级 step 交给 Orchestrator Action Dispatcher；Action Dispatcher 再调用 TEAM / BUS / TASKS / WORKTREES；Teammate 是 TEAM 管理的持久 actor 子系统；Skill 和 Compression 主要挂在 ReAct Executor 的工具与循环内部。**

## 目前主流 multi-agent 架构

感觉项目整体结构，尤其是 multi-agent 不太对。目前主流架构可以粗略说成：

> **Hierarchical + Tool-based + Workflow：Planner 负责拆任务，Workers 像工具一样执行，Memory 负责共享上下文。**

Tool-based 的意思是：Agent 之间不是主要靠“聊天协商”完成任务，而是把某些能力封装成可调用工具，由中心 Agent / Orchestrator 决定什么时候调用。

但 EcoA **不完全等于这个模板**。

最大的区别**不是没有实现 memory 共享**。其实实现了共享状态，只是它不是那种“统一向量记忆 / 全局语义 memory”。

现在的共享状态包括：

- `.tasks`：任务板，共享任务状态
- `.team/config.json`：teammate 状态
- `.team/inbox/*.jsonl`：消息队列
- `.worktrees/index.json`：worktree 状态
- `.worktrees/events.jsonl`：事件日志
- `.transcripts`：完整历史落盘
- context compression summary：压缩后的连续性记忆

更关键的区别在这里：

| 主流模板 | EcoA |
|---|---|
| Planner 往往是中心 | **Orchestrator 才是中心，Planner 只是生成声明式计划** |
| Workers 常是一次性工具化 agent | **Teammate 是持久 actor，有线程、身份、inbox、状态机** |
| Workflow 通常较固定 | **EcoA 是 rules-first 路由后动态选择 ReAct / Plan-and-Execute / teammate 协作** |
| Memory 通常是共享上下文或向量库 | **EcoA 是文件系统黑板 + inbox + task board + 压缩摘要** |
| Agent 可直接调用很多能力 | **组织级权力收回 Python Orchestrator，Executor 工具受限** |

所以一句话说：

> EcoA 基于 Hierarchical + Tool-based + Workflow 的主流骨架，但进一步引入了中心化 Orchestrator control plane、持久化 teammate actor、协议化 inbox 协作和黑板式共享状态；它不是没有 memory，而是实现了工程状态共享和压缩记忆，还没有做统一语义记忆层。

如果说“最大的不同”：

> **项目的 Workers 不是普通工具化 agent，而是可持久运行、可 idle/wake、可审批、可认领任务的 teammate actors；同时组织级调度权被 Python Orchestrator 控制，而不是交给 LLM 自由协商。**

