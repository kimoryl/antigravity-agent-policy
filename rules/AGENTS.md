# Antigravity Agent Policy & Global Runtime (全局运行时、监督适配层、跨 CLI 共享记忆与统一 Mod 挂载规则)

当处理复杂或多阶段工程任务时，遵循 `antigravity-agent-policy` 插件提供的 Global Runtime、Supervisor Engine 监督适配层与统一 Mod 挂载协议（完整流程见 `global-runtime` 技能）：

1. **MCP 服务与适配层工具调用**：
   - 本插件提供核心 MCP 服务 `ap`（包含 `hash_read`、`hash_outline`、`hash_edit`、`cli_worker_dispatch`、`team_message`、`mod_catalog`、`mod_info`、`mod_mount`、`mod_unmount`、`mod_mounted`、`mod_call`、`mod_pack`、`runtime_intake`、`runtime_task`、`runtime_plan`、`runtime_context`、`runtime_knowledge`、`runtime_experience`、`runtime_verify`、`runtime_delivery`、`runtime_result`、`runtime_evidence`、`policy_resolve`、`policy_check_tool`、`policy_record_result`、`dashboard_open` 等工具）。
   - **带锚点哈希校验的文件读写（`hash_read` / `hash_edit`）**：对关键代码区间进行修改时，优先使用 `hash_outline` 或 `hash_read` 获取 4 字符块哈希锚点（`anchor_hash`）及历史共现关联文件提示（`related_files`），再通过 `hash_edit` 进行乐观锁原子写入。
2. **Gemini 4 三核协处理器 + 异构制衡架构（日常默认）与 `/fleet` 并行长任务舰队模式**：
   - **日常默认模式**：
     * **Antigravity 主会话**：担任总控编排器（`gemini-4-carbon-high`）进行任务拆解与派发调度；
     * **`AGY-CLI` 三核分流**：
       1) **逻辑与攻坚重核 (`gemini-4-carbon-high`)**：负责跨模块复杂重构、核心算法、深水区 Debug、安全漏洞修复与架构演化；
       2) **高熵正式文字与叙事主笔核 (`gemini-4-argon-high`)**：负责 `@office` 正式技术报告撰稿、`@ppt` 叙事大纲策划与 `writing_*` / `presentation_planner` / `learning_*` 角色；
       3) **常规编码与高频快反核 (`gemini-4-hydrogen-high / xhigh / medium`)**：负责标准模块编码（`hydrogen-high` / `xhigh`）、全库检索摸底、轻量脚本与伴飞机械对账承载（`hydrogen-medium`）；
     * **`Codex-CLI` (`gpt-6.1-sol`)**：负责单元测试/集成测试运行、异构红队审查与交付验收；
     * **`Opus-CLI` (`claude-opus-5-5-high`)**：负责复杂顶层架构终审与 OKLCH 高审美 UI 设计。
   - **场景化 Mod 预设集（Profiles）与 `@core` 五大核心底座 Mod**：
     * 默认常驻 **`@core` 5 大核心底座 Mod**（位于仓库 `mods/` 目录，可通过 `AGY_MODS_DIR` 覆盖）：`code-intelligence`、`source-connectors`、`ambitious-deep-thinking`、`quota-monitor`、`context7-sdk-verifier`。
     * 执行专项任务时，可通过 `mod_mount(mod_id="@coding" | "@mobile" | "@web" | "@office" | "@automation" | "@ops", agent_id="all", replace=True)` 或 `mod_call` 按需调用扩展 Mod，任务完成后切回 `@core`。
3. **三维正交交叉记忆矩阵（`Project × Modality × Global`）与工作区绑定协议**：
   - **三维正交记忆存储（`05_Agent_Memory_Sync`）**：
     1. **纵向项目实体记忆（`L1_Projects/<project_id>.json`）**：通过 `00_Project_Registry.json` 管理绑定项目与兜底工作台 `workbench_misc`，沉淀项目客观事实与接口契约。
     2. **横向交付工艺记忆（`L1_Modalities/<modality_id>.json`）**：维护 5 大跨项目复用工艺库（`ppt_product_pitch`、`ui_aesthetic_design`、`office_report_doc`、`coding_and_testing`、`systems_and_automation`）。
     3. **顶层全局记忆（`L2_Experiences.json` + `L3_Core_Rules.md`）**：沉淀跨项目安全红线（如 Windows `CREATE_NO_WINDOW`）与受控表达规范。
   - **工作区动态绑定**：调用 `runtime_knowledge(action="project_bind", project_id="<project_id>", project_root="<workspace_path>")` 将工作区持久化绑定至指定项目。
   - **记忆读写分离与异构复核**：主会话与业务 CLI 默认只读消费记忆（`sdk_fact_lookup`）；写入须经机械事实对账（`sdk_fact_reconcile_report` -> `sdk_memory_review_commit`）。伴飞机械对账搭载 `gemini-4-hydrogen-medium`；高影响记忆复核禁用 `6.1sol` 与 `opus5.5`，按周剩余额度择优使用 `6.1terra` (`gpt-6.1-terra`) 或 `sonnet5.5` (`claude-sonnet-5-5`)。
4. **按需记忆伴飞（盲审隔离 Blind Audit）**：
   - 长链路复杂工程研发或重度跨维正式交付启动 `记忆事实伴飞审查员`（仅传递客观产物路径、`changed_files` 与测试退出码，严禁灌输主控主观推理结论）；方案讨论、轻量脚本维护、局部微调与只读查询免伴飞。
5. **确定性监督适配层（Supervisor Engine）与受控技术表达**：
   - 会话遥测（`SessionTelemetry`）拦截死循环重复调用；文件共现图谱（`CoOccurrenceGraph`）提示联动关联文件；证据账本（`EvidenceLedger`）要求实质性代码变更后必须通过测试验证闭环。
   - 面向用户的操作步骤、CLI 命令与排障清单遵循 ASD-STE100 受控技术语法（80% 强度：一词一义、零寒暄、禁用模糊词、主动祈使句、单步单动作；内部思考与复杂架构权衡阶段豁免句长限制）。
