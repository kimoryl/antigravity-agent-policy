# Antigravity Agent Policy & `@core` Base Mods

面向 **Antigravity IDE / AGY-CLI / Codex-CLI / Claude-Code-CLI** 的统一智能体监督适配层（Adapter Layer）与 `@core` 五大核心底座模组开源仓库。

---

## 一、解决的核心问题

原生多智能体与长周期编码会话通常面临四大工程痛点：
1. **上下文膨胀与工具刷屏**：全量挂载数十个 MCP 工具与冗长构建日志会迅速耗尽上下文窗口并触发 Prompt Cache 失效（Cache Tax）。
2. **缺少确定性边界防护**：子代理递归失控、重复报错死循环、无锚点大段覆盖写坏代码、未跑测试直接宣称完成。
3. **跨会话与跨 CLI 失忆**：不同智能体之间缺乏客观项目真值共享，离线记忆中的第三方 SDK 签名易过期产生幻觉。
4. **同源模型自审盲区**：单一模型既写代码又做验收，难以发现深层边界漏洞与假绿测试。

---

## 二、架构总览（适配层 + `@core` 五大核心底座）

```text
antigravity-agent-policy/
├── plugin.json / .mcp.json / hooks.json   # 插件清单、统一 MCP 短命名入口 (ap) 与生命周期钩子
├── hooks/                                 # Antigravity & CLI 双端确定性拦截钩子 (PreToolUse / PostToolUse / Stop)
├── scripts/                               # Global Runtime V1、Supervisor 监督内核、锚点编辑 (hash_edit)、Mod 挂载引擎
├── rules/ & skills/                       # 受控技术语法规范与按需加载技能池
├── ui/                                    # 本地回环可视化态势与人工交付审批看板 (Dashboard)
└── mods/                                  # @core 五大核心底座 Mod
    ├── code-intelligence/                 # 多语言静态 AST、调用链追踪、PageRank RepoMap 与跨边界契约图谱
    ├── source-connectors/                 # 零守护进程 Obsidian FTS5 + 384d 局部语义向量 + 二跳双链混合检索与爬虫
    ├── ambitious-deep-thinking/           # 动工前关键分歧追问 (Grill-Me)、高度/深度/广度升维与红蓝对抗推演
    ├── quota-monitor/                     # 多模型池实时配额水位线路由、CLI 输出折叠净化与空转熔断守卫
    └── context7-sdk-verifier/             # Project × Modality × Global 三维交叉记忆只读预检与机械事实对账引擎
```

### 1. 适配层（`antigravity-agent-policy`）
- **Supervisor Engine 确定性监督内核**：内置 `SessionTelemetry`（拦截连续重复失败调用）、`CoOccurrenceGraph`（关联修改提示）、`EvidenceLedger`（实质性代码修改后强制测试闭环）与 `Ring-0 Guard`（保护核心策略与 `.git/hooks` 免遭篡改）。
- **4 字符块哈希锚点编辑（`hash_outline` / `hash_read` / `hash_edit`）**：先提取 AST 符号骨架，再携带块级乐观锁哈希精准落盘，杜绝盲改覆盖。
- **异构制衡与 `/fleet` 隔离协作**：支持 Git Worktree 物理隔离并发执行、1KB JSON 契约对齐，以及本地回环 Dashboard 人工版本验收。

### 2. `@core` 五大核心底座 Mod
| Mod ID | 版本 | 核心技术与职责 |
| :--- | :---: | :--- |
| `code-intelligence` | `v2.1.0` | 多语言静态 AST + SQLite SHA256 增量缓存 + 个性化 PageRank RepoMap + HTTP/gRPC/Proto 跨边界追踪 |
| `source-connectors` | `v3.1.0` | SQLite FTS5 BM25 + 384 维确定性局部语义向量 + 二跳 `[[WikiLinks]]` 图扩散激活 + 有界断点文档爬虫 |
| `ambitious-deep-thinking` | `v1.2.0` | 关键分歧追问对齐、高度/深度/广度三维推演与红蓝可证伪矩阵加固 |
| `quota-monitor` | `v1.4.0` | 三池配额感知路由、CLI 高噪日志折叠压缩（节省 60%~85% 无效 Token）与 180s 零进展熔断守卫 |
| `context7-sdk-verifier` | `v1.1.0` | 三维正交记忆（纵向项目 × 横向工艺 × 全局经验）只读预检、盲审隔离对账与双端原子写入 |

---

## 三、快速安装与验证

### 1. 环境要求
- Python `>= 3.11`（推荐 Python `3.12`，核心运行时基于 Python 标准库与内置 `sqlite3` FTS5，零重型外部依赖）。

### 2. 安装至 Antigravity 插件目录
```bash
git clone https://github.com/kimoryl/antigravity-agent-policy.git ~/.gemini/config/plugins/antigravity-agent-policy
```

### 3. 可选环境变量
所有状态目录默认自动收敛在用户主目录 `~/.antigravity-agent-policy/` 与系统本地应用数据目录下，亦可通过环境变量自定义：
- `AGY_POLICY_DATA` / `CODEX_AGENT_POLICY_DATA`：运行时 SQLite 状态与事件轨迹存储目录。
- `AGY_MODS_DIR`：自定义扩展 Mod 仓库目录（默认使用本仓库内置 `mods/`）。
- `AGY_MEMORY_SYNC_DIR`：三维记忆同步主目录（默认 `~/.antigravity-agent-policy/05_Agent_Memory_Sync`）。
- `OBSIDIAN_VAULT_ROOT`：本地 Markdown 知识库目录（默认 `~/.antigravity-agent-policy/vault`）。

### 4. 运行全量回归测试
```bash
# 1. 运行适配层全量单元测试
python -X utf8 -m unittest discover -s tests -v

# 2. 运行 @core 五大核心底座 Mod 单元测试
python -X utf8 -m unittest discover -s mods/code-intelligence/tests -v
python -X utf8 -m unittest discover -s mods/source-connectors/tests -v
python -X utf8 -m unittest discover -s mods/ambitious-deep-thinking/tests -v
python -X utf8 -m unittest discover -s mods/quota-monitor/tests -v
python -X utf8 -m unittest discover -s mods/context7-sdk-verifier/tests -v
```

### 5. 启动本地可视化看板（Dashboard）
```bash
python -X utf8 scripts/dashboard_cli.py
```

---

## 四、开源许可（License）

本项目基于 [MIT License](./LICENSE) 开源；部分设计参考的第三方开源协议声明完整保留于 [`THIRD_PARTY_LICENSES/`](./THIRD_PARTY_LICENSES/)。
