# Architecture Decision Records (ADR)

| ID | 决策项 | 结论 | 状态 |
|---|---|---|---|
| ADR-01 | 首批支持文件格式 | 首选 TXT（自动识别 UTF-8/GB18030/GBK/UTF-16），次优先 EPUB（带路径穿越与解压大小限制）；PDF 暂阻止正式全书分析。 | Accepted |
| ADR-02 | 章节/字符位置基准 | 以规范化章节文本的 Python Unicode 码位 `[start_offset, end_offset)` 为唯一权威下标；接口额外提供 `utf16_start`/`utf16_end` 供前端 JS 高亮使用，避免代理对偏移错位。 | Accepted |
| ADR-03 | 化身/分身建模 | 独立 `characters` 实体，`entity_type='avatar'`，通过 `relationships(relation_type='avatar_of')` 与本体关联，保留争议与审核状态。 | Accepted |
| ADR-04 | 审核规则与自动发布阈值 | 所有证据必须通过逐字跨度校验；高风险冲突（同名消歧冲突、修为/阵营冲突）自动进入 `review_tasks` 队列。 | Accepted |
| ADR-05 | 证据多态关联实现 | `evidence_links` 采用 `(subject_kind, subject_id)` 并在应用层强校验目标存在性与 `novel_id` 一致性，兼顾灵活性与数据隔离。 | Accepted |
| ADR-06 | 嵌入模型与向量维度 | 预留 PostgreSQL `pgvector` 扩展，向量索引表延后至 M6 RAG 阶段启用。 | Deferred |
| ADR-07 | 鉴权模型 | V1 默认单管理员模式；后端通过依赖注入保留 `Role`（`admin` / `reviewer` / `reader`）与 Bearer Token 校验边界。 | Accepted |
| ADR-08 | 文件存储实现 | 抽象 `StorageBackend` 接口，V1 使用本地挂载卷（按 `novel_id` 隔离），后续可平滑替换为 S3 适配器。 | Accepted |
| ADR-09 | 原文阅读权限 | 章节原文接口受项目权限控制，公开展示优先使用短引用与章节位置。 | Accepted |
| ADR-10 | 模型与价格配置 | 模型名称（`deepseek-flash` / `deepseek-v4-pro`）、超时、重试与单价全部配置化，支持 Mock 模式用于离线开发与测试。 | Accepted |
