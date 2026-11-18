---
name: source-connectors
description: Obsidian 静态知识库与二跳 WikiLinks 图扩散 (v3.1.0)，兼容原有五个 MCP 工具，新增有界文档续爬与 SPA 自适应清洗。
---

# Source Connectors v3.1.0

Markdown Vault 是知识真源。SQLite 保存可重建的章节、FTS5、384d 向量和双链索引。运行依赖 Python 3.12、NumPy 与 PyYAML；不需要 Docker、Ollama、知识库 HTTP 服务或 API Key。

默认镜像 Vault：`~/.antigravity-agent-policy/vault`。
云盘 Vault：`$OBSIDIAN_MIRROR_VAULT`。
索引文件：`~/.antigravity-agent-policy/obsidian_vault_index.sqlite3`。

使用 `OBSIDIAN_VAULT_ROOT`、`OBSIDIAN_GDRIVE_VAULT_ROOT`、`OBSIDIAN_INDEX_DB` 覆盖路径。指定独立 Vault 时，默认关闭生产云盘写入，索引默认位于该 Vault 内。测试必须同时隔离 Vault、镜像、索引和同步状态。

| 目标 | 工具与参数 | 返回 |
| --- | --- | --- |
| 检查状态 | `weknora_kb_manage(action="health")` | Vault 路径与索引计数 |
| 列出知识库 | `weknora_kb_manage(action="list")` | 主 Vault 与目录分类 |
| 创建分类 | `weknora_kb_manage(action="create", kb="分类名")` | 本地目录分类 |
| 列出文档 | `weknora_kb_manage(action="list_docs", kb="分类名")` | 文档 ID、标题与路径 |
| 刷新镜像索引 | `weknora_kb_manage(action="sync_gdrive")` | 增量镜像与索引统计 |
| 检索章节 | `weknora_rag_query(query="...", mode="search", top_k=8)` | 原文、标题路径、行号、双链邻居与分数 |
| 获取问答证据 | `weknora_rag_query(query="...", mode="chat")` | 原文摘录与 `references`，由调用方模型综合回答 |
| 写入笔记 | `weknora_ingest(title="...", content="# Markdown...")` | YAML、自动 WikiLinks 与即时索引 |
| 导入网页 | `weknora_ingest(url="https://...")` | 网页 Markdown 笔记 |
| 提取视频或网页 | `source_ingest_url(url="...", save_to_weknora=true)` | 提取结果及可选 Vault 入库结果 |
| 查询仓库和文档 | `source_connect(operation="repository_search" / "repository_read" / "document_read")` | 保留原有操作 |

工具名、`kb`、`save_to_weknora` 与 `weknora_ingest` 返回键保留兼容含义，内部均使用 Obsidian。历史服务知识库 ID 回退到主 Vault；已创建的目录名可用于分类检索。

检索按 H1–H4 标题树切分，保留代码块和行号，再融合 FTS5 BM25、NumPy 余弦向量检索及默认二跳双链扩散邻居。384d 编码使用确定性概念同义词簇与特征哈希，无模型下载；它覆盖内置概念，不等同于通用训练式 embedding。

同步只调用 `sync_gdrive_to_weknora.py` 的 Markdown 增量镜像入口，再调用 `sync_vault_index()`；不调用旧服务客户端或脚本的记忆写入入口。同步输出写入 stderr，stdout 专用于 MCP JSON-RPC。

自定义 Vault 的镜像同步须显式配置 `OBSIDIAN_SYNC_SCRIPT`；否则只刷新本地索引并返回 `partial`，不加载使用生产目录的默认同步器。同步状态默认保存为 Vault 内 `.obsidian_sync_state.json`，可用 `OBSIDIAN_SYNC_STATE` 覆盖。

视频处理流程：

1. 调用 `source_ingest_url` 提取视频。
2. 读取下载视频的画面与语音。
3. 调用 `weknora_ingest` 保存核验笔记。

保留 `download_mp4_to` 参数。禁止仅凭视频标题生成内容总结。后台子进程必须设置 `CREATE_NO_WINDOW (0x08000000)` 与 `timeout`。

## v3.1.0 图扩散与网页采集

`weknora_rag_query(query="...", graph_hops=2)` 默认沿正向/反向 WikiLinks 扩展至二阶笔记。二阶激活权重为一阶的 `0.45`，最终 RRF 同步衰减；`graph_hops=1` 保留一跳行为，较大值仍最多扩展二阶。搜索与 chat 增加 `graph_hops`、`graph_activated_notes` 统计。

`source_ingest_url(url="...", render_mode="auto", timeout=15)` 保留原有参数和返回键。`http_fast` 只清洗原始 HTML；`auto` 在 SPA 空壳时优先提取 `__NEXT_DATA__`、JSON-LD、`window.__INITIAL_STATE__` 或 `<template>`。`headless_dom` 主动进入该路径。配置可调用的 `mcp_server.DOM_RENDER_HOOK(url, timeout=...)` 后可获取 DOM；未配置或钩子失败时安全返回 HTTP 结果，不启动浏览器。

清洗移除导航、页眉页脚、侧栏、脚本、样式、广告与 Cookie 弹窗，保留 H1–H4、围栏代码和 Markdown 表格。新增 `render_tier_used`、`spa_shell_detected`、`noise_stripped_chars`、`content_sha256`。内容哈希基于清洗正文，不包含来源 URL 和导航噪音。

`source_crawl_documentation(start_url="https://example.org/docs/start", url_prefix="https://example.org/docs/", max_pages=10, max_depth=2, kb=null, save_to_vault=true, resume=true)` 执行同域/前缀有界 BFS。`max_pages` 上限为 50，失败尝试也消耗页面预算；起点深度为 0，默认前缀为起点所在目录。原始超链接在噪音剥离前采集，片段和点段归一化后检查边界。

状态文件依次使用 `OBSIDIAN_CRAWL_STATE`、`OBSIDIAN_VAULT_ROOT/.obsidian_crawl_state.json`、默认镜像 Vault 内同名文件。测试同时设置 `OBSIDIAN_VAULT_ROOT`、`OBSIDIAN_INDEX_DB`、`OBSIDIAN_SYNC_STATE`、`OBSIDIAN_CRAWL_STATE`，并禁用生产云盘镜像。

断点按起点、前缀、分类与 Vault 隔离，逐页原子替换状态。恢复优先处理未完成队列，再重新抓取已知 URL 比较 SHA256；相同内容跳过重复入库，变化内容重新入库。预览抓取不标记为已入库。`resume=false` 重置当前任务账本，保留其他任务。批量笔记标题附加稳定 URL 哈希，防止同名页面互相覆盖。

六个 MCP 工具均提供等值的 `inputSchema` 与 `parameters`。`call_tool` 导出为 `dispatch` 别名，stdout 仅承载 JSON-RPC。

