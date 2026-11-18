---
name: quota-monitor
description: 实时查询 Antigravity 主控统领的三大 CLI 智能体（AGY-CLI、Codex-CLI、Opus-CLI）及三大底层模型池（Antigravity Gemini、Antigravity Claude/3P、OpenAI Codex）的剩余额度百分比、重置倒计时、Prompt Cache 5m TTL (Cache Tax) 状态与在途任务负载，支持额度感知型智能调度。
---

# Quota Monitor v1.4.0 多智能体实时额度、三核分流与任务级熔断指南

当你需要查看当前各智能体或模型的**剩余额度（Remaining Quota）**、**5小时/7天限流窗口（Rate Limits）**、**重置时间（Reset Time）**，或者在作为 **Antigravity 主控大脑（`Gemini 4 Carbon` / `gemini-4-carbon-high`）** 派发子任务前评估哪个 CLI 子智能体及 Gemini 4 子核最合适时，请使用 `quota-monitor` Mod 提供的工具。

## 1. 三大底层模型配额池与三核协处理器映射关系

| 配额池 ID (`quota_pool`) | 包含智能体 | 底层模型与子核分工 | 限流窗口与缓存机制 | 数据源 |
| :--- | :--- | :--- | :--- | :--- |
| **`antigravity_gemini`** | **`AGY-CLI`** + 伴飞子代理 | `gemini-4-carbon-high`（攻坚重核）/ `gemini-4-argon-high`（正式长文主笔）/ `gemini-4-hydrogen-high\|xhigh\|medium`（常规编码与快反伴飞） | **5小时动态恢复窗口**（无周上限，总容量 > Opus + Sol 之和；`<30%` 自动触发非安全任务护池降档） | `cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota` |
| **`openai_codex`** | **`Codex-CLI`** | `gpt-6.1-sol`（测试验收）/ `gpt-6.1-terra`（异构记忆审查） | **5小时主窗口 (`primary_window`) + 7天周窗口 (`secondary_window`)** | `chatgpt.com/backend-api/wham/usage` + 本地 Session JSONL 兜底 |
| **`antigravity_claude_3p`** | **`Opus-CLI`** | `claude-opus-5-5-high`（架构/高审美 UI）/ `claude-sonnet-5-5`（异构记忆审查） | **支持 Claude Ultra 动态窗口 + 7天周限额保护 + 5m Prompt Cache TTL** | `cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary` |

> **Prompt Cache 失效税（Cache Tax）防控红线**：
> Claude / 3P 池默认采用 5 分钟 Prompt Cache TTL。在同一个长任务周期内，切勿在会话中途频繁增删 MCP 工具定义（避免破坏系统提示词前缀导致全量 Cache 重算）；应在任务启动前通过 `mod_mount(mod_id="@profile")` 一次性挂载所需场景包。

## 2. 核心 MCP 工具与命令行调用方式

### 方式一：直接调用 MCP 工具（推荐）
- **`quota_status`**：
  一次性获取三大 CLI 智能体（`AGY-CLI`、`Codex-CLI`、`Opus-CLI`）的实时剩余额度百分比、重置时间、在途任务数（`in_progress` / `todo`）、`AGY-CLI` 三核映射（`submodels`）以及可直接渲染的 Markdown 状态表（`summary_markdown`）。
- **`quota_recommend_agent`**：
  传入 `task_description`（任务描述）、可选 `preferred_agent`（倾向智能体）与 `min_quota_percent`（最低安全额度阈值，默认 `15%`），返回最优派发智能体（`recommended_agent`）、备选智能体（`fallback_agent`）、**Gemini 4 三核细分路由（`gemini_submodel_routing`）**及**记忆审查异构模型周额度路由（`memory_reviewer_routing`）**。
- **`quota_check_agent`**：
  传入 `agent_name`（如 `"AGY-CLI"`、`"Codex-CLI"` 或 `"Opus-CLI"`），快速检查单个智能体的额度健康度与 `can_dispatch` 状态。

### 方式二：命令行直接查询
```powershell
python -X utf8 mods/quota-monitor/mcp/quota_monitor_mcp.py --cli status
python -X utf8 mods/quota-monitor/mcp/quota_monitor_mcp.py --cli recommend "重构认证模块并排查并发死锁"
```

## 3. Gemini 4 三核协处理器与异构制衡调度策略

1. **主控与 `AGY-CLI` 三核分流（正常水位，Gemini 5h 剩余额度 $\ge 30\%$）**：
   - **唯一总控大脑**：Antigravity 主会话（`Gemini 4 Carbon` / `gemini-4-carbon-high` 或 `gemini-pro-orchestrator`）专注任务拆解与派发调度。
   - **逻辑与攻坚重核 (`gemini-4-carbon-high`, `deep_reasoning_core`)**：跨模块复杂重构、核心算法、深水区 Debug、死锁/竞态、安全漏洞修复与 Mod 自进化。
   - **高熵正式文字与叙事主笔核 (`gemini-4-argon-high`, `prose_engine`)**：`@office` 正式工程报告撰稿、招投标、`@ppt` 叙事大纲策划、`writing_*` / `presentation_planner` / `learning_*` 角色与正式架构复盘文档润色。
   - **常规编码与高频快反核 (`gemini-4-hydrogen-high / xhigh / medium`)**：常规标准模块开发编码与渲染脚本走 `gemini-4-hydrogen-high`（`standard_coding_core`）或 `xhigh`；全库检索摸底、小脚本、配置微调、伴飞机械对账承载与知识蒸馏流式处理走 `gemini-4-hydrogen-medium`（`fast_explore_core`）。
2. **Gemini 5h 护池熔断（`< 30%`）**：
   当 `antigravity_gemini` 池 5h 剩余额度 `< 30.0%` 时，非核心安全/死锁攻坚任务自动降档至 `gemini-4-hydrogen-medium`；核心安全漏洞与死锁/竞态攻坚豁免降档，继续保持 `gemini-4-carbon-high`。
3. **异构制衡与三维记忆伴飞审查**：
   - 测试与验收 $\rightarrow$ **`ChatGPT` (`Codex-CLI`, `gpt-6.1-sol`)** 专门负责单元测试/集成测试运行、红队挑刺与交付验收。
   - 顶层架构与高审美 UI $\rightarrow$ **`Opus-CLI` (`claude-opus-5-5-high`)** 预留复杂顶层架构终审与 OKLCH 高审美 UI 设计。
   - 三维记忆伴飞与高影响复核 $\rightarrow$ 伴飞承载会话采用 `gemini-4-hydrogen-medium`（盲审机械对账）；高影响记忆复核**严禁消耗 `6.1sol` 与 `opus5.5`**，按实时周剩余额度百分比动态择优 `6.1terra` (`gpt-6.1-terra`) 或 `sonnet5.5` (`claude-sonnet-5-5`)。
4. **紧缺/耗尽态（`critical` $< 15\%$ 或 `exhausted` $\le 0.5\%$）**：
   禁止继续向该配额池派发重任务，自动触发跨池热备切换。

## 4. CLI 输出净化与任务预算保护

`quota_purify_cli_output(raw_output, command_hint="auto", max_lines=80, token_budget=1200)`
吸收 rtk-ai/rtk 的输出压缩思路，使用本地标准库处理文本，无外部程序依赖。
支持 `auto`、`pytest`、`git_status`、`git_diff`、`build_log`、`generic`。
移除 ANSI 与回车进度噪声，折叠成功用例及重复日志，保留失败断言、堆栈和统计。
Git diff 按文件统计增加与删除行，保留 hunk；Git status 去重文件列表。
Status 不含实际行数，因此标注 `+0 / -0 (diff counts unavailable in status)`，表示未观察到 diff 行。
净化结果超过行数或 Token 预算时，设置 `budget_circuit_breaker_triggered=true`。
熔断优先保留报错、告警及首尾摘要；返回节省量、压缩率与保留错误行数。
Token 估算为 `max(1, len(text) // 4)`；两个预算参数必须为正整数。

```json
{"name":"quota_purify_cli_output","arguments":{"raw_output":"tests/test_api.py::test_ok PASSED\n=== 1 passed in 0.1s ===","command_hint":"pytest","max_lines":80,"token_budget":1200}}
```

Codex 模型为 `gpt-6.1-sol`。`openai_codex` 池附带
`shared_chatgpt_codex_pool=true` 和
`budget_protection_policy="ChatGPT & Codex 共享额度池保护 + CLI 输出净化降噪"`。
这些字段声明本工具的额度保护策略；额度采集与路由接口保持兼容。

## 5. 高频动作认知镜像

`quota_cognitive_mirror_sniff(events=None, trace_paths=None, min_repeat_threshold=3)`
吸收 Rule of Three 与 cognitive-mirror 的轨迹模式挖掘方法。
显式事件和文件轨迹合并分析；两者均为空时读取 `QUOTA_COGNITIVE_TRACE_STORE`。
支持 JSON 事件数组、单个事件、`{"events": [...]}` 和 JSONL。
环境路径支持单文件或目录；目录只读取直接子级 JSON/JSONL。
只读最多 100 个常规文件，每个不超过 2 MB；跳过符号链接与损坏记录。
路径、数字、哈希、UUID 与临时参数归一化为意图签名。
命令归类为 `cli_command`，纠偏及提示词为 `user_correction`，动作为 `manual_workflow`。
每个高频模式返回计数、累计耗时与最多 3 条样本。
达到阈值后，建议固化到默认规则、Skill 脚本或 MCP 工具。
`proactive_prompt_zh` 提供中文确认文案；嗅探器只生成建议。

```json
{"name":"quota_cognitive_mirror_sniff","arguments":{"events":[{"command":"pytest tests/test_one.py","duration_sec":4},{"command":"pytest tests/test_two.py","duration_sec":5},{"command":"pytest tests/test_three.py","duration_sec":6}],"min_repeat_threshold":3}}
```

## 6. 进展感知型长任务守卫与 180 秒停滞嗅探 (`quota_task_guard`)

`quota_task_guard(action, task_id, max_step_gemini_delta_pct=15.0, max_step_codex_delta_pct=10.0, min_gemini_reserve_pct=10.0, max_reject_rounds=2, stall_timeout_sec=180, pid=None, watch_paths=None)`
为纯 CLI 子进程长链路编排提供**不断健康长任务**的确定性守卫：
1. 启动长任务时执行 `action="start"`（整单总消耗默认不封顶 `max_total_* = None`）。
2. 每当一个 DAG 节点顺利合并完成，执行 `action="advance_step"` **立即归零当前单步消耗增量与打回计数**，跨任意多模块或跨多个 5h 窗口均不断流。
3. 阶段巡检执行 `action="check"`：同一节点无进展空烧超限或连续 `180s` 无日志更新时返回 `decision="circuit_break"`；实时剩余跌破 `10%` 池底余量时返回 `decision="park_for_refill"` 温和挂起等待回血（不报错终止任务）。
4. 验证打回时执行 `action="record_reject"`，同一节点连续达到 `2` 次打回自动返回 `escalate_to="gemini-4-carbon-high"` 强制回炉诊断。
