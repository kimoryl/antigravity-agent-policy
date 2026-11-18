# CodeGraph v2.1 工程规则

## 静态分析与增量缓存

- 仅用 `ast.parse` 与词法扫描分析源码。
- 禁止导入或执行目标项目代码。
- 跳过 symlink 和 Reparse Point（`0x400`）。
- 在进入目录前剪枝链接与依赖目录。
- 按项目、解析版本和 SHA256 复用 SQLite。
- 用 `CODE_INTELLIGENCE_CACHE_PATH` 隔离缓存。
- 用临时目录隔离全部测试项目。
- 保留原五工具的签名与既有返回字段。
- 同步七工具的清单、注册与 Schema。
- 同时提供相同的 `inputSchema` 与 `parameters`。

## RepoMap PageRank 预算探索

以 `calls`、`inherits`、`imports` 有向边计算个性化 PageRank。保留调用频次权重，将跨文件调用与继承投影到模块节点。使用阻尼系数 `0.85`，迭代 `20` 轮；将悬挂节点质量按个性化先验重新分配。

- 用 `code_repomap_rank` 获取重要接口。
- 用 `focus_files` 聚焦文件。
- 用 `focus_symbols` 聚焦符号。
- 用 `token_budget` 限制签名树。
- 用 `codegraph_explore` 获取邻域摘要。
- 用 `symbol_trace` 查看紧凑调用链。

`estimated_tokens` 使用 UTF-8 字节数作为与模型无关的保守上界。`repomap_markdown` 的完整模块与签名行始终受预算限制；排序元数据不计入此预算。压缩率比较签名摘要与全量图谱 JSON，不表示整个返回字典的压缩率。

## Fallow 静态健康门禁

在已有解析遍历中收集函数圈复杂度、行数、控制流嵌套和文件显式导出。Python 使用 AST；其他语言使用屏蔽注释与字符串后的词法结构。非 Python 结果不保证类型推断或动态分派完整性。

- 用 `code_health_audit` 检查代码健康。
- 检查无外部入边的函数、方法、类。
- 豁免测试、入口、Visitor 和魔术方法。
- 豁免显式导出与指定 `entry_points`。
- 用 Tarjan SCC 提取跨模块循环。
- 按圈复杂度降序返回热点。
- 用 `CODE_INTELLIGENCE_BASELINE_PATH` 隔离基线。
- 核验结果后保存 JSON 基线。
- 依据 `regression_detected` 阻止新增问题。

死代码是静态候选项：反射、依赖注入与动态调用仍需人工核对。递归自调用不视为其他符号使用。测试入口豁免只影响死代码，不隐藏复杂度热点。基线指纹不含行号；已有热点复杂度升高也算回归。健康分数从 100 分扣减：每项死代码 2 分、每个循环 10 分、每个热点按超出阈值的分支数加 1 扣分，下限为 0。

## 跨语言协议契约与 SCIP 索引缝合（Cross-Boundary Symbol Trace）

静态链接 HTTP/gRPC 请求、后端 Handler、OpenAPI 2.0/3.0/3.1、Protobuf（含 `google.api.http` 转码）与 Sourcegraph SCIP 索引（`.scip` wire format 及 `.scip.json`），严禁网络探测或目标代码导入。

- 仅当协议路由或 RPC 身份唯一时建立跨界 `calls` 边（附带 `contract_id`、`scip_symbol`、`evidence`）。
- 对动态路径、未知 Host 或重复端点记录 `diagnostics`，禁止猜测链接。
- 支持直接传入路由路径（如 `GET /v1/users/7`）、`contract_id` 或 `scip-http` / `scip-grpc` 符号调用 `symbol_trace`、`codegraph_explore` 与 `impact_analysis`。
- 用 `export_scip_protocol_index` 导出跨仓库 SCIP 协议索引供上下游仓库静态缝合。
