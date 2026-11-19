# Novel Character Knowledge Engine (NCKE)

小说人物知识引擎（V1.0）—— 面向百万字级长篇小说的可追溯人物数据库、人物关系图谱与深度人物分析平台。

## 架构概览

- **前端 (`frontend/`)**：React 18 + TypeScript + Vite + Ant Design + TanStack Query + Cytoscape.js + Nginx 反向代理（含 SSE 零缓冲配置）。
- **后端 (`backend/app/`)**：Python 3.12+ + FastAPI 模块化单体 + SQLAlchemy 2.0 + Alembic。
- **异步分析 (`backend/worker/`)**：Celery + Redis 分布式任务管线（支持 `S1_DISCOVER` → `S6_PUBLISH` 分阶段执行、断点续跑、预算控制与幂等去重）。
- **数据存储**：PostgreSQL 16 + `pgvector` 扩展（唯一权威数据库，21 张核心业务表）+ 本地挂载卷（按 `novel_id` 隔离原文与版本化导出）。
- **大模型路由 (`backend/app/infrastructure/llm/deepseek.py`)**：DeepSeek OpenAI 兼容客户端，默认 `deepseek-flash` 做批量抽取、`deepseek-v4-pro` 做深度综合；内置空 JSON 校验、429/5xx 指数退避重试与离线 Mock 模式。

## 快速启动（Docker Compose）

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入 DEEPSEEK_API_KEY（或设置 DEEPSEEK_MOCK_MODE=true 进行离线体验）
```

### 2. 生产基线启动

```bash
docker compose up -d --build
```

启动顺序：`postgres` / `redis` 健康检查通过 → `migrate` 自动执行 `alembic upgrade head` → `backend` / `worker` 启动 → `frontend`（Nginx 监听 `http://localhost:8080`）对外提供服务。

### 3. 开发模式（前后端热更新）

```bash
docker compose -f compose.yaml -f compose.dev.yaml up --build
```

- 前端开发服务器：`http://localhost:5173`
- 后端 Swagger 文档：`http://localhost:8000/docs`

## 本地开发与测试（不依赖 Docker）

### 后端

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -v
```

启动本地 API（默认使用 SQLite + Mock 模式，无需外部数据库即可调试）：

```bash
DEEPSEEK_MOCK_MODE=true uvicorn app.main:app --reload --port 8000
```

导出 OpenAPI 契约快照：

```bash
python -c "import json; from app.main import app; open('../contracts/openapi.json', 'w').write(json.dumps(app.openapi(), ensure_ascii=False, indent=2))"
```

### 前端

```bash
cd frontend
npm install
npm run typecheck
npm run build
npm run dev
```

## 数据库迁移与备份恢复

### 执行迁移

```bash
docker compose run --rm migrate
```

### 备份 PostgreSQL（严禁在生产执行 `docker compose down -v`）

```bash
docker compose exec -T postgres pg_dump -U ncke -d ncke > backup_$(date +%Y%m%d_%H%M%S).sql
```

### 恢复 PostgreSQL

```bash
cat backup_YYYYMMDD_HHMMSS.sql | docker compose exec -T postgres psql -U ncke -d ncke
```

## 核心设计文档

- 架构决策记录（ADR-01 ~ ADR-10）：[docs/ADR.md](docs/ADR.md)
- Prompt 模板目录：[backend/prompts/](backend/prompts/)
- OpenAPI 契约快照：[contracts/openapi.json](contracts/openapi.json)
