# Context Compression 设计与实现记录

日期：2026-04-29

本文记录本轮对话中围绕 EcoA 上下文压缩机制所做的代码阅读、方案讨论、实现修改、测试现象、踩坑与结论。重点不是逐行解释代码，而是保留这次设计演进的逻辑脉络，方便后续继续迭代。

## 1. 初始问题

用户提出：当前 EcoA 的 context window 管理是时间维度的三层压缩。

实际代码阅读后确认：

- 第一层：`micro_compact()`，替换较旧的 tool result，只保留最近若干条工具结果。
- 第二层：超过 `THRESHOLD` 后自动触发 `auto_compact()`。
- 第三层：LLM 主动调用 `compact` 工具，手动触发 `auto_compact()`。

用户希望引入“重要性过滤”的压缩机制，而不是只按时间切掉历史。初步想法是：

- 包含“决定、确认、需求”等关键词的记录加分。
- 第二层和第三层压缩时再做重要性打分，而不是每条消息实时处理。
- 压缩后要把重要内容注入到之前压缩得到的摘要中。

## 2. 代码阅读结论

主要相关文件：

- `ecoa/compression.py`
  - 原本只有 `estimate_tokens()`、`extract_critical()`、`micro_compact()`、`auto_compact()`。
  - `auto_compact()` 会保存完整 transcript，然后把 `json.dumps(messages)[:80000]` 直接交给 strong LLM 摘要。
- `ecoa/agent.py`
  - 第二层自动压缩在 `estimate_tokens(messages) > THRESHOLD` 时触发。
  - 第三层主动压缩在模型调用 `compact` 工具后触发。
  - `compact` 工具已有 `focus` 参数，但原代码没有真正传给 `auto_compact()`。
- `ecoa/config.py`
  - 原本只有 `THRESHOLD`、`KEEP_RECENT` 等简单配置。
- `ecoa/tool_registry.py`
  - `compact` 工具 schema 中已有 `focus` 字段。

一个关键发现是：第二层和第三层最终都走 `auto_compact()`，所以最自然的改法就是直接增强 `auto_compact()`，不需要为自动压缩和主动压缩做两套逻辑。

## 3. 方案讨论

最初提出的方案是把压缩过程拆成一条管线：

```text
auto_compact(messages, focus="")
  1. 保存完整 transcript
  2. 提取 previous_summary
  3. 展开 messages 为可评分 records
  4. 规则打分
  5. weak LLM 复评候选 records
  6. 选出 important_records 和 recent_records
  7. strong LLM 合并旧摘要和重要内容
  8. 返回压缩后的两条 messages
```

用户追问：为什么不直接在 `compression.auto_compact()` 里加入重要性过滤，让第二层和第三层都复用？

回应与结论：

- 这个想法是正确的。
- 第二层和第三层只负责触发。
- 重要性过滤应该统一放进 `auto_compact()`。
- 压缩结果应该是“旧摘要 + 本轮重要内容 + 最近上下文”合并后的新摘要，而不是简单把重要内容硬拼在摘要后面。

用户进一步提出：第一次应该用 weak LLM 做混合规则打分，第二次用 strong LLM 总结。

回应与结论：

- 认可这个方向。
- 但 weak LLM 不应该单独决定丢弃什么。
- 更稳的生产逻辑是：规则打分提供安全底线，weak LLM 只做语义复评和补分，strong LLM 负责最终摘要合并。
- 如果 weak LLM 失败，应降级为纯规则打分，不能阻断压缩流程。

## 4. Records 展开设计

为了让压缩不是处理一个巨大字符串，而是处理可评分的语义单元，新增了 records 展开逻辑。

每个 record 大致包含：

```python
{
    "id": "m12.b0",
    "message_index": 12,
    "block_index": 0,
    "role": "user",
    "kind": "user_text",
    "tool_name": None,
    "tool_use_id": None,
    "text": "...",
    "char_count": 1234,
    "is_recent": True,
    "rule_score": 0,
    "llm_score": None,
    "final_score": 0,
    "reasons": [],
    "protected": False,
}
```

展开规则：

- `content` 是普通字符串时，生成一条 text record。
- assistant 的 `tool_use` block 生成 `tool_use` record。
- user 的 `tool_result` block 生成 `tool_result` record，并通过 `tool_use_id` 反查工具名。
- 压缩产生的旧 summary 不作为普通 record，而是提取为 `previous_summary`。

对应实现：

- `build_compaction_records()`
- `_build_tool_name_map()`
- `_block_type()`
- `_block_attr()`
- `_block_text()`

## 5. 规则打分设计

规则打分的目标不是完美理解语义，而是建立“不能轻易丢”的底线。

主要加分项：

- 用户消息。
- 最近消息。
- 最后一条用户请求。
- 包含明确需求、确认、决定、约束、禁止、保留、实现、修复等关键词。
- 包含 plan approval、approve、reject、next_action 等协议关键词。
- 包含 Error、Traceback、failed、blocked、异常、报错、阻塞等错误或阻塞关键词。
- 包含 TODO、pending、in_progress、completed、当前状态、下一步等状态关键词。
- 包含文件路径、函数名、类名、配置项等代码引用。
- 与 `compact` 的 `focus` 匹配。

主要降分项：

- 旧 tool result placeholder，例如 `[Previous: used ...]`。
- 超长且没有错误信号的普通 tool output。
- 空的 `read_inbox` 结果。

硬保护项：

- 最近记录。
- 最后一条用户请求。
- 用户明确需求或约束。
- 错误、阻塞、协议记录。

对应实现：

- `apply_rule_scores()`
- `_contains_any()`
- `_focus_terms()`

## 6. Weak LLM 混合打分

在规则打分后，只把候选 records 交给 weak LLM，而不是全量历史。

候选选择逻辑：

- 受保护记录。
- 最近记录。
- 规则分大于 0 的记录。
- 最多 `COMPACT_LLM_CANDIDATE_LIMIT` 条。

weak LLM 返回 JSON：

```json
{
  "scores": [
    {
      "id": "m12.b0",
      "score": 5,
      "category": "user_requirement",
      "reason": "explicit user constraint"
    }
  ]
}
```

最终分数计算：

```text
final_score = rule_score + llm_score * 2
```

如果 weak LLM 调用失败、返回非 JSON、JSON 解析失败或其他异常：

- 捕获异常。
- 打印日志。
- 返回 `{}`。
- 后续 `apply_llm_scores(records, {})` 不做任何修改。
- 压缩流程继续使用规则分。

对应实现：

- `select_llm_scoring_candidates()`
- `score_records_with_weak_llm()`
- `apply_llm_scores()`

## 7. Important Records 选择

最终输入 strong LLM 的上下文分为两类：

- `recent_records`：最近若干条记录，强制保留。
- `important_records`：从较旧历史中选出的高重要性记录。

选择顺序：

1. 先加入 protected records。
2. 再按类别补充：
   - requirement_or_decision
   - protocol
   - error_or_blocker
   - state_or_todo
   - code_reference
   - focus_match
3. 再加入 `final_score >= COMPACT_IMPORTANCE_MIN_SCORE` 的高分记录。
4. 最终按原始时间顺序排序，避免摘要模型读到乱序历史。

对应实现：

- `select_recent_records()`
- `select_important_records()`
- `_format_records()`

## 8. Strong LLM 摘要合并

strong LLM 的职责不是简单总结全量对话，而是合并：

- `previous_summary`
- `important_records`
- `recent_records`
- `critical_context`
- `TODO.render()`
- `transcript_path`
- `focus`

摘要 prompt 要求：

- 新决定覆盖旧决定。
- 保留用户明确需求、确认过的决定、当前状态、重要文件/函数/配置、关键错误、阻塞、下一步。
- 丢弃重复 polling、原始 dump、陈旧 tool output。
- 输出结构化的连续性摘要。

对应实现：

- `summarize_with_strong_llm()`

如果 strong LLM 摘要失败：

- 打印错误。
- 用旧摘要、important records、recent records 组成 fallback summary。

## 9. 实际代码改动

### `ecoa/config.py`

新增环境变量读取辅助函数：

- `_env_int()`
- `_env_bool()`

新增压缩配置：

- `COMPACT_KEEP_RECENT_MESSAGES`
- `COMPACT_IMPORTANCE_MIN_SCORE`
- `COMPACT_MAX_IMPORTANT_RECORDS`
- `COMPACT_LLM_CANDIDATE_LIMIT`
- `COMPACT_INPUT_CHAR_BUDGET`
- `COMPACT_MAX_RECORD_CHARS`
- `COMPACT_USE_WEAK_LLM_SCORING`

### `ecoa/compression.py`

主要新增：

- 关键词组和正则：
  - `REQUIREMENT_KEYWORDS`
  - `PROTOCOL_KEYWORDS`
  - `ERROR_KEYWORDS`
  - `STATE_KEYWORDS`
  - `FILE_REF_RE`
  - `SYMBOL_RE`
- 旧摘要提取：
  - `extract_previous_summary()`
- records 展开：
  - `build_compaction_records()`
- 规则打分：
  - `apply_rule_scores()`
- weak LLM 打分：
  - `score_records_with_weak_llm()`
- important/recent 选择：
  - `select_recent_records()`
  - `select_important_records()`
- strong LLM 摘要合并：
  - `summarize_with_strong_llm()`

`auto_compact()` 被改为统一执行：

```text
save transcript
extract previous summary
build records
rule score
weak LLM score
select recent and important records
strong summary merge
return compressed messages
```

### `ecoa/agent.py`

第三层主动压缩现在会读取 `compact` 工具的 `focus` 参数：

```python
manual_compact_focus = str(block_input.get("focus", "") or "")
messages[:] = auto_compact(messages, focus=manual_compact_focus)
```

## 10. 测试提示词与现象

曾提供几组测试提示词，用于验证：

- 主动压缩能保留用户需求。
- 新决定能覆盖旧决定。
- 错误和阻塞能保留。
- 关键函数和文件路径能保留。
- 最近上下文能强制保留。

测试中用户使用了如下提示词：

```text
请记住一个关键阻塞：当前 auto_compact 的风险是 weak LLM 打分失败时不能阻断压缩流程，必须降级为规则打分。错误关键词：Traceback: weak scoring JSON parse failed。请调用 compact 工具压缩，focus 填“errors and blockers”。
```

观察到：

- `compact` 被调用。
- `auto_compact()` 保存了 transcript。
- 但压缩后模型继续调用了 `bash`、`read_file`、`check_background` 等工具。

后续询问：

```text
刚才记录的关键阻塞和 Traceback 是什么？失败时应该怎么降级？
```

模型能够回答出：

- 关键阻塞是 weak LLM 打分失败时不能阻断压缩流程。
- 错误关键词是 `Traceback: weak scoring JSON parse failed`。
- 失败时应捕获异常，返回 `{}`，降级为规则打分，后续流程继续。

这说明重要信息确实进入了压缩摘要。

## 11. 踩过的坑

### 11.1 主动 compact 后继续执行工具

现象：

```text
> compact:
Compressing...
[manual compact]
[transcript saved: ...]

> bash:
...
```

原因：

- `agent.py` 中手动压缩后没有 `return`。
- ReAct loop 会继续下一轮。
- 压缩后的两条消息包含 assistant 确认语。
- 模型把摘要里的“关键阻塞 / errors and blockers”理解成还需要继续检查的任务。

判断：

- 这不一定是正常使用中的 bug。
- 模型自己主动 compact 时，压缩后继续执行是合理的。
- 不应该为了测试场景专门加入 `after_compact="finish"` 之类参数。

### 11.2 压缩后模型的自我认知断层

压缩后 active history 被替换为两条消息：

1. user message：压缩摘要。
2. assistant message：确认已恢复压缩上下文。

因此模型可能说“我没有调用过 compact”，因为它只看到摘要，不再直接看到原始 tool call。

判断：

- 这是压缩机制的自然副作用。
- 不需要为了测试专门优化。
- 但摘要可以更明确地记录重要事件和约束。

### 11.3 摘要把记忆误写成待执行任务

更核心的问题是：摘要可能把“用户要求记住的风险/阻塞/约束”写成“当前要处理的问题”。

这会诱导模型压缩后继续查代码。

最终决定修这个点，因为它不是测试特例，而是正常长会话中也可能出现的生产风险。

### 11.4 回复污染

在一次最终回复中，误混入了一段无关文本。随后检查整个工作区，没有发现该文本被写入任何文件。

结论：

- 这是回复层面的污染，不是代码或文件污染。
- 后续汇报命令输出时需要更谨慎，避免把非命令输出内容混入结果描述。

## 12. 最终补充修复

用户明确表示：

- 不希望为了测试而优化 active history 被替换为两条压缩消息的问题。
- 不希望因为用户明确要求调用 compact，就给 compact 增加参数。
- 希望按正常使用逻辑判断是否需要修复。

回应：

- 不修“压缩后继续执行”的控制流。
- 不给 `compact` 增加参数。
- 修摘要语义，让“记忆事实”和“待执行任务”分得更清楚。

最终修改：

### Strong summary prompt

新增要求：

```text
Treat this as memory reconstruction, not as a new user request.
Do not convert remembered constraints, risks, or blockers into action items unless the records explicitly ask the agent to act on them now.
```

摘要结构新增：

```text
Remembered constraints or risks
```

并要求：

```text
Put an item under Open tasks or Next steps only when it is explicitly an unfinished action, not merely something the user asked to remember.
```

### 压缩后的 assistant 确认语

从：

```text
Understood. I have the context from the summary. Continuing.
```

改为：

```text
Understood. I have restored the compressed context. This summary is memory, not a new user request.
```

这样不改变正常 compact 后继续执行的逻辑，但能降低摘要诱导模型误行动的概率。

## 13. 验证

执行过：

```powershell
python -m py_compile .\ecoa\compression.py .\ecoa\agent.py .\ecoa\config.py
python -m py_compile .\ecoa\compression.py
git diff --check
```

还运行过一个本地纯函数测试，确认：

- records 能展开。
- 规则能打分。
- recent records 能选出。
- important records 逻辑能跑通。
- `extract_previous_summary()` 能去掉旧摘要中的 `<todos>` 块。

`git diff --check` 没有空白错误，只出现 Git 关于 LF/CRLF 转换的提示。

## 14. 当前结论

本轮最终形成的压缩设计是：

```text
第二层自动压缩
第三层主动压缩
        |
        v
统一进入 auto_compact()
        |
        v
保存完整 transcript
        |
        v
规则打分建立安全底线
        |
        v
weak LLM 对候选 records 复评
        |
        v
选出 important_records + recent_records
        |
        v
strong LLM 合并旧摘要和重要内容
        |
        v
返回压缩后的两条 messages
```

关键设计原则：

- 完整 transcript 永远落盘。
- 规则打分负责不丢关键内容。
- weak LLM 只辅助语义判断，不单独决定丢弃。
- weak LLM 失败时降级为规则打分。
- strong LLM 负责合并旧摘要与重要内容。
- 摘要必须区分“记忆中的约束/风险/阻塞”和“真正待执行的下一步”。
- 第二层和第三层复用同一套 `auto_compact()`，避免逻辑分叉。

## 15. 后续可考虑的方向

后续如果继续迭代，可以考虑：

- 给重要性 records 增加调试日志开关，便于观察每次压缩保留了什么。
- 把 prompt 从 `compression.py` 抽到 `prompts.py`，方便维护。
- 为 `build_compaction_records()`、`apply_rule_scores()`、`select_important_records()` 增加单元测试。
- 在 transcript 旁边保存一份 `compaction_debug_*.json`，记录每个 record 的 rule_score、llm_score、final_score 和 reasons。
- 观察多轮压缩后 summary 是否越来越长，必要时增加 summary budget 或二次精简逻辑。
