# Source Connectors 规则 (v3.1.0)

- 默认知识库 Vault：`~/.antigravity-agent-policy/vault`（可通过环境变量 `OBSIDIAN_VAULT_ROOT` 覆盖）。
- 可选镜像 Vault：通过环境变量 `OBSIDIAN_MIRROR_VAULT` 指定。
- 索引数据库：`~/.antigravity-agent-policy/obsidian_vault_index.sqlite3`（可通过 `OBSIDIAN_INDEX_DB` 覆盖）。
- 优先使用 `weknora_rag_query` 执行 SQLite FTS5 BM25 + 384d 局部语义向量 + 二跳 `[[WikiLinks]]` 图扩散混合检索。
- 批量抓取站点文档时使用 `source_crawl_documentation`（支持 `max_pages`、`max_depth` 与断点续爬 `resume=True`）。
