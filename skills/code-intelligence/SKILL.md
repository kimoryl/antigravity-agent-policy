---
name: code-intelligence
description: CodeGraph v2.1 多语言静态 AST 与增量 SQLite 图谱：符号搜索、调用追踪、影响分析、架构提取、预算邻域、个性化 PageRank RepoMap 和代码健康基线门禁，共七个 MCP 工具。
---

# Code Intelligence & Polyglot CodeGraph Mod (v2.1.0)

本 Mod 提供**零守护进程、无外部二进制依赖、带 SHA256 SQLite 增量缓存**的多语言静态代码知识图谱（支持 `.py/.pyi`, `.ts/.tsx/.js/.jsx/.mjs/.cjs`, `.go`, `.rs`, `.cs`, `.java`, `.c/.cpp/.h/.hpp`）：

1. **`codegraph_explore`（推荐首选）**：
   - 输入 `project_root` 与 `query`（符号名或模块关键字），通过 `max_depth`（默认 2 跳）和 `max_nodes`（默认 25 个节点上限，彻底杜绝外部 CodeGraph 的“胖输出陷阱”）一次性返回：**匹配符号卡片 + 上下游调用与继承边 + 关联文件 + 受影响测试 + SQLite 缓存命中率**。
2. **`code_search`**：
   - 按关键字与符号类型（`class` / `interface` / `function` / `method` / `file`）检索项目符号及签名行号。
3. **`symbol_trace`**：
   - 跨文件与跨语言追踪指定函数/方法、HTTP 路由（如 `"GET /v1/users/7"`）、gRPC 方法（如 `"/shop.v1.Catalog/Get"`）或 SCIP 协议符号（`scip-http ...` / `scip-grpc ...`）的调用方（`callers`）、后端 Handler 与下游被调用方（`callees`），返回 `cross_boundary_edges` 与协议契约证据链。
4. **`impact_analysis`**：
   - 输入拟修改的文件路径列表、符号列表或 Git Diff，沿调用链与继承链反向闭包计算所有受影响的文件、符号及关联测试文件（`test_*`, `*.spec.*`, `*.test.*`）。
5. **`architecture_extract`**：
   - 提取项目级模块节点与跨模块 `imports` / `calls` / `inherits` 拓扑图。

6. **`code_repomap_rank`**：
   输入 `project_root`，可选 `focus_files`、`focus_symbols`、`token_budget=1000`、`provider="polyglot"`。返回符号与模块 PageRank、入度/出度、紧凑模块签名树及压缩率。PageRank 使用阻尼系数 0.85，迭代 20 轮；聚焦参数提高先验权重。

```json
{"project_root":"/workspace/project","focus_files":["src/api.py"],"focus_symbols":["dispatch"],"token_budget":1000}
```

`repomap_markdown` 仅保留完整模块和签名行。`estimated_tokens` 以 UTF-8 字节数作为保守 token 上界，保证不超过预算。`ranked_symbols` 与 `ranked_modules` 元数据不受该摘要预算约束；压缩率比较摘要与全量图谱 JSON。

7. **`code_health_audit`**：
   输入 `project_root`，可选 `entry_points`、`complexity_threshold=10`、`baseline_path`、`save_baseline=False`、`provider="polyglot"`。返回死代码候选、跨模块简单循环、复杂度热点、健康分数、基线差异和 Markdown 摘要。

```json
{"project_root":"/workspace/project","entry_points":["api.external_handler"],"complexity_threshold":10,"baseline_path":"/workspace/state/health.json","save_baseline":true}
```

- 核对首次体检的候选问题。
- 用 `save_baseline=true` 保存基线。
- 用 `save_baseline=false` 检查回归。
- 检查 `baseline_diff.regression_detected`。
- 为动态入口声明 `entry_points`。

新增死代码、循环、热点或已有热点复杂度升高触发回归；解决死代码返回 `resolved_dead_code`。测试文件、测试函数、常见入口、`visit_*`、`__*__`、Python `__all__`、顶层 `TOOLS` / `TOOLS_SCHEMA` 引用及 TS/JS、Go、Rust 显式导出自动豁免死代码候选。

`symbol_trace` 附带 `pagerank_scores`、`cross_boundary_edges` 与 `compact_trace_markdown`。`codegraph_explore` 按 PageRank 返回邻域卡片，并提供 `repomap_digest_markdown`、`token_savings_estimate_pct`。

## 跨语言 HTTP/gRPC 与 SCIP 跨仓库协议缝合

自动扫描工作区内的源码与契约文件（`.json`/`.yaml`/`.yml` OpenAPI、`.proto` 含 `google.api.http` 网关转码、二进制 `.scip` Protobuf 索引及 `.scip.json`）：
- **前端/客户端识别**：`fetch`、`axios`（含 `axios.create` 与 `const` 路径前缀折叠）、Python `requests`/`httpx`/`aiohttp`、Go `http.Get`/`New*Client`、Java `*Grpc.newBlockingStub`、TS/C# `new *Client`/`*Stub`、Rust Tonic `*Client::connect`。
- **后端 Handler 识别**：FastAPI/APIRouter、Flask/Blueprint、Django `urlpatterns`、Express/Router、NestJS `@Controller`、Go `net/http` 与 Gin/Echo `Group`、Java Spring Boot `@RequestMapping`/`@GetMapping`、C# ASP.NET Core `[Route]`/`[HttpGet]`、Rust Actix-web `#[get]`/Axum `.route` 以及 Python/Go/Java/C#/Rust gRPC 服务实现。
- **跨仓 SCIP 索引导出与挂载**：调用 `export_scip_protocol_index(project_root, as_bytes=True)` 导出标准 `scip.Index` 字节流（`.scip`），放入消费方仓库即可静态打通跨仓 `symbol_trace` 与 `impact_analysis`。

全程使用静态 AST 与词法扫描，禁止执行目标代码。遍历跳过 symlink 与 Windows Reparse Point。通过 `CODE_INTELLIGENCE_CACHE_PATH` 和 `CODE_INTELLIGENCE_BASELINE_PATH` 隔离测试状态；缓存按解析版本隔离。非 Python 分析为词法近似，不能证明动态调用完整性；同时检查 `analysis` 的诊断信息。
