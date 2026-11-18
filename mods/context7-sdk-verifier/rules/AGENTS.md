# Context7 SDK Verifier v1.1.0：三维交叉记忆伴飞规则

## 1. 三维交叉记忆矩阵（Project × Modality × Global）

- **Project：纵向项目真值**。读取 `L1_Projects/<project_id>.json` 的事实、接口契约、记忆与经验。
- **Modality：横向交付工艺**。读取 `L1_Modalities/<modality_id>.json` 的有效规则、预设、记忆与经验。
- **Global：全局边界**。读取 `L2_Experiences.json` 的有效经验及 `L3_Core_Rules.md` 的规则条目。

五类标准工艺为 `ppt_product_pitch`、`ui_aesthetic_design`、`office_report_doc`、`coding_and_testing`、`systems_and_automation`。通过 `modalities` 显式指定，通过 `task_intent` 补充推断；两者合并去重，无匹配时默认代码与测试工艺。

## 2. Sidecar ON / OFF

- **Sidecar ON：所有产出型、交付型任务**。包括代码编写、重构、依赖变更、测试修复、产品介绍、路演 PPT、UI 页面与组件、正式工程报告（Word/Excel/PDF）及其他正式文档交付。
- **Sidecar OFF：轻量交互**。包括日常问答、尚未制作交付物的头脑风暴、只读配额/磁盘/状态查询及单行文案微调。

任务是否产生正式交付物决定触发状态。文档、PPT、UI 与工程报告不再按“未修改源码”豁免。

## 3. 读写分离、盲审隔离与异构模型周额度择优

执行 Agent 只读调用 `sdk_fact_lookup`，消费《三维交叉作战简报（Cross-Dimension Briefing Card）》。禁止执行 Agent 主观自报并直接改写共享项目或工艺记忆。跨模块复杂需求拆解 DAG 后须先经人工确认（STOP & WAIT）。

伴飞审查员（`Role="记忆事实伴飞审查员"`，承载会话默认采用 `gemini-4-hydrogen-medium` 执行盲审机械对账）遵循以下 5 项硬约束：

1. **机械核对优先**：优先验证物理清单、锁文件、SHA256、静态 AST 接口签名与 MCP Schema（支持 `mechanical_only=True` 拒绝主观判断入库）。
2. **异构复核与模型择优（禁用 `6.1sol` 与 `opus5.5`）**：记忆审查**严禁使用 `6.1sol` (`gpt-6.1-sol`) 和 `opus5.5` (`claude-opus-5-5`)**；统一对比 `6.1terra` (`gpt-6.1-terra`) 与 `sonnet5.5` (`claude-sonnet-5-5`) 的实时周剩余额度百分比（`reviewer_routing`），**哪个周额度剩的多用哪个**。高影响条目支持 `require_heterogeneous_review=True` 强制异构复核。
3. **来源版本与重验周期（TTL）**：每条写入自动记录 `source_revision`、`reviewer_model`、`verification_mode`，支持通过 `ttl_days` 生成 `reverify_after` 重验时间戳并自动过滤过期 `expires_at` 条目。
4. **盲审上下文隔离（Blind Audit）**：审查会话只接收客观产物（代码路径、清单、哈希、测试结果），严禁主控灌输主观推理过程。

禁止动态导入未知目标脚本。可通过 Context7 补充官方文档证据。

## 4. 两阶段对账与三维分流

1. 调用 `sdk_fact_reconcile_report` 获取对账哈希 `reconcile_hash` 及周额度最优审查模型 `reviewer_routing`。
2. 核对报告与产物的客观证据（高影响条目交由周额度更充裕的 `6.1terra` 或 `sonnet5.5` 复核）。
3. 调用 `sdk_memory_review_commit` 提交核验结果（携带 `reconcile_hash` 与 `reviewer_model`）。

提交必须携带签发的 `reconcile_hash`。输入与目标对应如下：

| 输入 | 持久化目标 |
| --- | --- |
| `verified_facts`、`verified_memories`、`verified_experiences` | `L1_Projects/<project_id>.json` |
| `verified_modality_rules` | `L1_Modalities/<mid>.json` 的 `craft_rules` |
| `verified_design_presets` | 同文件的 `design_presets` |
| `verified_modality_memories` | 同文件的 `memories` |
| `promote_to_global=True` 且符合晋升条件的经验 | `L2_Experiences.json` |

使用 `modality_id`、`modalities` 或条目内的 `modality_id` 路由。只有单一目标时才可省略条目目标；多个目标须逐条指定。ID 仅允许字母、数字、下划线、点与连字符，拒绝 `.`、`..` 和路径分隔符。

## 5. 双时间轴、归档与镜像

规则按 `rule_id` 合并，预设按 `preset_key` 合并，记忆按 `memory_id` 合并。`supersedes` / `supersede_ids` 使旧规则或记忆失效，记录 `invalid_at` 与 `superseded_by`。检索只返回有效项。

项目有效事实/记忆上限为 80/50；工艺有效规则/记忆上限为 60/50。历史与超额普通条目分别归档至 `L1_Projects/archive/` 和 `L1_Modalities/archive/`。保留 `hard_boundary`；受保护项单独超限时拒绝提交。

提交在校验完成后逐文件原子替换，并同步主路径与镜像：

- `~/.antigravity-agent-policy/05_Agent_Memory_Sync`
- `~/.antigravity-agent-policy/05_Agent_Memory_Mirror`

模态提交会创建缺失的镜像目录。多文件提交不提供整体事务回滚；失败返回 `ok=False`、错误与 `written_paths`，由审查员核查已完成路径。
