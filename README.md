# Category AI / Castory AI — Multi-Role Audiobook & Character Knowledge Engine

小说人物知识引擎 + 多角色 AI 有声书制作平台（V2.0 / M4 生产加固版）—— 基于 DeepSeek 与阿里云百炼 CosyVoice v3.5 Flash，支持从 TXT/EPUB 小说到人物知识库、智能分集、逐段对话归属、固定角色音色、断点续跑批量配音及 MP3/WAV/ZIP 导出的完整生产闭环。

## 架构概览

- **前端 (`frontend/`)**：React 18 + TypeScript + Vite + Ant Design + TanStack Query + Cytoscape.js + Nginx 反向代理（含 SSE 零缓冲配置）。包含人物知识引擎与有声书工作台（分集规划 / 配音剧本 / 角色音色 / 音频制作 / 音频导出）。
- **后端 (`backend/app/`)**：Python 3.12+ + FastAPI 模块化单体 + SQLAlchemy 2.0 + Alembic（迁移 `0001` → `0003`）。
- **异步管线 (`backend/worker/`)**：Celery + Redis 分布式任务队列，支持人物分析（`S1_DISCOVER` → `S6_PUBLISH`）、单集/整书批量配音渲染、暂停/恢复/取消/仅重试失败单元、预算控制与幂等去重。
- **大模型与语音引擎**：
  - **DeepSeek (`infrastructure/llm/deepseek.py`)**：`deepseek-flash` 批量抽取、`deepseek-v4-pro` 深度分析，费用以 **USD** 统计。
  - **CosyVoice v3.5 Flash (`infrastructure/tts/cosyvoice.py`)**：对齐阿里云百炼官方非实时 HTTP 合成接口（`/services/audio/tts/SpeechSynthesizer`）与声音设计接口（`model="voice-enrollment"`、必填 `prefix`、`preview_audio.data`、`query_voice` 状态查询），费用以 **CNY** 统计（北京地域公开价 0.80 元/万字符）。
- **数据安全与一致性**：
  - 剧本重分析采用稳定 `ScriptSegment` ID 原位增量更新（绝不级联删除已生成音频与人工编辑）。
  - 未确认说话人的对话/心理活动严格拦截（HTTP 409），绝不静默配为旁白。
  - 音色参数变更自动递增版本快照并局部失效受影响音频；导出前强制校验脚本与音色版本一致性。
  - 超长旁白自动切分为 `SynthesisUnit` 逐单元合成并合并；混采样率/多声道 WAV 合并前自动标准化为统一 PCM。
  - 大型整书 ZIP 导出采用磁盘流式写入，内存占用有界。
  - 生产环境（`APP_ENV=production`）严禁缺凭据时静默降级到 Mock，且禁用 URL 通用 `access_token`（改用 HttpOnly Cookie 或短时 HMAC 签名 `media_token`）。

## 端到端操作流程

1. **上传小说**：在「小说项目」上传 TXT/EPUB 并确认版权授权，运行 DeepSeek 人物知识抽取。
2. **分集规划**：生成 100% 覆盖校验的 `EpisodePlan`（超 60 集自动分批生成全量标题），支持手工调整边界。
3. **配音剧本与审核**：运行剧本标注，点击「渲染前剧本校验」确认所有未归属对话（红色阻塞项），支持逐句指定角色/情绪。
4. **角色音色选角**：为旁白与各角色创建模板 → AI 设计候选音色并试听 → 查询供应商部署状态 → 锁定 `voice_id`。
5. **音频制作**：后台提交单集或「整书批量排队配音」，支持暂停/恢复/取消/仅重试失败段，以及单段局部重生成。
6. **音频导出**：查看「导出前就绪度检查」，导出单集 MP3/WAV 或整书 ZIP（含 `manifest.json` 与 SHA-256 校验和）。

## 快速启动（Docker Compose）

### 1. 配置环境变量

```bash
cp .env.example .env
# 生产环境请务必设置：
# APP_ENV=production
# ADMIN_API_TOKEN=<至少16位强随机令牌>
# DEEPSEEK_API_KEY=<你的 DeepSeek Key> & DEEPSEEK_MOCK_MODE=false
# COSYVOICE_API_KEY=<阿里云百炼北京地域 API Key> & TTS_MOCK_MODE=false
```

> **地域提示**：CosyVoice v3.5 Flash 与 `voice-enrollment` 音色资产具有地域隔离性，请确保 `COSYVOICE_BASE_URL`（默认 `https://dashscope.aliyuncs.com/api/v1` 北京地域）与创建音色的 API Key 所属地域一致。

### 2. 启动服务

```bash
docker compose up -d --build
```

启动顺序：`postgres` / `redis` 健康检查通过 → `migrate` 自动执行 `alembic upgrade head` → `backend` / `worker` 启动 → `frontend`（Nginx 监听 `http://localhost:8080`）。

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

启动本地 API（默认使用 SQLite + Mock 模式，无需外部数据库与付费密钥）：

```bash
DEEPSEEK_MOCK_MODE=true TTS_MOCK_MODE=true uvicorn app.main:app --reload --port 8000
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

```bash
# 执行迁移（0001 -> 0002 -> 0003）
docker compose run --rm migrate

# 备份 PostgreSQL（严禁在生产执行 docker compose down -v）
docker compose exec -T postgres pg_dump -U ncke -d ncke > backup_$(date +%Y%m%d_%H%M%S).sql

# 恢复 PostgreSQL
cat backup_YYYYMMDD_HHMMSS.sql | docker compose exec -T postgres psql -U ncke -d ncke
```

## 核心设计文档

- 架构决策记录（ADR-01 ~ ADR-10）：[docs/ADR.md](docs/ADR.md)
- Prompt 模板目录：[backend/prompts/](backend/prompts/)
- OpenAPI 契约快照：[contracts/openapi.json](contracts/openapi.json)
