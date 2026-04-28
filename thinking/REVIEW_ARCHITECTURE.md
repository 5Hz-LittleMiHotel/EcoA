# Production-Grade Architecture Review: Plan-and-Execute + ReAct + Reflection

**Date:** 2025-07-17  
**Scope:** `ecoa/agent.py`, `ecoa/orchestrator.py`, `ecoa/planner.py`, `ecoa/reflection.py`  
**Type:** Static architecture review (no code changes)

---

## Executive Summary

The EcoA architecture implements a clean, modular Plan-and-Execute + ReAct + Reflection pattern with well-separated responsibilities. The four core modules (`agent.py`, `orchestrator.py`, `planner.py`, `reflection.py`) have clear interfaces and bounded loops. However, four systemic concerns would prevent this system from reaching production readiness:

| # | Concern | Severity | Scope |
|---|---------|----------|-------|
| 1 | **No LLM API error handling** in any module | 🔴 Critical | All 4 modules |
| 2 | **No model configuration fallback chain** anywhere | 🟠 High | All 4 modules |
| 3 | **Fragile cross-module private-API coupling** | 🟡 Medium | `reflection.py` → `planner.py` |
| 4 | **Discarded second reflection verdict** after repair | 🟡 Medium | `orchestrator.py` |

---

## Detailed Findings

### 1. Responsibility Boundaries — Assessment: ✅ Mostly Clean

| Module | Role | Assessment |
|--------|------|------------|
| `agent.py` | ReAct loop execution (tool dispatch, LLM calls, compression) | ✅ Clean. Dependency injection on `run_react_loop()` parameters. ⚠️ Minor: `agent_loop()` defaults to `"weak"` model — a forgotten override silently reduces quality. |
| `orchestrator.py` | Plan-and-Execute flow orchestration | ✅ Clean delegation to `agent.py`, `planner.py`, `reflection.py`. ⚠️ Minor: imports `latest_assistant_text` from `agent.py` (internal helper) and duplicates text-extraction logic found in `planner.py`. |
| `planner.py` | Plan generation | ✅ Clean single public function `plan_task()`. ⚠️ Minor: private helpers `_extract_text` and `_parse_json_object` are imported by `reflection.py` (see coupling issue below). |
| `reflection.py` | Reflection & repair step generation | ✅ Clean single public function `reflect_task()`. ⚠️ Cross-module coupling via private API (see below). |

**Coupling Issue (Medium Severity):**  
`reflection.py` imports `from .planner import _extract_text, _parse_json_object`. These are private helpers of `planner.py`. This creates a fragile dependency:

- Any signature/behavior change to these helpers in `planner.py` breaks `reflection.py`.
- These helpers are **utility functions** (text extraction from LLM responses, JSON parsing with recovery) — they belong in a shared utility module (e.g., `ecoa/utils.py` or `ecoa/llm_utils.py`).
- Additionally, `orchestrator.py` has its own `_message_text` helper that duplicates `_extract_text`'s functionality, confirming the need for a shared utility layer.

**Recommendation:** Extract `_extract_text` and `_parse_json_object` (and any other shared helpers) into a dedicated utility module. Both `planner.py` and `reflection.py` should import from that shared location.

---

### 2. Loop Risks — Assessment: ✅ Low Risk (Well-Controlled)

| Module | Loop Mechanism | Guard | Risk Level |
|--------|----------------|-------|------------|
| `agent.py` | Tool-calling loop (`while`) | `MAX_TOOL_ROUNDS = 40` hard cap | ✅ LOW |
| `agent.py` | Inbox polling | `EMPTY_INBOX_STALL_THRESHOLD = 6` | ✅ LOW |
| `agent.py` | Todo reminder | `rounds_since_todo` counter, non-blocking | ✅ LOW |
| `orchestrator.py` | Step iteration (`for`) | Bounded by plan steps (original + repair) | ✅ LOW |
| `orchestrator.py` | Reflection repair cycle | Single pass — second reflection verdict is **not** checked | ✅ LOW (but see finding below) |
| `planner.py` | None | N/A | ✅ NONE |
| `reflection.py` | None | N/A | ✅ NONE |

**Total steps are unbounded** (Minor): While each individual loop is bounded, there is no global cap on total LLM calls across original steps + repair steps. A plan with 12 steps + 6 repair steps = 18 step executions, each with up to 40 tool rounds. This could produce excessive costs in production.

**Discarded second reflection (Medium Severity):**  
After executing repair steps, the orchestrator calls `reflect_task()` again but **does not act on the verdict**. If the second reflection returns `"revise"` (indicating the repair was insufficient), this feedback is silently ignored. This is a partial defect — the repair cycle should either:
- Loop with a max-retry budget (e.g., up to 2-3 repair cycles), or
- Log/deliver the second reflection's verdict and issues even if not acting on them.

**Recommendation:** Add a configurable `max_repair_cycles` parameter (default 1) to `orchestrate_task()` and implement bounded looping. At minimum, surface the second reflection's issues in the final response.

---

### 3. Model Configuration Fallback — Assessment: 🔴 Critical Gap

**Finding: No module implements any form of model fallback.** Every module performs a single `get_model_profile(name)` call, with no fallback chain, no retry, and no escalation mechanism.

| Module | Profile Used | Fallback? | What Happens on Failure |
|--------|-------------|-----------|------------------------|
| `agent.py` (`run_react_loop`) | `model_profile` parameter (caller provides; `agent_loop()` defaults to `"weak"`) | ❌ None | Exception propagates → loop terminated |
| `orchestrator.py` (`_run_step`) | Hardcoded `"weak"` | ❌ None | Exception propagates → orchestration fails |
| `planner.py` (`plan_task`) | `model_profile` parameter (default `"strong"`) | ❌ None | Exception propagates → planning fails |
| `reflection.py` (`reflect_task`) | `model_profile` parameter (default `"strong"`) | ❌ None | Exception propagates → reflection fails |

**Risks:**
- **Single point of failure:** If `"weak"` or `"strong"` is misconfigured in `config.py`, all calls fail.
- **No transient-failure resilience:** A 429 rate limit or 5-minute network blip kills the entire task.
- **No graceful degradation:** Failed step execution cannot be retried with a stronger model. Failed planning/reflection cannot fall back to a simpler model.
- **No circuit breaker:** Repeated failures would hammer the API with no backoff.

**Recommendation:** Implement a fallback chain at the `get_model_profile()` level in `config.py`:
1. Try the requested profile name.
2. If unavailable, try a fallback profile (e.g., `"weak"` for `"strong"` failures, or a generic `"default"`).
3. Return a structured error/None if all profiles fail, rather than raising.
   
   Additionally, add per-call retry logic with exponential backoff for transient API errors (at minimum in `agent.py`'s `run_react_loop`, which is the most latency-sensitive component).

---

### 4. Error Handling — Assessment: 🔴 Critical Gap

**Finding: Only "happy-path" errors are handled (bad LLM output). "Unhappy-path" errors (API failures) are unhandled in all modules.**

#### What IS handled:
| Scenario | Handler | Module |
|----------|---------|--------|
| Tool execution throws exception | `try/except` around `handler(**block.input)` → returns `f"Error: {e}"` | `agent.py` |
| JSON parsing fails (LLM returned bad JSON) | `try/except` → `_fallback_plan()` or `_fallback_reflection()` | `planner.py`, `reflection.py` |
| Plan/reflection structure is invalid | `_normalize_plan()` / `_normalize_reflection()` defensive `.get()` calls | `planner.py`, `reflection.py` |
| Empty or missing step results | `_select_deliverable_results()` graceful fallback | `orchestrator.py` |

#### What is NOT handled:
| Scenario | Impact | Where |
|----------|--------|-------|
| LLM API call throws (network timeout, 429, 401, 500) | ❌ Unhandled exception → process crash | **All modules** — no `try/except` around `profile.client.messages.create(...)` |
| `get_model_profile()` raises (profile not found) | ❌ Unhandled exception → process crash | All modules |
| `_run_step()` in orchestrator throws | ❌ Unhandled — no `try/except` in step loop | `orchestrator.py` |
| `plan_task()` or `reflect_task()` throws | ❌ Unhandled — no `try/except` in orchestrator | `orchestrator.py` |
| Tool handler returns unexpected type | ❌ No validation of tool result type before `str()` conversion | `agent.py` |

**The critical missing pattern across all four modules:**  
```python
# Current (everywhere):
response = profile.client.messages.create(...)

# Needed (everywhere):
try:
    response = profile.client.messages.create(...)
except APIError as e:
    # Log, retry with backoff, or escalate
    ...
```

**Recommendation:**
1. **All LLM API calls** (`profile.client.messages.create(...)`) should be wrapped in `try/except` blocks. At minimum, catch broad `Exception`, log the error, and either:
   - Retry with exponential backoff (for transient failures like 429/503), or
   - Call a fallback mechanism (e.g., `_fallback_plan()` for planning, `_fallback_reflection()` for reflection), or
   - Re-raise with a wrapped context error.
2. **The orchestrator** should wrap its three external calls (`plan_task`, `_run_step` loop, `reflect_task`) in a top-level `try/except` that produces a graceful error response rather than crashing.
3. **Add a shared LLM API utility** that handles retries, timeouts, and error normalization once, rather than repeating the pattern in four modules.

---

## Cross-Cutting Concerns

### Configuration Inconsistency
- `agent.py` (via `agent_loop()`) defaults to `"weak"`
- `orchestrator.py` hardcodes `"weak"` for step execution
- `planner.py` defaults to `"strong"` for planning
- `reflection.py` defaults to `"strong"` for reflection

This means planning and reflection use the most expensive model, while execution uses a cheaper one — which is a **sensible default** but is **not documented or configurable centrally**. A production system should define these profiles in a single configuration table (e.g., `config.model_roles = {"planner": "strong", "executor": "weak", "reflector": "strong"}`).

### Code Duplication
- Text/response extraction: `_extract_text` (planner.py), `_message_text` (orchestrator.py), and an inline pattern in reflection.py — three implementations of the same concept.
- JSON parsing with recovery: `_parse_json_object` (planner.py) is reused by reflection.py via private import — should be in a utility module.

---

## Recommendations Summary

| Priority | Recommendation | Effort | Affected Modules |
|----------|---------------|--------|-----------------|
| 🔴 P0 | Wrap all LLM API calls in `try/except` with retry + fallback | Medium | All 4 modules |
| 🔴 P0 | Implement model profile fallback chain in `config.get_model_profile()` | Small | `config.py` + all callers |
| 🟠 P1 | Extract shared utilities (`_extract_text`, `_parse_json_object`) into `ecoa/utils.py` | Small | `planner.py`, `reflection.py` |
| 🟠 P1 | Add total-step cap to orchestrator (e.g., `max_total_steps=30`) | Small | `orchestrator.py` |
| 🟠 P1 | Act on second reflection verdict (bounded multi-cycle repair) | Medium | `orchestrator.py` |
| 🟡 P2 | Document/default model roles in a central config table | Small | `config.py` |
| 🟡 P2 | Add input validation for tool handler return types | Small | `agent.py` |
| 🟢 P3 | Remove default `"weak"` in `agent_loop()` — require explicit profile | Tiny | `agent.py` |

---

## Conclusion

The EcoA architecture has a **solid structural foundation**: clean module boundaries, safe bounded loops, and excellent defensive parsing/normalization of LLM output. However, it has **two critical gaps** that must be addressed before production deployment:

1. **No resilience to LLM API failures** — every external API call is a single point of failure with no retry, fallback, or error recovery.
2. **No model configuration fallback chain** — if a configured model profile is unavailable, the entire system fails with an unhandled exception.

These gaps affect all four modules uniformly and share a common remediation path: wrapping LLM API calls with error handling and implementing fallback-aware model resolution at the config layer. The remaining findings (private-API coupling, discarded reflection verdict, code duplication) are medium-severity concerns that should be addressed in a subsequent refactoring pass.

**Overall readiness for production:** ⚠️ **Not ready** — the two critical gaps create unacceptable reliability risk. Estimated remediation effort: 1-2 days for the critical items, 2-3 days for all recommendations.
