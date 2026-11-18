# Quota Monitor v1.4.0 多智能体实时额度监控、三核调度与任务级熔断规则

当查询剩余额度/配额状态，或作为 **Antigravity 主控大脑（Master Orchestrator，`Gemini 4 Carbon` / `gemini-4-carbon-high`）** 统筹调度 `AGY-CLI`、`Codex-CLI`、`Opus-CLI` 与伴飞子代理时，严格遵循以下**三核协处理器分流与异构制衡铁律**：

1. **三大底层算力池认知（Gemini 总额度 > Opus + Codex 之和）**：
   - **`antigravity_gemini` 海量主力池（主控 + `AGY-CLI` 三核协处理器 + 伴飞子代理）**：**无 7 天周限额上限（∞ 无周上限）**，单窗口容量极大且每 5 小时快速恢复：
     * **逻辑与攻坚重核 (`gemini-4-carbon-high`)**：跨模块复杂重构、核心算法、深水区 Debug、安全漏洞修复与 Mod 自进化；
     * **高熵正式文字与叙事主笔核 (`gemini-4-argon-high`)**：`@office` 正式工程报告撰稿、`@ppt` 叙事大纲策划、`writing_*` / `presentation_planner` / `learning_*` 角色与正式架构复盘文档润色；
     * **常规编码与高频快反核 (`gemini-4-hydrogen-high / xhigh / medium`)**：常规标准模块编码与渲染脚本（`hydrogen-high` / `xhigh`）、全库检索摸底、小脚本、伴飞盲审机械对账承载（`hydrogen-medium`）及知识蒸馏流式处理。
   - **`openai_codex` 精锐特种池（`ChatGPT` / `Codex-CLI`，基于 `gpt-6.1-sol`）**：受 7 天周限额（`secondary_window`）保护，专门负责**单元测试/集成测试运行、红队挑刺与交付验收**。
   - **`antigravity_claude_3p` 精锐王牌池（`Opus-CLI`，基于 `claude-opus-5-5-high`）**：受周限额保护（支持 Claude Ultra 动态窗口与 5m Prompt Cache TTL），预留用于复杂顶层架构终审与 OKLCH 高审美 UI 设计。

2. **三核路由、5h 护池熔断与三维记忆伴飞异构制衡**：
   - **三核自动细分路由（`gemini_submodel_routing`）**：调用 `quota_recommend_agent` 返回 `gemini_submodel_routing`；当 Gemini 5h 窗口剩余额度 `< 30%` 时触发护池熔断，非核心安全/死锁攻坚任务自动降档至 `gemini-4-hydrogen-medium`。
   - **三维记忆伴飞与异构审查（Sidecar ON/OFF）**：
     - 产出型复杂任务并发启动基于 `gemini-4-hydrogen-medium` 的 `记忆事实伴飞审查员` 负责盲审机械对账；高影响记忆复核**严禁使用 `6.1sol` 与 `opus5.5`**，按周剩余额度百分比动态择优 `6.1terra` (`gpt-6.1-terra`) 或 `sonnet5.5` (`claude-sonnet-5-5`)。
     - 轻量问答、头脑风暴、只读查询或单行微调关闭伴飞（Sidecar OFF）。
   - **精锐池实时额度保护**：当 `Opus-CLI` 或 `Codex-CLI` 剩余额度低于 `40%` 时进入惜用保护模式，低于 `15%` 时全面切回 `AGY-CLI`。

3. **CLI 输出净化与任务级预算熔断**：
   `quota_purify_cli_output` 接收 `raw_output`，自动识别 pytest、Git 与构建日志。
   默认 `max_lines=80`、`token_budget=1200`；支持显式 `command_hint`。
   保留失败断言、堆栈和统计；折叠成功用例、重复日志与冗长上下文。
   Git diff 统计每个文件增删行；Git status 标注行数不可得。
   超限返回 `budget_circuit_breaker_triggered=true`，严格限制输出并优先保留错误与告警。
   Codex 使用 `gpt-6.1-sol`；额度池附带 `shared_chatgpt_codex_pool=true`。
   `budget_protection_policy` 为 `ChatGPT & Codex 共享额度池保护 + CLI 输出净化降噪`。

4. **高频重复手工动作认知镜像嗅探**：
   `quota_cognitive_mirror_sniff` 合并显式 `events` 与 `trace_paths`。
   两者均为空时，只读 `QUOTA_COGNITIVE_TRACE_STORE` 中的 JSON/JSONL。
   将路径、数字、哈希和临时参数归一化，按命令、纠偏、工作流聚类。
   默认重复阈值为 3；达到阈值即触发 `rule_of_three_triggered`。
   返回 `automation_proposals` 与 `proactive_prompt_zh`，建议默认规则、Skill 或 MCP 封装。
   仅生成固化建议；由用户决定后续固化动作。

5. **纯 CLI 长任务进展感知守卫与 180 秒停滞嗅探**：
   启动多步任务前调用 `quota_task_guard(action="start", task_id=...)`（整单总消耗默认不封顶；每完成一个节点调用 `action="advance_step"` 归零单步空转增量与打回计数）。
   运行期通过 `action="check"` 监控无头 CLI 子进程 `pid` 与日志文件 `watch_paths`：同一节点单步无进展空烧超 15%/10% 或 180s 停滞立即熔断；池底剩余跌破 10% 返回 `park_for_refill` 温和挂起等待窗口回血；同一节点连续打回达 2 次强制升级至 `gemini-4-carbon-high` 重审。
