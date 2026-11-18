---
name: sdk-memory-sidecar
description: v1.1.0 三维交叉记忆伴飞（Project × Modality × Global）。所有产出型任务执行只读预检、独立静态/产物核验与双端原子分流提交；轻量问答、头脑风暴、只读查询关闭伴飞。
---

# 三维交叉记忆伴飞（sdk-memory-sidecar v1.1.0）

## 1. 触发规则

**Sidecar ON**：所有产出型、交付型任务，包括代码与测试、产品介绍与路演 PPT、UI 页面与组件、Word/Excel/PDF 正式工程报告、其他正式文档交付。

**Sidecar OFF**：轻量问答、初步头脑风暴、只读状态/配额/磁盘查询、单行文案微调。正式文档与演示文稿不再因未修改源码而豁免。

Master Orchestrator（`Gemini 4 Carbon` / `gemini-4-carbon-high`）为 ON 任务并行启动 `gemini-4-hydrogen-medium`（高频快反与盲审机械对账核），角色为“记忆事实伴飞审查员”。执行 Agent（`AGY-CLI` 三核协处理器：`carbon-high` 攻坚 / `argon-high` 正式长文主笔 / `hydrogen-high` 常规编码）只读消费记忆，独立审查员负责机械对账，高影响记忆交由周额度更充裕的 `6.1terra` 或 `sonnet5.5` 异构复核后提交。

## 2. Pre-Flight：装配三维交叉简报

```python
sdk_fact_lookup(
    project_root=".",
    modalities=["ppt_product_pitch", "ui_aesthetic_design"],
    task_intent="为 mods 项目制作产品介绍 PPT 与 UI 设计",
)
```

显式工艺与意图推断合并去重；字符串工艺 ID 也可直接传入。标准工艺包括 `ppt_product_pitch`、`ui_aesthetic_design`、`office_report_doc`、`coding_and_testing`、`systems_and_automation`。无匹配时默认 `coding_and_testing`。

返回的 `cheat_sheet_markdown` 分三节：

1. **项目客观真值**：`L1_Projects/<id>.json` 的事实、记忆、经验与匹配清单。
2. **交付工艺与审美准则**：`L1_Modalities/<mid>.json` 的规则、预设、记忆与经验。
3. **全局安全与表达底线**：`L2_Experiences.json` 的经验及 `L3_Core_Rules.md` 的规则条目。

通过 `symbols` / `packages` 过滤项目事实，通过 `query_context7=True` 显式启用官方文档查询。模态与全局约束不受 SDK 检索词过滤。所有维度过滤 `invalid_at` 失效项。
## 3. Post-Task：盲审隔离、机械核对与异构复核（6.1terra vs sonnet5.5）

1. **盲审上下文隔离（Blind Audit）**：仅向伴飞审查员传入客观产物路径、`changed_files` 与测试结果，严禁传入主控主观推理结论。
2. **机械核对优先**：核对物理清单、锁文件、SHA256、静态 AST 接口签名与 MCP Schema（支持 `mechanical_only=True`）。
3. **异构审查模型周额度动态择优（严禁使用 `6.1sol` 与 `opus5.5`）**：调用 `sdk_fact_reconcile_report` 获取 `reconcile_hash` 与 `reviewer_routing`，自动对比 `6.1terra` (`gpt-6.1-terra`) 与 `sonnet5.5` (`claude-sonnet-5-5`) 的周剩余额度百分比，**哪个周额度剩的多用哪个**。
4. **来源指纹与重验周期（TTL）**：支持传入 `reviewer_model`、`require_heterogeneous_review=True` 与 `ttl_days` 生成 `reverify_after` 重验时间戳。

禁止动态导入未知目标脚本。PPT/UI/报告核验须回溯项目真值；只提交有产物或客观证据支持的工艺偏好。

## 4. 分流提交

```python
sdk_memory_review_commit(
    project_root=".",
    reconcile_hash=report["reconcile_hash"],
    reviewer_model=report["reviewer_routing"]["selected_model"],  # "gpt-6.1-terra" or "claude-sonnet-5-5"
    ttl_days=30,
    verified_facts=[{"fact_key": "mod_total_count", "value": "24"}],
    modality_id="ui_aesthetic_design",
    verified_modality_rules=[{
        "rule_id": "visual_hierarchy_v2",
        "category": "layout",
        "title": "量化视觉层级",
        "summary": "按已核验产物的字号比例组织视觉层级",
        "anti_patterns": ["无层级堆叠文本"],
        "hard_boundary": False,
        "supersedes": ["visual_hierarchy_v1"],
    }],
    verified_design_presets=[{
        "preset_key": "visual_style",
        "value": "linear_precision",
        "source_mod": "ui-aesthetic-studio",
    }],
    verified_modality_memories=[{"summary": "保留本次核验的版式偏好"}],
    verified_experiences=[{
        "experience_id": "verify_product_claims",
        "summary": "从项目证据核验交付物中的功能声明",
        "scope": "global",
    }],
    promote_to_global=True,
)
```

同次调用更新项目、工艺与符合晋升条件的全局经验。`mod_total_count` 支持动态整数，并同步项目名称和注册表名称中的数量。

`modalities` 可指定多个目标；每条规则、预设、记忆使用自己的 `modality_id` 分流。只有单一目标时可省略条目目标。非法 ID、被禁用的审查模型（`6.1sol` / `opus5.5`）或多目标歧义返回 `ok=False`，不执行文件写入。

规则按 `rule_id` 合并，缺失时生成稳定哈希 ID；预设按 `preset_key` 合并；记忆按 `memory_id` 合并。`supersedes` / `supersede_ids` 使旧规则或记忆失效，保留审计链。

## 5. 容量与双端同步

项目有效事实/记忆上限为 80/50；工艺有效规则/记忆上限为 60/50。超额普通条目与失效历史进入各自的 `archive/<id>_archive.json`。`hard_boundary` 不因超额被丢弃；受保护项超限时拒绝提交。

主路径为 `~/.antigravity-agent-policy/05_Agent_Memory_Sync`；镜像为 `~/.antigravity-agent-policy/05_Agent_Memory_Mirror`。模态文件与归档在两端逐文件原子替换，保持字节一致；缺失的镜像目录自动创建。

检查提交结果的 `updated_modalities`、`active_modality_rules`、`active_modality_memories` 与 `written_paths`。后两项计数为本次目标工艺的有效条目总数。多文件写入失败没有整体回滚；依据错误和已完成路径核查同步状态。
