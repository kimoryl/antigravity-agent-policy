"""Standard stdio MCP server for Ambitious Deep Thinking Mod (v1.2.0).

Upgrades in v1.2.0:
1. Systematic "Altitude · Depth · Breadth" (高度 · 深度 · 广度) 3D Methodology:
   - Altitude (高度): Elevate from single-instance bug fixing to a durable "Reproduce -> Root-Cause Execute -> Auto-Verify" closed loop with explicit validity preconditions.
   - Depth (Depth): Replace outdated "You are an expert in X" persona labels with real expert instruments, 3-state evidence triage (Verified Facts vs Unverified Claims vs Missing Conditions), and extreme target measurement calibers.
   - Breadth (广度): Transform static deliverable nouns into audience-decision verbs (time-bounded cause-and-effect decision making) and cross-domain verification workflows without roleplay tone.
2. Red-Blue Confrontation Loop (红蓝对抗破立闭环):
   - Blue Team Defense (蓝军立论) -> Red Team 4-Vector Attack (红军四命门破局：吹牛水分/证据缺失、极端边界崩溃、隐性成本膨胀、安全防注入) -> Blue Team Counter-Hardening (蓝军反制补丁) -> Falsifiable Check Matrix & Outside Judge Verdict (可证伪实验与外部客观裁判终审).
3. Preserves 100% backward compatibility with v1.1.0 Grill-Me (`ambitious_grill_alignment`), Sequential Thinking (`ambitious_sequential_reflect`), Product 4-Step workflow, and all legacy fields.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

VERSION = "1.2.0"
MOD_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = MOD_ROOT / "skills" / "ambitious-deep-thinking"
SKILL_MD = SKILL_DIR / "SKILL.md"
REF_MD = SKILL_DIR / "references" / "felix-rieseberg-ambitious-tutorial.md"

# Each optional JSON store has its own in-process ledger; load it lazily so the
# environment can be redirected to an isolated store before the first call.
_SESSION_LEDGER: dict[str, dict[str, Any]] = {}
_SESSION_STORE_PATH: Path | None = None


def _record_session_step(session_id: str, reset_session: bool, entry: dict[str, Any]) -> dict[str, Any]:
    global _SESSION_STORE_PATH
    store_env = os.environ.get("AMBITIOUS_SESSION_STORE")
    store = Path(store_env).resolve() if store_env else None
    if store != _SESSION_STORE_PATH:
        loaded = json.loads(store.read_text(encoding="utf-8")) if store and store.is_file() else {}
        if not isinstance(loaded, dict):
            raise ValueError("AMBITIOUS_SESSION_STORE must contain a JSON session mapping")
        _SESSION_LEDGER.clear()
        _SESSION_LEDGER.update(loaded)
        _SESSION_STORE_PATH = store

    session = deepcopy(_SESSION_LEDGER.get(session_id)) if not reset_session else None
    if session is None:
        session = {"steps": [], "branches": {}, "superseded_steps": [], "accumulated_evidence": []}
    session["steps"].append(entry)
    session["branches"].setdefault(entry["branch_id"], []).append(entry["thought_step"])
    if entry["is_revision"] and entry["revises_step"] is not None:
        session["superseded_steps"] = sorted(set(session["superseded_steps"]) | {entry["revises_step"]})
    if entry["evidence_or_constraint"]:
        session["accumulated_evidence"].append(entry["evidence_or_constraint"])

    if store:
        store.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=store.parent,
                                             prefix=store.name + ".", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({**_SESSION_LEDGER, session_id: session}, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, store)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
    _SESSION_LEDGER[session_id] = session
    return {
        "session_id": session_id,
        "history_count": len(session["steps"]),
        "branches": deepcopy(session["branches"]),
        "superseded_steps": list(session["superseded_steps"]),
        "accumulated_evidence": list(session["accumulated_evidence"]),
        "active_chain_summary": " -> ".join(
            f"{s['thought_step']}@{s['branch_id']}: {s['task_or_hypothesis']}"
            for s in session["steps"] if s["thought_step"] not in session["superseded_steps"]
        ),
    }


DEEP_PROMPT_EN = (
    "I want a deeper version of this task across Altitude, Depth, Breadth, and Red-Blue Confrontation—not a bigger one. "
    "Before rewriting it: "
    "(1) [Altitude] Elevate from fixing a single instance to a durable reproduce-fix-verify loop with explicit preconditions; "
    "(2) [Depth] Say what a top professional in this field would do here that I'm not asking for, name the rawest material they would start from (separating verified facts from unverified claims), name the specific methods/tools they would use instead of expert roleplay, and lock the measurement caliber for an extreme target; "
    "(3) [Breadth] Replace static deliverable nouns with audience-decision verbs and borrow real verification workflows from adjacent fields; "
    "(4) [Red-Blue & Outside Judge] Pit a Red Team (attacking missing evidence, edge crashes, hidden costs, and injection risks) against a Blue Team (supplying concrete counter-patches and falsifiable checks), and say what outside judge—other than you or me—will prove the result is right. "
    "Then rewrite my prompt to ask for exactly that."
)

DEEP_PROMPT_ZH = (
    "我想要的是这个任务在【高度、深度、广度与红蓝对抗】上更深入（deeper）的版本，而不是一个更大/更冗长（bigger）的版本。请停止“你是某某领域专家”的表面人设话术，在重写题目之前：\n"
    "（1）【高度 Altitude】：如何从“解决单次故障”升维到“定位复现 ➔ 根治执行 ➔ 自主验证”的持久闭环，并明确成立前提；\n"
    "（2）【深度 Depth】：请说明该领域的顶尖高手会做哪些我没有要求的隐性动作？会从哪些最原始的一手素材入手（严格区分已核验事实 vs 待核验声明）？会调用哪些真实工具与方法？在设定挑战性极限目标时锁死什么测量口径？\n"
    "（3）【广度 Breadth】：如何将静态交付物名词转化为“让受众在限定时间内看清因果并拍板决策”的行动动词，并借用跨界领域的真实查证工作流（严禁模仿角色语气）？\n"
    "（4）【红蓝对抗与外部裁判】：请展开『蓝军立论 ➔ 红军四命门破局（水分/边界/成本/安全）➔ 蓝军反制补丁 ➔ 可证伪实验』攻防，并指出除了你我之外，有什么外部客观裁判（outside judge）可以证明结果正确。\n"
    "然后，请修改我的题目，使其完全符合上述要求。"
)

GRILL_PROTOCOL_MD = """# Grill-Me 交互式分歧收敛协议 (Grill-Me Alignment Protocol)

对标 `MattPocock/grill-me-skill`：当需求边界模糊或存在多条正交技术路线时，**严禁直接输出万字模板方案**：
1. **识别一阶分歧（First-Order Trade-offs）**：从目标边界、架构耦合度、验证裁判与性能/成本预算中定位 2~3 个决定全局走向的分歧点。
2. **提供带权衡的推荐项（Options with Trade-offs）**：每个问题提供 2~3 个明确选项，首项标注 `(Recommended)` 并说明正反代价。
3. **收敛后再深度执行（Converge then Execute）**：用户拍板或确认采用推荐项后，再展开三维升维、红蓝对抗与代码/产物交付。
"""

PRODUCT_4STEP_MD = """# AI 产品经理四步工程化作业流 (Product-to-Code 4-Step Protocol)

吸收 `daymade/claude-code-skills` 开源产品设计技能库精华，打通从模糊想法到契约级代码交付的全链路：
1. **Step 1 · 锚定真痛点（Core Pain Point）**：剥离表面伪需求，界定『什么角色在什么高频场景下因什么瓶颈遭受了什么可量化损失』。
2. **Step 2 · MVP 优先级砍削（Ruthless Priority Triage）**：按『P0 必达核心闭环 / P1 体验与性能增强 / P2 远期拓展（本轮显式砍除）』三级裁剪，防止范围蔓延。
3. **Step 3 · 契约级 PRD 定义（Contract-Grade PRD）**：明确输入输出 Schema、状态转换边界、异常降级策略与外部客观验收指标（Outside Judge）。
4. **Step 4 · 原子研发任务拆解（Atomic Execution Tasks）**：拆解为单步可测试、可独立回滚的 CLI 原子任务（含目标文件、修改契约与验证命令）。
"""

ALTITUDE_DEPTH_BREADTH_MD = """# 「高度 · 深度 · 广度」三维深度思考方法论 (Altitude · Depth · Breadth Methodology)

沉淀自 Anthropic Claude 工程主管 Felix Rieseberg 原文与知识库《别再对AI说你是某某领域的专家了》（`BV1EFWb6uEoW`）：告别“你是某某专家”的人设开光，将身份暗示彻底转化为可验证的任务契约：

1. **第一维：高度（Altitude）—— 把目标往上提，从单点干活升维到根治闭环**
   - **去包装看本质**：剥离表面宣传词汇或孤立任务名，还原其在系统中的真实工程范式。
   - **从修单点 Bug 到根治闭环**：不要只说“帮我修这次闪退”，而是升维为 **「根据崩溃证据在指定环境复现 ➔ 根治同类缺陷 ➔ 自动化回归验证」** 的持久工作流（Goal beyond the goal）。
   - **锁定成立前提**：任何高目标都必须限定覆盖范围、环境版本与输入契约，拒绝无边界的许愿。

2. **第二维：深度（Depth）—— 往深挖一层，用真实工具、一手证据与极限口径取代“扮演专家”**
   - **去人设换真工具**：对 AI 说“你是数据分析专家”只会让它换一套专业话术（Switched vocabulary without switching methods）。必须直接指定顶尖高手使用的**底层工具、算法模型与隐性加固动作**。
   - **证据状态三分法**：直取最原始的一手素材（源码、崩溃栈、原始日志、论文数据），严格区分 **「已核验事实（Verified Facts）」**、**「待核验声明（Unverified Claims）」** 与 **「缺失条件（Missing Conditions）」**，绝不凭空脑补。
   - **挑战性极限目标 + 锁死测量口径**：敢于设定极限挑战目标（如网页加载从 `10秒 ➔ 0.2秒`），但必须同步锁死页面规模、网络缓存条件与统计口径，严防通过“删减功能”或“偷换口径”作弊。
   - **独立外部客观裁判（Outside Judge）**：绝不让模型自评通过；未实际运行验证的步骤必须标记为“待验证”，严禁伪造“已验证”。

3. **第三维：广度（Breadth / Range）—— 往宽扩一层，名词转动词（受众决策倒逼）与跨界工作流迁移**
   - **名词转动词（Noun → Verb）**：别说“帮我做一份 PPT / 写份报告”（静态名词），改成 **“让忙碌的决策者在 20 分钟内看清前因后果、权衡正反代价并做出决策”**（受众效果与行动动词），用最终决策效果倒逼信息取舍与因果叙事。
   - **跨界借用真实工作流**：跳出单一领域局限，引入控制论双环学习、编译器 PGO 剖析、做空机构尽调、法务会计、混沌工程等跨学科的**真实查证流程**，严禁模仿角色说话腔调。
"""

RED_BLUE_PROTOCOL_MD = """# 红蓝对抗与可证伪加固协议 (Red-Blue Confrontation Protocol)

拒绝单向泛泛而谈的“风险提示”，强制执行 **「有攻有防、有破有立」** 的四步红蓝对抗博弈闭环：

1. **🔵 第一步：蓝军立论（Blue Team Defense · 建构与主创方）**
   - 基于「高度 · 深度 · 广度」提出主干架构/解决方案，明确核心运行链路、成立前提与预期量化收益。
2. **🔴 第二步：红军破局（Red Team Attack · 攻击与证伪方）**
   - 严禁模仿做空者或黑客的阴阳怪气语气，直接调用跨界证伪工具对准蓝军方案的 **4 大命门** 猛攻：
     - **命门 1 [水分与证据缺失]**：哪些性能宣称或论断缺乏一手数据、基准环境和统一测量口径？
     - **命门 2 [极端边界与崩溃]**：在断网超时、并发重入、空/畸形输入、环境缺包或超大规模数据下哪里会崩？
     - **命门 3 [隐性成本与副作用]**：是否会引发 Token 写放大（Cache Tax）、延迟劣化、状态污染或维护负担激增？
     - **命门 4 [安全与防注入风险]**：外部不可信输入（网页/字幕/日志）是否可能绕过隔离边界触发越权或提示词注入？
3. **🛡️ 第三步：蓝军反制加固（Blue Counter-Hardening · 补丁与熔断）**
   - 面对红军刺出的每一处漏洞，蓝军不得嘴硬辩解，必须给出具体的**工程反制补丁、前置契约断言、降级回退分支（Fallback）或预算熔断阈值**。
4. **🔬 第四步：可证伪检查表与外部裁判终审（Falsifiable Check & Outside Judge）**
   - 将每轮红蓝交锋转化为 **「原假设/论断 ➔ 红军攻击向量 ➔ 蓝军反制补丁 ➔ 可证伪实验（怎么测就能拆穿或证明防住）」** 四元组，最终交由隔离单测、AST 签名对账或基准压测一锤定音。
"""

# ---------------------------------------------------------------------------
# 领域原型库：融合「高度·深度·广度」三维锚点、「红蓝对抗」四命门攻防剧本与 Grill-Me 分歧模板
# ---------------------------------------------------------------------------
DOMAIN_ARCHETYPES: dict[str, dict[str, Any]] = {
    "software_architecture": {
        "label": "软件架构与全栈工程重构",
        "keywords": ["架构", "重构", "代码", "接口", "后端", "数据库", "性能", "并发", "api", "refactor", "backend", "schema", "bug", "测试", "闪退", "crash"],
        "unasked_expert_action": "在动工前先扫描 AST 调用图谱与共现修改文件（Co-Occurrence Graph），识别隐藏的跨层循环依赖、破坏性签名变更与回滚边界。",
        "rawest_material": "真实源码 AST、Git 历史提交 Diff、锁文件（pyproject.toml / package.json）、线上报错堆栈与慢查询/遥测原始日志。",
        "specific_tools": "AST 静态图谱（CodeGraph v2）、属性测试/边界单元测试（pytest / unittest）、火焰图剖析与契约回归套件。",
        "outside_judge": "隔离环境单元测试 100% 绿灯、静态类型与签名对账（sdk_fact_reconcile_report）零误差、P99 延迟/内存基准不退化。",
        "red_team_method": "混沌工程故障注入法（Chaos Fault Injection）+ 破坏性输入边界证伪（假设下游超时、编码损坏、并发重入）。",
        "altitude_loop": "从『修单点 Bug / 改单个函数』升维为『基于真实报错栈在隔离环境复现 ➔ 根治同类架构脆弱点 ➔ 固化回归断言防复发』的持久闭环。",
        "depth_challenge_caliber": "挑战极限延迟/零回归目标（如核心路径耗时压缩 80% 且 100% 向后兼容），同步锁死输入载荷规模、冷/热缓存状态与 P99 计时口径，严禁靠删减校验逻辑凑速度。",
        "breadth_verb_shift": "将静态名词『写一个模块/改一下接口』转化为动词『在不中断任何现有上下游调用方的前提下，消除隐性状态耦合与异常崩溃并返回可机读诊断契约』。",
        "cross_domain_crew": "借用【航空电子双余度容错设计（前置断言+优雅降级）】、【编译器静态数据流分析（AST 签名与副作用追踪）】与【混沌工程故障注入】的真实核查工作流。",
        "red_blue_matrix": {
            "blue_thesis": "采用纯标准库/内聚模块化改造，在保持既有 API/Schema 100% 向后兼容的前提下完成核心逻辑升级，并通过 tempfile 隔离单测闭环。",
            "fights": [
                {
                    "dimension": "证据与口径核查",
                    "claim_or_assumption": "宣称架构优化后性能显著提升且完全兼容既有调用方",
                    "red_team_attack": "红军质疑：缺少冷启动 vs 热缓存的统一计时口径，且未核对所有历史调用点的隐式返回键依赖，存在『删校验换速度』或『静默破坏下游』水分。",
                    "blue_counter_patch": "蓝军反制：锁定全量旧字段契约断言（Schema Parity Check），固定输入样本规模与运行环境测量改前/改后耗时。",
                    "falsifiable_experiment": "运行新旧接口字段对账单测与固定样本基准对比，若缺失任一既有字段或回归失败则直接判定失格。",
                },
                {
                    "dimension": "极端边界与故障注入",
                    "claim_or_assumption": "假设输入参数格式正常、磁盘读写顺畅且单线程顺序调用",
                    "red_team_attack": "红军攻击：注入空字符串、超长畸形 JSON、Windows 文件句柄占用或并发重入写入，刺穿无锁落盘逻辑。",
                    "blue_counter_patch": "蓝军反制：入口强制执行非空与类型归一化校验，文件持久化采用 NamedTemporaryFile + os.replace 原子替换。",
                    "falsifiable_experiment": "在 tempfile 隔离目录构造空输入、越界参数与临时文件替换测试，验证抛错清晰且零残留脏文件。",
                },
                {
                    "dimension": "隐性成本与写放大",
                    "claim_or_assumption": "新增结构化分析维度不会明显增加响应体积与延迟",
                    "red_team_attack": "红军攻击：全量展开所有说明文档会导致单次返回 JSON 暴涨数倍，加剧主控上下文膨胀与 Cache Tax。",
                    "blue_counter_patch": "蓝军反制：坚持按需分层返回（Progressive Disclosure），默认仅输出高密度核心字段，长文档通过 section 参数按需拉取。",
                    "falsifiable_experiment": "测量默认调用返回 JSON 字符长度与子进程响应耗时（<2s），验证体积受控。",
                },
                {
                    "dimension": "安全与防注入边界",
                    "claim_or_assumption": "用户输入或外部日志文本仅作为普通字符串拼接处理",
                    "red_team_attack": "红军攻击：若 raw_task 中包含恶意控制字符、路径穿越符号或伪造指令，可能污染会话账本或诱发越权操作。",
                    "blue_counter_patch": "蓝军反制：将会话状态存储路径严格限制在显式环境变量或内存字典内，外部输入仅做纯数据文本序列化，严禁 eval/shell 拼接。",
                    "falsifiable_experiment": "传入含特殊字符与伪造指令的 raw_task，验证 JSON-RPC 正常序列化且无任何越权副作用。",
                },
            ],
        },
        "grill_templates": [
            {
                "dimension": "兼容性与演进策略",
                "question": "本次架构改造对既有调用方接口（API / MCP Schema）的兼容策略是什么？",
                "why_it_matters": "直接决定是可以平滑热替换，还是需要同步迁移所有上下游调用点。",
                "options": [
                    "(Recommended) 100% 向后兼容演进：保留既有函数签名与返回字段，通过可选参数扩展新能力",
                    "双轨并存过渡：新增独立 V2 接口/工具，旧接口标记 Deprecated 并保留适配层",
                    "破坏性彻底重构：直接替换底层契约并一次性全量修改所有调用方",
                ],
            },
            {
                "dimension": "状态存储与外部依赖边界",
                "question": "新增逻辑的数据状态与运行环境边界应如何约束？",
                "why_it_matters": "影响部署复杂度、跨平台可移植性以及冷启动耗时。",
                "options": [
                    "(Recommended) 纯标准库 / 零外部守护进程：使用内存或轻量级本地 SQLite/JSON 原子落盘，零额外服务依赖",
                    "复用现有共享基础设施：接入现有本地索引库或全局缓存目录",
                    "引入独立扩展组件：允许新增第三方 SDK 依赖或独立后台进程",
                ],
            },
            {
                "dimension": "客观验收门禁（Outside Judge）",
                "question": "交付完成的核心验证裁判标准是什么？",
                "why_it_matters": "防止主观自评通过，确保代码变更具备可重复验证的客观证据。",
                "options": [
                    "(Recommended) 隔离单测 + 全量回归套件双绿灯：新增 tempfile 隔离单元测试并通过仓库级回归测试",
                    "端到端真实样本压测：在真实业务样本集上跑通前后对比基准并校验输出 SHA256",
                    "快速最小可行性验证（PoC）：先跑通核心主路径冒烟测试，后续再补齐边缘单测",
                ],
            },
        ],
    },
    "multiagent_mcp": {
        "label": "多智能体编排与 MCP / Skill 工具链",
        "keywords": ["agent", "mcp", "skill", "cli", "子代理", "智能体", "提示词", "prompt", "orchestrat", "mod", "额度", "token", "上下文", "思考"],
        "unasked_expert_action": "评估工具 Schema 与终端输出对上下文窗口（Context Window）及 5 分钟 Prompt Cache TTL（Cache Tax）的膨胀冲击，实施读写分离与输出降噪。",
        "rawest_material": "MCP stdio JSON-RPC 原始收发报文、SessionTelemetry 会话轨迹日志、mod.json 物理清单与真实 Token 消耗账本。",
        "specific_tools": "JSON-RPC 2.0 协议校验器、AST 签名核验（context7-sdk-verifier）、终端输出折叠净化器与主从 CLI 路由策略器。",
        "outside_judge": "stdio 握手与 tools/list + tools/call 零异常返回、单次调用 Token 体积高效受控、跨 CLI 挂载回归测试全通过。",
        "red_team_method": "对抗式提示词注入与死循环诱导测试（模拟子代理超时、畸形 JSON 参数、重复调用同一失败工具）。",
        "altitude_loop": "从『写一段静态提示词/加一个孤立工具』升维为『前置分歧收敛 ➔ 高度·深度·广度三维升维 ➔ 红蓝对抗证伪 ➔ 外部裁判验收』的智能体认知控制循环（Agentic Control Loop）。",
        "depth_challenge_caliber": "摒弃『你是某某专家』的表面人设开光，强制输出可机读的三维证据矩阵与红蓝攻防表；锁死 stdio JSON-RPC 冷启动耗时（<1s）与 Schema 一致性口径。",
        "breadth_verb_shift": "将静态名词『优化提示词/封装 MCP 工具』转化为动词『在 30 秒内迫使模型暴露隐含假设、完成红蓝自我证伪并生成零歧义的可验证执行契约』。",
        "cross_domain_crew": "借用【控制论双环学习（Single/Double-Loop Learning 纠偏）】、【做空机构尽调证伪（剥离营销水分）】与【形式化契约设计（Design-by-Contract）】的真实核查流程。",
        "red_blue_matrix": {
            "blue_thesis": "通过高度·深度·广度三维引擎与红蓝对抗矩阵，将用户的浅层模糊指令编译为带证据核验、极限口径与可证伪检查项的结构化契约。",
            "fights": [
                {
                    "dimension": "证据与口径核查",
                    "claim_or_assumption": "宣称重写后的 Prompt 能大幅提升模型思考深度与交付质量",
                    "red_team_attack": "红军质疑：若只是把『你是专家』换成一长串空洞的方法论口号（模板复读机），模型依然只会换套话术演戏，缺乏针对当前任务的真实证据锚点。",
                    "blue_counter_patch": "蓝军反制：强制执行『证据状态三分法（已核验输入 vs 待核验声明 vs 缺失条件）』与任务实体动态提取，拒绝无证据脑补。",
                    "falsifiable_experiment": "传入不同领域的真实任务，检查输出是否准确分离出已给约束与缺失维度，并绑定可执行的外部裁判命令。",
                },
                {
                    "dimension": "极端边界与故障注入",
                    "claim_or_assumption": "多步思考链（Sequential Reflect）能稳定追踪主干与分支状态",
                    "red_team_attack": "红军攻击：当外部裁判返回报错信号（error/timeout/不兼容）或并发切换会话存储文件时，状态机可能遗漏修订步或损坏账本。",
                    "blue_counter_patch": "蓝军反制：自动嗅探反例关键词触发 needs_revision=True 并动态扩充 total_thoughts，账本写入采用临时文件原子替换。",
                    "falsifiable_experiment": "注入包含报错证据的 step 调用与 reset_session 测试，验证 superseded_steps 自动剔除旧步且磁盘账本零损坏。",
                },
                {
                    "dimension": "隐性成本与写放大",
                    "claim_or_assumption": "常驻 @core 的思考工具不会造成额外的上下文负担",
                    "red_team_attack": "红军攻击：如果每次调用都把上万字的参考教程全量塞进返回包，会迅速打爆主会话上下文窗口与 5m Cache Tax。",
                    "blue_counter_patch": "蓝军反制：`ambitious_thinking_guide` 按 `section` 枚举精细按需加载，重写工具输出结构化高信噪比字段。",
                    "falsifiable_experiment": "逐一调用各 section 枚举参数，验证仅请求模块被返回且 schema 与 mod.json 100% 对齐。",
                },
                {
                    "dimension": "安全与防注入边界",
                    "claim_or_assumption": "处理的视频字幕、外部文档或用户任务描述均安全可信",
                    "red_team_attack": "红军攻击：外部不可信语料中常夹带『忽略上述指令』等间接提示词注入，若在复盘时被当成系统规则写入记忆将引发持久污染。",
                    "blue_counter_patch": "蓝军反制：在红蓝对抗矩阵中内建防间接提示词注入审计项，严格区分外部静态语料与系统控制指令。",
                    "falsifiable_experiment": "检查生成的红蓝对抗清单是否显式包含防注入与外部客观裁判门禁。",
                },
            ],
        },
        "grill_templates": [
            {
                "dimension": "能力承载形态（Mod 纪律裁决）",
                "question": "该项智能体能力应当如何合入现有 24 Mods 体系？",
                "why_it_matters": "遵循全局反膨胀铁律，防止工具数量碎片化导致上下文 Cache Tax 飙升。",
                "options": [
                    "(Recommended) 纵深内聚升级存量 Mod：在不增加新 Mod 数量的前提下，增强现有对口 Mod 的工具与规则",
                    "作为纯规则 / Skill 协议注入：仅更新 SKILL.md 与 AGENTS.md 认知约束，不新增 MCP 工具定义",
                    "孵化独立新工具端点：在现有 Mod 内新增专用 MCP 工具（需同步更新 mod.json 与回归测试）",
                ],
            },
            {
                "dimension": "上下文与 Token 预算控制",
                "question": "工具返回载荷或提示词展开的详细程度如何平衡？",
                "why_it_matters": "决定高频调用时的延迟与多 CLI 共享额度池消耗速度。",
                "options": [
                    "(Recommended) 按需分层返回（Progressive Disclosure）：默认返回高密度结构化摘要，支持参数按需展开全量细节",
                    "极简紧凑模式：严格限制单次返回字符数，剔除所有解释性散文",
                    "全量详尽模式：一次性返回完整指南、双语对照与全套推演蓝图",
                ],
            },
            {
                "dimension": "主从 Agent 读写与执行分工",
                "question": "该流程在主控（Master）与业务/伴飞子代理之间如何分工？",
                "why_it_matters": "确保记忆库单写者隔离（Single-Writer Isolation）与并发安全性。",
                "options": [
                    "(Recommended) 主控聚焦决策对齐 + 伴飞侧车独立审计：业务端只读消费简报，产物由主控与测试子代理对账落盘",
                    "单会话闭环直出：在当前主控上下文内直接完成分析、重写与验证，不额外拉起子代理",
                    "派发 CLI 特种兵并行攻坚：将生成后的原子任务交给 codex-cli / agy-cli 在隔离分支执行",
                ],
            },
        ],
    },
    "ui_visual_motion": {
        "label": "高审美 UI 视觉系统与物理动效/视频渲染",
        "keywords": ["ui", "前端", "视觉", "美学", "动效", "视频", "渲染", "oklch", "css", "svg", "html", "组件", "设计", "threejs", "shader", "ppt"],
        "unasked_expert_action": "拒绝 AI Slop（烂大街紫蓝渐变与通用卡片套娃），先锁定 OKLCH 感知均匀色阶、WCAG 2.1 对比度数学解与二阶阻尼弹簧物理方程，并校验 Layout Thrashing。",
        "rawest_material": "DOM 几何包围盒数据、sRGB/OKLCH 原始色值矩阵、CSS 关键帧时间轴、SVG 路径节点与 60fps 帧耗时监控数据。",
        "specific_tools": "OKLCH 色彩空间求解器（ui-aesthetic-studio）、二阶谐振子弹簧编译器（anatomy-motion-studio）、无头浏览器帧级确定性光栅化管线。",
        "outside_judge": "Anti-AI Slop 审计评分 ≥90 分、WCAG 正文对比度 ≥4.5:1、零非合成属性（top/left/width/height）动画、支持 prefers-reduced-motion。",
        "red_team_method": "视觉噪音与极端视口压力测试（超长多语言文本溢出、色盲模拟滤镜、低端设备 4x CPU 降速掉帧核查）。",
        "altitude_loop": "从『画一个好看的静态页面/PPT』升维为『构建具备数学色彩和谐度、物理弹簧质感与自动化视觉回归防线的可交互决策沙盘』。",
        "depth_challenge_caliber": "设定 60fps 零掉帧与 WCAG AAA/AA 严苛对比度目标，锁死视口分辨率矩阵（375px 移动端至 1920px 大屏）、字体回退栈与 GPU 合成层测量口径。",
        "breadth_verb_shift": "将静态名词『做一份 PPT / 写个前端页面』转化为动词『在 30 秒内用零塑料感的视觉层级抓住受众注意力，并在 20 分钟内辅助其看清复杂系统因果并拍板决策』。",
        "cross_domain_crew": "借用【瑞士国际主义平面网格系统（数学比例留白）】、【经典力学二阶阻尼振子（自然弹簧动效）】与【工业印刷色彩管理（OKLCH 感知等亮）】的工作流。",
        "red_blue_matrix": {
            "blue_thesis": "采用 OKLCH 感知均匀色彩令牌 + 二阶物理弹簧曲线 + 零依赖语义化 HTML/SVG 架构，彻底消除 AI 塑料感并保障高帧率交互。",
            "fights": [
                {
                    "dimension": "证据与口径核查",
                    "claim_or_assumption": "宣称界面达到高端杂志级/Linear 级高审美水准且动画丝滑",
                    "red_team_attack": "红军质疑：缺乏明暗主题下的实际对比度数值与帧耗时采样证据，可能暗藏紫粉渐变俗套（AI Slop）或触发布局抖动的 top/left 动画。",
                    "blue_counter_patch": "蓝军反制：强制运行 Anti-AI Slop 规则扫描，禁用高饱和霓虹渐变，所有位移/缩放动画仅允许操作 transform 与 opacity。",
                    "falsifiable_experiment": "执行 CSS 属性静态扫描与 WCAG 对比度公式核算，检出违规属性或对比度 <4.5:1 即判定不通过。",
                },
                {
                    "dimension": "极端边界与故障注入",
                    "claim_or_assumption": "卡片与排版在标准示例文案下对齐完美",
                    "red_team_attack": "红军攻击：注入 3 倍长度德文/中文折行文案、空数据状态或 320px 极窄视口，刺穿固定高度容器导致文字溢出重叠。",
                    "blue_counter_patch": "蓝军反制：使用 CSS Grid `minmax(0, 1fr)`、流体字号 `clamp()` 与Bounding-Box 溢出自适应裁剪机制。",
                    "falsifiable_experiment": "在多视口尺寸与极端长短文本样本下校验容器包围盒，确保零横向滚动条与零文字截断重叠。",
                },
                {
                    "dimension": "隐性成本与写放大",
                    "claim_or_assumption": "引入酷炫光影、模糊玻璃态与粒子特效可提升视觉冲击力",
                    "red_team_attack": "红军攻击：大面积 `backdrop-filter: blur()` 与未节流的 Canvas 重绘会导致集显笔记本风扇狂转、帧率跌破 20fps。",
                    "blue_counter_patch": "蓝军反制：限制模糊图层面积与层叠深度，离屏不可见元素自动暂停动画循环，并提供 `prefers-reduced-motion` 无障碍降级。",
                    "falsifiable_experiment": "核查代码中是否包含 `prefers-reduced-motion` 媒体查询及动画生命周期销毁逻辑。",
                },
                {
                    "dimension": "安全与防注入边界",
                    "claim_or_assumption": "动态渲染外部传入的 Markdown 或图表配置文本",
                    "red_team_attack": "红军攻击：未经转义的 `innerHTML` 或外部 CDN 脚本加载可能引发 XSS 执行或离线环境白屏崩溃。",
                    "blue_counter_patch": "蓝军反制：动态文本一律走 `textContent` 或白名单转义，核心样式与图标内联闭合，消除脆弱外部运行时依赖。",
                    "falsifiable_experiment": "断网离线打开产物并注入含 `<script>` 标签的测试数据，验证渲染正常且无脚本执行。",
                },
            ],
        },
        "grill_templates": [
            {
                "dimension": "视觉美学原型（Aesthetic Archetype）",
                "question": "界面或演示产物的核心视觉气质定位于哪一类？",
                "why_it_matters": "决定色彩色相角（Hue）、字体排印层级、圆角嵌套半径与留白密度。",
                "options": [
                    "(Recommended) Swiss Precision / Linear 工业级高密度极简：高对比中性底色 + 1px 精密边框 + 信号色锚点",
                    "Editorial Narrative 杂志级叙事排版：衬线/无衬线高反差标题 + 宽幅留白 + 信息图解标注（①②③）",
                    "Atmospheric Minimalism 沉浸式暗色调光影：深邃多层背景 + 微妙高光玻璃态 + 物理弹簧微交互",
                ],
            },
            {
                "dimension": "动效与交付载体形态",
                "question": "最终视觉产物的运行与交付形态是什么？",
                "why_it_matters": "决定使用零依赖单文件 HTML、框架组件代码还是声明式视频渲染管线。",
                "options": [
                    "(Recommended) 零依赖单文件交互式 HTML/SVG + CSS Spring：开箱即用，兼容所有现代浏览器与 Artifact 预览",
                    "生产级组件源码（React / Vue / Tailwind + WAAPI）：直接合入前端工程仓库",
                    "声明式时间轴无头视频渲染脚本（HTML-to-Video）：面向产品路演自动合成确定性高清演示视频",
                ],
            },
        ],
    },
    "distributed_systems_infra": {
        "label": "高并发分布式系统、存储内核与云原生架构",
        "keywords": ["分布式", "高并发", "共识", "raft", "存储引擎", "事务", "微服务", "网关", "缓存穿透", "死锁", "吞吐", "p99", "云原生", "kubernetes"],
        "unasked_expert_action": "在调整并发度或缓存层前，先定位端到端关键路径瓶颈、锁竞争热点与故障域隔离边界，严禁盲目堆砌重试导致雪崩放大。",
        "rawest_material": "压测火焰图、慢查询执行计划、WAL/事务日志序列、连接池水位指标与真实网络超时拓扑。",
        "specific_tools": "确定性混沌故障注入测试台、压测基准对比器、事务隔离级别验证器与结构化延迟分位分析脚本。",
        "outside_judge": "P99 延迟与吞吐达标、网络分区与节点宕机故障注入下零脏写零丢数、前后压测基线对比完整可复现。",
        "red_team_method": "网络抖动、时钟漂移、进程崩溃重启与并发写冲突注入（核查幂等键失效、重试风暴与连接泄漏）。",
        "altitude_loop": "从『修补单次超时报错』升维为『压测基线采集 ➔ 锁与IO热点溯源 ➔ 幂等与背压加固 ➔ 故障注入回归验证』的工程闭环。",
        "depth_challenge_caliber": "在保障线性一致性或明确事务隔离边界的前提下优化 P99 尾延迟，锁死并发度、负载模型与硬件资源口径。",
        "breadth_verb_shift": "将静态名词『写一个高并发接口 / 搭一套缓存』转化为动词『向调用方提供一套在峰值流量与局部节点故障下仍具备确定性背压与零数据损坏保证的服务契约』。",
        "cross_domain_crew": "借用【数据库内核 WAL 崩溃恢复校验】、【排队论 Little 定律容量规划】与【航空双余度熔断降级】的工作流。",
        "red_blue_matrix": {
            "blue_thesis": "基于有界队列背压、幂等状态机与原子批量提交重构关键路径，并通过确定性并发测试验证。",
            "fights": [
                {
                    "dimension": "证据与口径核查",
                    "claim_or_assumption": "宣称优化后吞吐提升且延迟下降",
                    "red_team_attack": "红军质疑：只报告平均延迟（Avg）而掩盖了 P99/P999 长尾毛刺，或未给出相同负载口径下的前后基线对比。",
                    "blue_counter_patch": "蓝军反制：强制输出 P50/P95/P99 全分位延迟与错误率前后对照表，锁死压测并发参数。",
                    "falsifiable_experiment": "运行固定种子并发基准脚本，核验错误率为 0 且 P99 尾延迟不劣于基线。",
                },
                {
                    "dimension": "极端边界与故障注入",
                    "claim_or_assumption": "下游慢响应时自动重试可提升成功率",
                    "red_team_attack": "红军攻击：无退避无预算的同步重试会瞬间打满连接池并引发级联重试风暴。",
                    "blue_counter_patch": "蓝军反制：引入带抖动的指数退避、全局重试预算上限（≤10%）与熔断快速失败机制。",
                    "falsifiable_experiment": "注入下游超时故障，验证上游线程池零阻塞耗尽且熔断器按期开合恢复。",
                },
                {
                    "dimension": "隐性成本与写放大",
                    "claim_or_assumption": "每次状态变更同步全量刷盘最安全",
                    "red_team_attack": "红军攻击：高频随机小 IO 全量覆写会引发严重写放大并拖垮磁盘 IOPS。",
                    "blue_counter_patch": "蓝军反制：采用追加写 WAL 与组提交（Group Commit）批量落盘，兼顾崩溃恢复原子性与高吞吐。",
                    "falsifiable_experiment": "对比开启组提交前后的磁盘写放大倍率与崩溃重启数据完整性校验。",
                },
                {
                    "dimension": "安全与防注入边界",
                    "claim_or_assumption": "内部微服务网关转发外部请求头与参数",
                    "red_team_attack": "红军攻击：未清洗伪造内部身份头或未限制请求载荷体积可能触发越权访问与内存耗尽。",
                    "blue_counter_patch": "蓝军反制：入口层强制剥离外部伪造内部头、校验 Schema 载荷上限并绑定全链路超时。",
                    "falsifiable_experiment": "构造超长载荷与伪造内部头请求，验证网关返回 400/403 且内存水位平稳。",
                },
            ],
        },
        "grill_templates": [
            {
                "dimension": "核心架构目标与一致性权衡",
                "question": "当前分布式系统调优的首要控制矛盾是什么？",
                "why_it_matters": "决定采用强一致同步复制、最终一致异步流水线，还是有界背压限流架构。",
                "options": [
                    "(Recommended) 强一致性与零丢数优先：确保事务幂等与故障恢复闭合，在此基础上压降 P99 尾延迟",
                    "峰值吞吐与低延迟优先：允许受控时间窗内的异步最终一致与读缓存加速",
                    "高可用容灾与故障隔离优先：聚焦熔断降级、重试预算与跨节点无损切换",
                ],
            },
            {
                "dimension": "变更影响面与兼容性边界",
                "question": "允许本次架构改造触及的协议与存储范围是什么？",
                "why_it_matters": "涉及上下游调用方接口契约与在线平滑灰度回滚能力。",
                "options": [
                    "(Recommended) 保持外部 API 与存储 Schema 100% 向后兼容，仅重构内部并发管线与索引路径",
                    "允许新增双写演进字段或扩展内部 RPC 契约并配套灰度开关",
                    "仅做只读压测剖析与瓶颈诊断，输出带证据的架构改造方案",
                ],
            },
        ],
    },
    "product_data_strategy": {
        "label": "AI 产品设计、数据分析与商业决策",
        "keywords": ["产品", "需求", "prd", "用户", "痛点", "运营", "商业", "数据", "分析", "财报", "量化", "调研", "汇报", "路演", "mvp"],
        "unasked_expert_action": "拒绝罗列功能清单或表面均值统计，先用反事实推演验证『伪需求风险』，拆解用户真实作业流中的最大摩擦阻力，并绑定可证伪的北极星验收指标。",
        "rawest_material": "用户真实操作轨迹日志、原始业务明细表（未聚合前）、竞品真实差评样本、一线的报错/流失漏斗原始记录。",
        "specific_tools": "AI 产品经理四步法（痛点定位 ➔ MVP 优先级砍削 ➔ 契约级 PRD ➔ 原子任务）、队列留存/归因拆解模型、做空机构尽调证伪清单。",
        "outside_judge": "契约级 PRD 具备无歧义输入输出 Schema 与边界用例、MVP 核心链路可在单次迭代内闭环验证、外部回测指标显著改善。",
        "red_team_method": "做空机构尽调法（Short-Seller Due Diligence）+ 幸存者偏差核查（假设核心增长只是季节性噪音或单一大客户拉动）。",
        "altitude_loop": "从『按模板更新季度数字/罗列产品功能清单』升维为『识别本周期最关键的异常变化与归因 ➔ 锁定最紧迫的 3 个战略决策及正反论据 ➔ 建立比静态汇报更早捕捉风险的持续预警机制』。",
        "depth_challenge_caliber": "拒绝只给平均数或口头宣称『效果翻倍』，强制下钻到分群分位数（P50/P90/P99）、队列留存（Cohort）与对照组实验；锁死样本时间窗、分母定义与归因口径。",
        "breadth_verb_shift": "将静态名词『写一份 PRD / 做一份商业分析报告』转化为动词『在 20 分钟内让忙碌的决策者基于一手证据看清因果关系、明确砍掉 P2 伪需求并在不确定性下拍板资源调配』。",
        "cross_domain_crew": "借用【华尔街做空机构尽调（排查财务/增长数据水分）】、【法务会计（追踪异常资金/流量去向）】与【调查记者（交叉核实一手信源）】的真实查证工作流。",
        "red_blue_matrix": {
            "blue_thesis": "通过『真痛点锚定 ➔ P0/P2 优先级残酷砍削 ➔ 契约级 PRD ➔ 原子研发任务』四步闭环，用最小工程代价验证核心价值假设。",
            "fights": [
                {
                    "dimension": "证据与口径核查",
                    "claim_or_assumption": "宣称某新功能或策略是用户核心刚需，能带来大幅增长或提效",
                    "red_team_attack": "红军质疑（做空尽调视角）：把星标数、围观热度或个别用户口头愿望当成真实留存证据；平均值增长可能被单一极端样本拉高（幸存者偏差）。",
                    "blue_counter_patch": "蓝军反制：剥离宣传热度与总量泡沫，必须提供高频场景下的真实摩擦耗时损耗与中位数/分群转化数据。",
                    "falsifiable_experiment": "设定最小反事实对照实验：若上线 P0 原型后核心留存/完成率未达阈值，则立即证伪该需求假设。",
                },
                {
                    "dimension": "极端边界与故障注入",
                    "claim_or_assumption": "产品流程在理想用户配合下顺畅运转",
                    "red_team_attack": "红军攻击：当面对零基础小白误操作、冷启动零数据、外部 API 限流或脏数据导入时，用户体验是否直接崩溃？",
                    "blue_counter_patch": "蓝军反制：在契约级 PRD 中强制定义冷启动默认态、异常降级兜底文案与一键回退路径。",
                    "falsifiable_experiment": "走查 PRD 与原型中的空状态、错误状态与超时状态覆盖率，缺一不可。",
                },
                {
                    "dimension": "隐性成本与写放大",
                    "claim_or_assumption": "把 200 个功能或全套配置一次性塞进产品显得大而全",
                    "red_team_attack": "红军攻击：功能堆砌会导致研发排期失控、认知负荷爆炸，且过度简化的摘要可能掩盖关键业务风险。",
                    "blue_counter_patch": "蓝军反制：执行残酷的 P0/P1/P2 砍削（本轮显式砍除所有 P2 边缘功能），同时在决策简报中保留关键假设与代价说明。",
                    "falsifiable_experiment": "检查输出方案是否明确列出了『本轮显式砍除（P2 Cut）清单』与『核心方案代价』。",
                },
                {
                    "dimension": "安全与防注入边界",
                    "claim_or_assumption": "业务数据采集、第三方插件接入与自动化运营完全合规",
                    "red_team_attack": "红军攻击：未核查数据授权边界、许可证传染风险、接口调用计费黑洞或不可信内容注入。",
                    "blue_counter_patch": "蓝军反制：增设许可证/计费配额硬上限与外部输入脱敏清洗门禁。",
                    "falsifiable_experiment": "核查外部依赖的开源协议、API 费用熔断上限与权限最小化配置。",
                },
            ],
        },
        "grill_templates": [
            {
                "dimension": "目标受众与第一痛点锚定",
                "question": "该产品/方案最优先解决哪一类核心场景的痛点？",
                "why_it_matters": "决定 MVP 的核心主路径设计，防止做成面面俱到却无一深入的平庸工具。",
                "options": [
                    "(Recommended) 聚焦高频刚需闭环（砍掉 80% 边缘功能，把最痛的单一链路做到一键自动化）",
                    "面向专业高级用户的全功能工作台（提供高自由度参数配置与多视图联动）",
                    "面向管理层/外部客户的高审美叙事演示（优先打磨核心洞察可视化与交互体验）",
                ],
            },
            {
                "dimension": "交付深度与落地颗粒度",
                "question": "本次任务期望产出的最终交付物颗粒度是什么？",
                "why_it_matters": "决定停留在战略蓝图/PRD 阶段，还是直接穿透到可运行代码与测试闭环。",
                "options": [
                    "(Recommended) 产品设计 + 代码落地全链路（先输出精炼四步契约 PRD，紧接着拆解原子任务并编写可验证代码）",
                    "仅输出契约级 PRD 与高阶深度思考 Prompt（供后续交由专项 CLI 团队执行）",
                    "快速可交互原型（Generative UI / MVP 脚本）先行验证核心假设",
                ],
            },
        ],
    },
}


def _detect_domain_archetype(raw_task: str, domain: str) -> tuple[str, dict[str, Any]]:
    blob = f"{domain} {raw_task}".lower()
    best_key = "software_architecture"
    best_score = 0
    for key, arch in DOMAIN_ARCHETYPES.items():
        score = sum(1 for kw in arch["keywords"] if kw in blob)
        if score > best_score:
            best_score = score
            best_key = key
    if best_score == 0:
        if any(w in blob for w in ["产品", "需求", "分析", "报告", "商业", "product", "strategy"]):
            best_key = "product_data_strategy"
    return best_key, DOMAIN_ARCHETYPES[best_key]


def _assess_ambiguity(raw_task: str, constraints: list[str] | None = None) -> dict[str, Any]:
    text = raw_task.strip()
    constraints_list = [c.strip() for c in (constraints or []) if c and str(c).strip()]
    blob = f"{text} {' '.join(constraints_list)}".lower()

    missing: list[str] = []
    if len(text) < 35:
        missing.append("任务背景与具体输入输出边界过于简短")
    if not any(w in blob for w in ["测试", "验证", "指标", "标准", "报错", "退出码", "test", "verify", "assert", "metric", "sha256"]):
        missing.append("缺少外部客观验收标准（Outside Judge）")
    if not any(w in blob for w in ["兼容", "不破坏", "保留", "架构", "目录", "路径", ".py", ".ts", ".json", "schema", "接口"]):
        missing.append("未明确存量系统兼容边界或目标物理路径")
    if not any(w in blob for w in ["优先", "p0", "mvp", "只做", "不做", "范围", "约束", "禁止"]):
        missing.append("未界定 MVP 优先级砍削边界（易引发过度设计）")

    score = min(1.0, round(0.18 * len(missing) + (0.25 if len(text) < 25 else 0.05), 2))
    is_ambiguous = score >= 0.40 and len(constraints_list) < 2
    return {
        "ambiguity_score": score,
        "is_ambiguous": is_ambiguous,
        "missing_dimensions": missing,
        "recommended_mode": "grill_first" if is_ambiguous else "direct_rewrite",
    }


def _is_dimension_already_aligned(dimension: str, known_low: str) -> bool:
    dim_low = dimension.lower()
    if "兼容" in dim_low and any(k in known_low for k in ["向后兼容", "100% 兼容", "backward compatible", "backward-compatible", "保留既有"]):
        return True
    if ("依赖" in dim_low or "存储" in dim_low) and any(k in known_low for k in ["标准库", "stdlib", "零外部", "zero external", "sqlite"]):
        return True
    if ("验收" in dim_low or "judge" in dim_low) and any(k in known_low for k in ["pytest", "unittest", "隔离单测", "回归测试"]):
        return True
    return False


FALLBACK_DYNAMIC_TEMPLATES: list[dict[str, Any]] = [
    {
        "dimension": "延迟与 Token 预算护栏（Latency & Cache Tax Budget）",
        "question": "在高并发或长链路调用下，单次执行的耗时与上下文体积上限如何约束？",
        "why_it_matters": "防止单次工具返回过大撑爆 5 分钟 Prompt Cache TTL 或触发超时中断。",
        "options": [
            "(Recommended) 严格有界执行（单次耗时 <2s、默认返回紧凑摘要，按需展开详情）",
            "吞吐量优先（允许批量流式处理与后台异步落盘）",
            "精度优先（完整展开全部推演细节与双语对照，不限制返回体积）",
        ],
    },
    {
        "dimension": "异常降级与容错回退（Failure Fallback Strategy）",
        "question": "当外部输入异常、文件缺失或下游反例触发时，系统应采取何种降级策略？",
        "why_it_matters": "决定系统在边缘恶劣工况下的鲁棒性与可观测性。",
        "options": [
            "(Recommended) 确定性优雅降级 + 显式错误码诊断（绝不静默吞掉异常，同时保持主流程可恢复）",
            "快速失败熔断（Fail-Fast：遇到任何契约偏离立即抛错终止并回滚）",
            "只读安全模式回退（自动切回无副作用的只读分析结果）",
        ],
    },
]


def _build_grill_questions(
    raw_task: str,
    domain: str,
    known_context: str = "",
    max_questions: int = 3,
) -> dict[str, Any]:
    arch_key, arch = _detect_domain_archetype(raw_task, domain)
    max_q = max(1, min(int(max_questions or 3), 4))
    known_low = f"{raw_task} {known_context}".lower()

    auto_aligned_dimensions: list[str] = []
    selected: list[dict[str, Any]] = []
    for tpl in arch["grill_templates"]:
        if known_context.strip() and _is_dimension_already_aligned(tpl["dimension"], known_low):
            auto_aligned_dimensions.append(tpl["dimension"])
            continue
        selected.append(
            {
                "id": f"grill_q{len(selected) + 1}",
                "dimension": tpl["dimension"],
                "question": tpl["question"],
                "why_it_matters": tpl["why_it_matters"],
                "options": list(tpl["options"]),
                "recommended_default": tpl["options"][0],
            }
        )
        if len(selected) >= max_q:
            break

    # 若因上下文已对齐或模板不足导致数量少于 max_q，补充动态高杠杆追问
    fallback_pool = list(FALLBACK_DYNAMIC_TEMPLATES)
    if arch_key != "product_data_strategy":
        fallback_pool.append(DOMAIN_ARCHETYPES["product_data_strategy"]["grill_templates"][0])
    for fb in fallback_pool:
        if len(selected) >= max_q:
            break
        if any(q["dimension"] == fb["dimension"] for q in selected):
            continue
        selected.append(
            {
                "id": f"grill_q{len(selected) + 1}",
                "dimension": fb["dimension"],
                "question": fb["question"],
                "why_it_matters": fb["why_it_matters"],
                "options": list(fb["options"]),
                "recommended_default": fb["options"][0],
            }
        )

    ambiguity = _assess_ambiguity(
        raw_task,
        constraints=[known_context] if known_context.strip() else None,
    )
    ready = (not ambiguity["is_ambiguous"]) or bool(auto_aligned_dimensions) or bool(
        re.search(r"(直接执行|无需追问|按默认|all|p0)", known_low)
    )

    ask_question_payload = {
        "questions": [
            {
                "question": f"【{q['dimension']}】{q['question']}",
                "options": q["options"],
                "is_multi_select": False,
            }
            for q in selected
        ]
    }

    md_lines = [
        f"### 🔥 Grill-Me 关键分歧对齐单（领域：{arch['label']}）",
        f"- **原始需求**：`{raw_task[:120]}`",
        f"- **模糊度评估**：`{ambiguity['ambiguity_score']}`（{'建议先确认以下 2~3 个关键权衡点再动工' if not ready else '边界较清晰，可直接按 (Recommended) 默认最优解推进'}）",
    ]
    if auto_aligned_dimensions:
        md_lines.append(f"- **已从上下文自动对齐维度**：`{', '.join(auto_aligned_dimensions)}`")
    md_lines.append("")
    for i, q in enumerate(selected, start=1):
        md_lines.append(f"{i}. **[{q['dimension']}] {q['question']}**")
        md_lines.append(f"   - *架构影响*：{q['why_it_matters']}")
        for opt in q["options"]:
            md_lines.append(f"   - `{opt}`")

    return {
        "ok": True,
        "raw_task": raw_task,
        "domain": domain or arch["label"],
        "domain_archetype": arch_key,
        "ambiguity_score": ambiguity["ambiguity_score"],
        "missing_dimensions": ambiguity["missing_dimensions"],
        "auto_aligned_dimensions": auto_aligned_dimensions,
        "ready_to_execute": ready,
        "grill_questions": selected,
        "ask_question_payload": ask_question_payload,
        "convergence_summary_markdown": "\n".join(md_lines),
    }


def _build_altitude_depth_breadth(
    raw_task: str,
    domain: str,
    arch: dict[str, Any],
    constraints: list[str] | None = None,
) -> dict[str, Any]:
    """构建『高度 (Altitude) · 深度 (Depth) · 广度 (Breadth)』三维深度思考矩阵。"""
    clean_task = raw_task.strip()
    c_list = [str(c).strip() for c in (constraints or []) if c and str(c).strip()]
    ambiguity = _assess_ambiguity(clean_task, c_list)

    verified_inputs: list[str] = [f"用户显式任务目标：{clean_task[:120]}"]
    if domain.strip():
        verified_inputs.append(f"指定专业领域：{domain.strip()}")
    if c_list:
        verified_inputs.extend(f"已确认硬约束：{c}" for c in c_list)

    unverified_claims: list[str] = [
        f"围绕『{clean_task[:50]}』的改动在未跑通外部客观裁判（{arch['outside_judge']}）前，一切有效性均属待核验假设（Unverified Claim）"
    ]
    numbers_found = re.findall(r"\d+(?:\.\d+)?\s*(?:ms|s|秒|倍|%|万|k|fps|gb|mb)", clean_task.lower())
    if numbers_found:
        unverified_claims.append(
            f"任务中提及的量化指标（{', '.join(numbers_found)}）需锁死基准环境、样本规模与统计口径后方可通过核验"
        )

    missing_conditions = list(ambiguity["missing_dimensions"]) or [
        "需在执行期核对一手原始源码/数据（Rawest Material）的物理路径与基线状态"
    ]

    return {
        "altitude": {
            "dimension_label": "第一维：高度（Altitude · 去包装看本质 & 从单点干活升维到根治闭环）",
            "surface_single_task": clean_task,
            "goal_beyond_the_goal": (
                f"跳出『{clean_task[:60]}』的单次表面交付，向上追问一层：消除该类问题背后的系统性人工摩擦与回归隐患，"
                f"构建可复现、可根治、可自动验证的持久工程闭环。"
            ),
            "durable_reproduce_fix_verify_loop": arch["altitude_loop"],
            "validity_preconditions": (
                c_list
                if c_list
                else [
                    "明确输入数据/源码的物理边界与兼容契约",
                    "限定目标运行环境（OS / Python 或宿主版本）与超时预算",
                    f"绑定可自动执行的外部验收门禁：{arch['outside_judge']}",
                ]
            ),
        },
        "depth": {
            "dimension_label": "第二维：深度（Depth · 去专家人设开光、一手证据三分法、真工具与极限测量口径）",
            "anti_persona_rule": (
                "严禁使用『你是某某领域专家』的表面身份标签开光（那只会切换词汇话术而不会切换方法）；"
                "必须直接指定行业顶尖高手的底层分析动作、输入材料与核验工具。"
            ),
            "unasked_expert_action": arch["unasked_expert_action"],
            "rawest_material": arch["rawest_material"],
            "evidence_triage_3state": {
                "verified_facts": verified_inputs,
                "unverified_claims": unverified_claims,
                "missing_conditions": missing_conditions,
            },
            "professional_methods_and_tools": arch["specific_tools"],
            "extreme_target_and_measurement_caliber": arch["depth_challenge_caliber"],
            "outside_judge_and_honesty_gate": (
                f"{arch['outside_judge']}（铁律：未实际跑过验证的步骤一律标记为『待验证』，严禁伪造『已验证』！）"
            ),
        },
        "breadth": {
            "dimension_label": "第三维：广度（Breadth / Range · 名词转动词受众决策倒逼 & 跨界工作流迁移）",
            "noun_to_verb_reframe": arch["breadth_verb_shift"],
            "audience_decision_effect": (
                "用最终受众/下游调用方在限定时间内的决策效果与零返工率，倒逼信息取舍与接口契约收敛，砍除一切无助于决策的充数废话。"
            ),
            "cross_domain_workflows": arch["cross_domain_crew"],
            "anti_roleplay_iron_law": "严禁模仿跨界角色（如做空者、审计员）的说话腔调，只提取并执行其底层研究方法与查证清单。",
        },
    }


def _build_red_blue_confrontation(
    raw_task: str,
    domain: str,
    arch: dict[str, Any],
    evidence: str = "",
) -> dict[str, Any]:
    """构建『红蓝对抗（Red-Blue Confrontation）』四步破立攻防与可证伪检查矩阵。"""
    rb = deepcopy(arch["red_blue_matrix"])
    fights: list[dict[str, str]] = list(rb["fights"])

    if evidence.strip():
        fights.insert(
            0,
            {
                "dimension": "一手实测反例/报错破局（Live Evidence Attack）",
                "claim_or_assumption": f"原假设认为『{raw_task[:60]}』当前主路径可直接通过",
                "red_team_attack": f"红军注入一手实测反例/报错证据：{evidence.strip()}",
                "blue_counter_patch": "蓝军反制：立即废弃原脆弱假设（触发 is_revision=True），根据报错堆栈/约束边界切换到备选防御分支并补齐回归断言。",
                "falsifiable_experiment": "针对该报错证据编写专属回归单测，验证修复后退出码为 0 且同类输入不再复现异常。",
            },
        )

    return {
        "protocol": "Red-Blue Confrontation v1.2（🔵 蓝军立论 ➔ 🔴 红军四命门破局 ➔ 🛡️ 蓝军反制补丁 ➔ 🔬 可证伪实验与外部裁判终审）",
        "blue_team_defense": {
            "role": "🔵 蓝军（Blue Team · 架构建构与防御主创）",
            "core_thesis": rb["blue_thesis"],
            "target_task": raw_task.strip()[:120],
        },
        "red_team_attacks": [
            f"[{item['dimension']}] {item['red_team_attack']}" for item in fights
        ],
        "blue_team_counter_hardening": [
            f"[{item['dimension']}] {item['blue_counter_patch']}" for item in fights
        ],
        "falsifiable_check_matrix": fights,
        "outside_judge_verdict": arch["outside_judge"],
    }


def _build_product_4step(raw_task: str, domain: str, arch: dict[str, Any]) -> dict[str, Any]:
    return {
        "step1_core_pain_point": (
            f"剥离『{raw_task[:60]}』的表面功能词汇，直击核心摩擦：解决现有流程中因静态模板、手工重复操作或盲区漏检导致的返工与上下文损耗。"
        ),
        "step2_mvp_priority_triage": {
            "P0_must_have": "打通核心主干闭环、保持既有接口 100% 向后兼容并通过自动化验证门禁",
            "P1_should_have": "增强高度·深度·广度三维推断、红蓝对抗攻防加固与结构化可读输出",
            "P2_cut_for_now": "砍除一切需要额外部署守护进程、重型外部依赖或纯表面角色扮演的冗余包装",
        },
        "step3_contract_prd": {
            "input_contract": "明确必填参数、可选模式枚举（mode）与边界异常兜底，拒绝隐式全局状态污染",
            "output_contract": "固定返回可机读 JSON 结构（含三维升维矩阵与红蓝对抗表）与清晰 Markdown 执行蓝图",
            "outside_judge": arch["outside_judge"],
        },
        "step4_atomic_tasks": [
            "Task 1 [高度与一手证据锁定]：读取最原始一手源码/数据（Rawest Material），盘点已核验事实 vs 待核验声明并确认兼容性约束",
            "Task 2 [深度真工具与核心实现]：按 P0 范围调用专业方法编写零副作用核心逻辑，锁死测量口径",
            "Task 3 [广度转换与红蓝对抗证伪]：执行红军四命门攻击（水分/边界/成本/安全）与蓝军反制补丁，构造极端边界隔离单测",
            "Task 4 [跨端同步与外部裁判终审]：运行外部客观裁判核验退出码与签名对账，未跑通项严禁标已验证",
        ],
    }


def _build_sequential_branch_plan(raw_task: str, arch: dict[str, Any]) -> dict[str, Any]:
    return {
        "primary_hypothesis": f"主路径（Main Branch · 蓝军主方案）：围绕『{arch['label']}』采用零外部守护进程的内聚增强方案，通过 {arch['specific_tools']} 直接闭环。",
        "alternative_branch": "正交备选分支（Alt Branch · 红蓝反制回退路径）：若红军证伪或实测表明单次内聚处理超出上下文/延迟预算，则拆分为『前置轻量分歧收敛（Grill-Me）+ 后台分步流式验证（Sequential Reflect）』双阶段流水线。",
        "revision_trigger": "当红军可证伪实验或外部客观裁判（Outside Judge）检出回归测试失败、签名不匹配或 Token 体积膨胀 >30% 时，立即触发 is_revision=True 回退修正假设。",
        "outside_judge": arch["outside_judge"],
    }


def tools_list() -> list[dict]:
    return [
        {
            "name": "ambitious_thinking_guide",
            "description": "获取 Claude 技术主管 Felix Rieseberg「高度 · 深度 · 广度 + 红蓝对抗」深度思考法（高度 Altitude、深度 Depth、广度 Breadth/Range、红蓝对抗 Red-Blue Confrontation、四问深度重写元指令、Grill-Me 追问对齐协议与 AI 产品经理四步法）完整技能指南与中英双语原文。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": [
                            "skill",
                            "reference",
                            "meta_prompt",
                            "grill_protocol",
                            "product_4step",
                            "altitude_depth_breadth",
                            "red_blue_protocol",
                            "all",
                        ],
                        "description": "要加载的内容模块：skill（执行工作流）、reference（Felix原文与解读）、meta_prompt（中英四问重写提示词）、grill_protocol（Grill-Me追问收敛协议）、product_4step（AI产品设计四步法）、altitude_depth_breadth（高度·深度·广度三维方法论）、red_blue_protocol（红蓝对抗与可证伪核验协议）、all（全部）",
                    }
                },
                "required": [],
            },
        },
        {
            "name": "ambitious_prompt_rewrite",
            "description": "将用户的原始浅层任务/提示词套入「高度 · 深度 · 广度 + 红蓝对抗」深度思考框架 v1.2，生成包含 Grill-Me 关键分歧追问、高度根治闭环、深度去人设真工具与证据口径、广度名词转动词与跨界迁移、红蓝对抗可证伪攻防矩阵、Sequential 动态分支推演与 AI 产品四步法的结构化深度思考指令包。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "raw_task": {
                        "type": "string",
                        "description": "用户的原始任务或初始提示词（Prompt）",
                    },
                    "domain": {
                        "type": "string",
                        "description": "任务所属的专业领域（如：软件架构、多智能体/MCP、UI视觉动效、高并发分布式系统、产品设计等）",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["zh", "en", "bilingual"],
                        "description": "输出语言偏好（默认 zh）",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "grill_first", "direct_rewrite", "product_to_code"],
                        "description": "重写与收敛模式：auto（自动评估模糊度并附带 Grill-Me 追问）、grill_first（先追问关键分歧）、direct_rewrite（直接展开四维深度重写）、product_to_code（AI产品四步法直通代码拆解）",
                    },
                    "constraints": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "已明确的硬性约束条件列表（可选）",
                    },
                },
                "required": ["raw_task"],
            },
        },
        {
            "name": "ambitious_grill_alignment",
            "description": "【Grill-Me 深度追问对齐器】在复杂或模糊任务动工前，精准提炼 2~3 个一阶架构权衡点（附正反代价与 Recommended 首推选项）及可直接传给 ask_question 的交互载荷，消除盲目长篇输出。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "raw_task": {
                        "type": "string",
                        "description": "待对齐的原始任务或需求描述",
                    },
                    "domain": {
                        "type": "string",
                        "description": "任务所属专业领域（可选，未传则自动根据语义识别）",
                    },
                    "known_context": {
                        "type": "string",
                        "description": "已知上下文或用户已拍板的约束条件（可选）",
                    },
                    "max_questions": {
                        "type": "integer",
                        "description": "最大追问问题数（默认 3，范围 1~4）",
                    },
                },
                "required": ["raw_task"],
            },
        },
        {
            "name": "ambitious_sequential_reflect",
            "description": "【Sequential Thinking 动态分支推演与红蓝对抗证伪器】融合高度·深度·广度三维校验、sequentialthinking 动态分支与红蓝对抗（Red-Blue Confrontation）机制，支持分步思考状态推演、正交分支探索（Branching）、基于一手证据的假设证伪修正（Revision）与外部客观裁判门禁。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_or_hypothesis": {
                        "type": "string",
                        "description": "当前待推演的任务目标、设计方案或架构假设",
                    },
                    "thought_step": {
                        "type": "integer",
                        "description": "当前思考步序号（从 1 开始，默认 1）",
                    },
                    "total_thoughts": {
                        "type": "integer",
                        "description": "预计总思考步数（默认 3，可根据推演复杂度动态调整）",
                    },
                    "is_revision": {
                        "type": "boolean",
                        "description": "当前步是否为推翻/修正前序假设的反证步（默认 false）",
                    },
                    "revises_step": {
                        "type": "integer",
                        "description": "若 is_revision 为 true，指明所修正的前序步号",
                    },
                    "branch_from_step": {
                        "type": "integer",
                        "description": "若当前为分叉探索，指明从第几步分叉",
                    },
                    "branch_id": {
                        "type": "string",
                        "description": "当前思考分支标识（默认 'main'）",
                    },
                    "evidence_or_constraint": {
                        "type": "string",
                        "description": "新发现的一手客观证据、报错信息或硬边界约束（可选）",
                    },
                    "domain": {
                        "type": "string",
                        "description": "任务所属专业领域（可选）",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "有状态思考链会话标识（默认 'default'，用于在多步推演间追踪分支与已推翻步骤）",
                    },
                    "reset_session": {
                        "type": "boolean",
                        "description": "是否清空当前 session_id 的历史思考链重新开始（默认 false）",
                    },
                },
                "required": ["task_or_hypothesis"],
            },
        },
    ]


TOOLS = tools_list()


def call_tool(name: str, args: dict) -> dict:
    if name == "ambitious_thinking_guide":
        section = args.get("section", "skill")
        result: dict[str, Any] = {
            "mod": "ambitious-deep-thinking",
            "version": VERSION,
            "meta_prompt_zh": DEEP_PROMPT_ZH,
            "meta_prompt_en": DEEP_PROMPT_EN,
        }
        if section in ("skill", "all") and SKILL_MD.is_file():
            result["skill_markdown"] = SKILL_MD.read_text(encoding="utf-8")
        if section in ("reference", "all") and REF_MD.is_file():
            result["reference_markdown"] = REF_MD.read_text(encoding="utf-8")
        if section in ("grill_protocol", "all"):
            result["grill_protocol_markdown"] = GRILL_PROTOCOL_MD
        if section in ("product_4step", "all"):
            result["product_4step_markdown"] = PRODUCT_4STEP_MD
        if section in ("altitude_depth_breadth", "all"):
            result["altitude_depth_breadth_markdown"] = ALTITUDE_DEPTH_BREADTH_MD
        if section in ("red_blue_protocol", "all"):
            result["red_blue_protocol_markdown"] = RED_BLUE_PROTOCOL_MD
        return result

    if name == "ambitious_grill_alignment":
        raw_task = str(args.get("raw_task", "")).strip()
        if not raw_task:
            raise ValueError("raw_task is required and cannot be empty")
        domain = str(args.get("domain", "")).strip()
        known_context = str(args.get("known_context", "")).strip()
        max_questions = int(args.get("max_questions", 3) or 3)
        return _build_grill_questions(raw_task, domain, known_context, max_questions)

    if name == "ambitious_sequential_reflect":
        hypothesis = str(args.get("task_or_hypothesis", "")).strip()
        if not hypothesis:
            raise ValueError("task_or_hypothesis is required and cannot be empty")
        step = max(1, int(args.get("thought_step", 1) or 1))
        total = max(step, int(args.get("total_thoughts", 3) or 3))
        is_rev = bool(args.get("is_revision", False))
        revises_step = args.get("revises_step")
        branch_from = args.get("branch_from_step")
        branch_id = str(args.get("branch_id", "main") or "main").strip()
        evidence = str(args.get("evidence_or_constraint", "") or "").strip()
        domain = str(args.get("domain", "") or "").strip()
        session_id = str(args.get("session_id", "default") or "default").strip()
        reset_session = bool(args.get("reset_session", False))

        arch_key, arch = _detect_domain_archetype(hypothesis, domain)
        stage_map = {
            1: "Stage 1 · 高度升维（Altitude 根治闭环）与 Grill-Me 一阶分歧锁定",
            2: "Stage 2 · 深度穿透（Depth 去人设真工具、一手证据三分法与极限口径）",
            3: "Stage 3 · 广度扩维（Breadth 名词转动词）与红蓝对抗（Red-Blue Confrontation）可证伪闭环",
        }
        if is_rev:
            stage_focus = f"Revision Step（修正第 {revises_step or max(1, step - 1)} 步）· 基于红军反例/新证据推翻原假设并启用蓝军反制分支"
        elif branch_from is not None:
            stage_focus = f"Branch Exploration（自第 {branch_from} 步分叉 -> `{branch_id}`）· 探索正交备选技术路线"
        else:
            stage_focus = stage_map.get(step, f"Stage {step} · 红蓝对抗可证伪实验与外部客观裁判终审")

        needs_revision = bool(
            evidence and any(w in evidence.lower() for w in ["error", "fail", "timeout", "超限", "报错", "失败", "冲突", "不兼容"])
        )
        next_needed = step < total or needs_revision

        ledger_summary = _record_session_step(
            session_id=session_id,
            reset_session=reset_session,
            entry={
                "thought_step": step,
                "branch_id": branch_id,
                "task_or_hypothesis": hypothesis,
                "is_revision": is_rev,
                "revises_step": int(revises_step) if revises_step is not None else None,
                "branch_from_step": int(branch_from) if branch_from is not None else None,
                "evidence_or_constraint": evidence,
                "needs_revision": needs_revision,
            },
        )

        adb_matrix = _build_altitude_depth_breadth(
            hypothesis,
            domain or arch["label"],
            arch,
            constraints=[evidence] if evidence else None,
        )
        red_blue = _build_red_blue_confrontation(hypothesis, domain or arch["label"], arch, evidence=evidence)

        return {
            "ok": True,
            "domain_archetype": arch_key,
            "domain_label": arch["label"],
            "state": {
                "thought_step": step,
                "total_thoughts": total + (1 if needs_revision and step >= total else 0),
                "branch_id": branch_id,
                "is_revision": is_rev,
                "revises_step": int(revises_step) if revises_step is not None else None,
                "branch_from_step": int(branch_from) if branch_from is not None else None,
                "next_thought_needed": next_needed,
            },
            "session_ledger": ledger_summary,
            "stage_focus": stage_focus,
            "current_analysis": {
                "unasked_expert_action": arch["unasked_expert_action"],
                "rawest_material_target": arch["rawest_material"],
                "professional_instruments": arch["specific_tools"],
                "evidence_incorporated": evidence if evidence else "尚无外部反例注入，按蓝军主干假设推进并启动红军四命门压力测试。",
            },
            "altitude_depth_breadth_matrix": adb_matrix,
            "red_blue_confrontation": red_blue,
            "for_vs_against_matrix": {
                "for_arguments": [
                    f"【🔵 蓝军立论】{red_blue['blue_team_defense']['core_thesis']}",
                    f"【🛡️ 蓝军反制补丁】{red_blue['blue_team_counter_hardening'][0]}",
                ],
                "against_red_team": [
                    f"【🔴 红军方法】{arch['red_team_method']}",
                    *red_blue["red_team_attacks"][:2],
                ],
            },
            "branch_exploration": _build_sequential_branch_plan(hypothesis, arch),
            "product_engineering_4step": _build_product_4step(hypothesis, domain, arch),
            "outside_judge_gate": arch["outside_judge"],
            "needs_revision": needs_revision,
            "next_step_directive": (
                f"红军/实测检出反例故障信号（{evidence[:60]}），下一步请设置 is_revision=True, revises_step={step} 切换到蓝军反制分支修正方案。"
                if needs_revision
                else (
                    f"请继续推进 thought_step={step + 1}（共 {total} 步），完成高度·深度·广度核验与红蓝对抗可证伪测试。"
                    if next_needed
                    else "全部思考步与红蓝对抗已收敛，请立即按 product_engineering_4step.step4_atomic_tasks 执行交付并运行外部客观裁判！"
                )
            ),
        }

    if name == "ambitious_prompt_rewrite":
        raw_task = str(args.get("raw_task", "")).strip()
        domain_in = str(args.get("domain", "")).strip()
        lang = str(args.get("language", "zh") or "zh").strip()
        mode = str(args.get("mode", "auto") or "auto").strip()
        constraints = args.get("constraints") or []
        if not isinstance(constraints, list):
            constraints = [str(constraints)]

        arch_key, arch = _detect_domain_archetype(raw_task, domain_in)
        domain = domain_in or arch["label"]
        ambiguity = _assess_ambiguity(raw_task, constraints)
        effective_mode = ambiguity["recommended_mode"] if mode == "auto" else mode
        grill_pack = _build_grill_questions(raw_task, domain, " ".join(str(c) for c in constraints), max_questions=3)
        seq_plan = _build_sequential_branch_plan(raw_task, arch)
        prod_4step = _build_product_4step(raw_task, domain, arch)
        adb_matrix = _build_altitude_depth_breadth(raw_task, domain, arch, constraints)
        red_blue = _build_red_blue_confrontation(raw_task, domain, arch)

        grill_brief_zh = "\n".join(
            f"  - Q{idx} [{q['dimension']}]：{q['question']}\n    首推默认解：{q['recommended_default']}"
            for idx, q in enumerate(grill_pack["grill_questions"], start=1)
        )
        red_blue_brief_zh = "\n".join(
            f"  - ⚔️ 【{f['dimension']}】\n"
            f"    · 🔴 红军破局：{f['red_team_attack']}\n"
            f"    · 🛡️ 蓝军反制：{f['blue_counter_patch']}\n"
            f"    · 🔬 可证伪实验：{f['falsifiable_experiment']}"
            for f in red_blue["falsifiable_check_matrix"]
        )

        composed_zh = (
            f"【原始任务】\n{raw_task}\n\n"
            f"【所属领域】{domain}（识别原型：`{arch_key}` · 模式：`{effective_mode}`）\n\n"
            f"【第零阶：Grill-Me 关键分歧对齐（先收敛核心权衡，拒绝盲目长篇空谈）】\n"
            f"{grill_brief_zh}\n\n"
            f"【第一维：高度升维（Altitude - 去包装看本质、从单点修复升维到根治闭环 & 产品四步法）】\n"
            f"- 向上追问一层（Goal beyond the goal）：{adb_matrix['altitude']['goal_beyond_the_goal']}\n"
            f"- 根治级持久闭环（复现 ➔ 根治 ➔ 自验）：{adb_matrix['altitude']['durable_reproduce_fix_verify_loop']}\n"
            f"- MVP 优先级砍削：P0={prod_4step['step2_mvp_priority_triage']['P0_must_have']} ｜ 本轮显式砍除={prod_4step['step2_mvp_priority_triage']['P2_cut_for_now']}\n\n"
            f"【第二维：深度穿透（Depth - 拒绝“你是某某专家”人设开光、一手证据三分法、真工具与极限测量口径）】\n"
            f"{DEEP_PROMPT_ZH}\n"
            f"- 去人设换真动作：{adb_matrix['depth']['unasked_expert_action']}\n"
            f"- 一手原始素材（区分已核验事实 vs 待核验声明）：{adb_matrix['depth']['rawest_material']}\n"
            f"- 专业方法与工具：{adb_matrix['depth']['professional_methods_and_tools']}\n"
            f"- 挑战性极限目标与锁死口径：{adb_matrix['depth']['extreme_target_and_measurement_caliber']}\n"
            f"- 外部客观裁判（Outside Judge）：{adb_matrix['depth']['outside_judge_and_honesty_gate']}\n\n"
            f"【第三维：广度扩维（Breadth / Range - 名词转动词受众决策倒逼 & 跨界真实工作流迁移）】\n"
            f"- 名词转动词（受众决策效果倒逼）：{adb_matrix['breadth']['noun_to_verb_reframe']}\n"
            f"- 跨界特种战队工作流（严禁表面角色扮演腔调）：{adb_matrix['breadth']['cross_domain_workflows']}\n\n"
            f"【第四维：红蓝对抗（Red-Blue Confrontation - 蓝军立论 ➔ 红军破局 ➔ 蓝军反制 ➔ 可证伪实验）】\n"
            f"- 🔵 蓝军核心立论：{red_blue['blue_team_defense']['core_thesis']}\n"
            f"- 🔄 动态分支回退触发：{seq_plan['revision_trigger']}\n"
            f"{red_blue_brief_zh}"
        )
        composed_en = (
            f"[Original Task]\n{raw_task}\n\n"
            f"[Domain] {domain} (Archetype: `{arch_key}` | Mode: `{effective_mode}`)\n\n"
            f"[Stage 0: Grill-Me Trade-off Alignment (Converge on 2-3 key decisions before writing long plans)]\n"
            f"Surface the top architectural trade-offs with (Recommended) defaults.\n\n"
            f"[Dimension 1: Altitude (Root-Cause Durable Loop & Product 4-Step Workflow)]\n"
            f"Go up in abstraction: Elevate from single-point task execution to a durable 'Reproduce -> Root-Cause Fix -> Auto-Verify' loop with explicit validity preconditions and P0/P2 MVP triage.\n\n"
            f"[Dimension 2: Depth (No Expert Persona Roleplay, 3-State Evidence Triage & Extreme Caliber)]\n"
            f"{DEEP_PROMPT_EN}\n\n"
            f"[Dimension 3: Breadth / Range (Noun-to-Verb Audience Decision Reframing & Cross-Domain Workflows)]\n"
            f"Replace static deliverable nouns with time-bounded audience-decision verbs, and borrow real verification workflows from adjacent disciplines without roleplay tone.\n\n"
            f"[Dimension 4: Red-Blue Confrontation (Blue Defense -> Red 4-Vector Attack -> Blue Counter-Hardening -> Falsifiable Checks)]\n"
            f"Execute adversarial Red-Blue teaming ({arch['red_team_method']}), supply concrete engineering counter-patches for every Red Team attack, and verify solely via the outside judge ({arch['outside_judge']})."
        )
        return {
            "raw_task": raw_task,
            "domain": domain,
            "version": VERSION,
            "mode_used": effective_mode,
            "ambiguity_assessment": {
                **ambiguity,
                "domain_archetype": arch_key,
            },
            "grill_me_questions": grill_pack["grill_questions"],
            "altitude_depth_breadth_matrix": adb_matrix,
            "red_blue_confrontation": red_blue,
            "sequential_branch_plan": seq_plan,
            "product_engineering_4step": prod_4step,
            "deep_rewrite_meta_prompt_zh": DEEP_PROMPT_ZH,
            "deep_rewrite_meta_prompt_en": DEEP_PROMPT_EN,
            "ambitious_execution_prompt": composed_zh if lang == "zh" else (composed_en if lang == "en" else f"{composed_zh}\n\n---\n\n{composed_en}"),
            "checklist": [
                "0. 分歧收敛 (Grill-Me)：是否已针对模糊需求提炼 2~3 个核心架构权衡点并给出 (Recommended) 默认解？",
                "1. 高度 (Altitude)：是否已从修单点问题升维到『定位复现 ➔ 根治执行 ➔ 自主验证』持久闭环并明确成立前提？",
                "2. 产品四步法 (Product 4-Step)：是否已完成『真痛点锚定 ➔ P0/P2 优先级砍削 ➔ 契约级 PRD ➔ 原子任务拆解』？",
                "3. 深度-去人设与隐性动作 (Depth Q1)：是否摒弃了『你是某某专家』表面话术，列出了顶尖高手会做的隐性加固动作？",
                "4. 深度-证据三分法 (Depth Q2)：是否直取一手原始素材，并严格区分了『已核验事实 vs 待核验声明 vs 缺失条件』？",
                "5. 深度-真工具与极限口径 (Depth Q3)：是否指定了底层专业工具，并在设定挑战性极限目标时锁死了测量口径？",
                "6. 深度-外部客观裁判 (Depth Q4)：是否绑定了独立外部裁判（outside judge），且将未跑通项如实标记为『待验证』？",
                "7. 广度与红蓝对抗 (Breadth & Red-Blue)：是否完成了名词转动词受众决策转化、跨界工作流迁移，并走完了『蓝军立论 ➔ 红军四命门破局 ➔ 蓝军反制补丁 ➔ 可证伪实验』闭环？",
            ],
        }

    raise ValueError(f"Unknown tool: {name}")


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        method = req.get("method")
        req_id = req.get("id")
        if method == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "ambitious_deep_thinking", "version": VERSION},
                },
            }
            print(json.dumps(resp, ensure_ascii=False), flush=True)
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools_list()}}
            print(json.dumps(resp, ensure_ascii=False), flush=True)
        elif method == "tools/call":
            params = req.get("params", {})
            t_name = params.get("name", "")
            t_args = params.get("arguments") or {}
            try:
                data = call_tool(t_name, t_args)
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
                        "isError": False,
                    },
                }
            except Exception as exc:
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps({"error": str(exc)}, ensure_ascii=False)}],
                        "isError": True,
                    },
                }
            print(json.dumps(resp, ensure_ascii=False), flush=True)
        elif req_id is not None:
            print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {}}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
