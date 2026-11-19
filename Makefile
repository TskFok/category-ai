.DEFAULT_GOAL := help

# 镜像命名与构建参数（可通过命令行覆盖，例如：make docker-build-amd64 REGISTRY=ghcr.io/org TAG=v1.0.0）
REGISTRY ?=
IMAGE_PREFIX ?= ncke
TAG ?= latest
PLATFORM ?= linux/amd64
EXPORT_DIR ?= dist

ifeq ($(REGISTRY),)
BACKEND_IMAGE ?= $(IMAGE_PREFIX)-backend:$(TAG)
FRONTEND_IMAGE ?= $(IMAGE_PREFIX)-frontend:$(TAG)
else
BACKEND_IMAGE ?= $(REGISTRY)/$(IMAGE_PREFIX)-backend:$(TAG)
FRONTEND_IMAGE ?= $(REGISTRY)/$(IMAGE_PREFIX)-frontend:$(TAG)
endif

.PHONY: help \
	docker-build docker-build-backend docker-build-frontend \
	docker-build-amd64 docker-build-amd64-backend docker-build-amd64-frontend \
	docker-push docker-push-amd64 docker-save-amd64 \
	up down dev logs migrate

help: ## 显示可用 Make 命令说明
	@echo "Novel Character Knowledge Engine (NCKE) Makefile"
	@echo ""
	@echo "用法: make <target> [REGISTRY=...] [IMAGE_PREFIX=ncke] [TAG=latest]"
	@echo ""
	@echo "当前镜像配置:"
	@echo "  BACKEND_IMAGE  = $(BACKEND_IMAGE)"
	@echo "  FRONTEND_IMAGE = $(FRONTEND_IMAGE)"
	@echo "  PLATFORM       = $(PLATFORM)"
	@echo ""
	@echo "可用命令:"
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z0-9_-]+:.*##/ {printf "  \033[36m%-28s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ==============================================================================
# Docker 本地架构镜像构建
# ==============================================================================

docker-build: docker-build-backend docker-build-frontend ## 快速构建本机架构的全部 Docker 镜像（backend + frontend）

docker-build-backend: ## 构建本机架构的后端 Docker 镜像
	docker build -t $(BACKEND_IMAGE) -f backend/Dockerfile ./backend

docker-build-frontend: ## 构建本机架构的前端 Docker 镜像
	docker build -t $(FRONTEND_IMAGE) -f frontend/Dockerfile .

# ==============================================================================
# Docker linux/amd64 平台镜像构建（适用于 Apple Silicon 跨平台构建部署包）
# ==============================================================================

docker-build-amd64: docker-build-amd64-backend docker-build-amd64-frontend ## 快速构建 linux/amd64 平台的全部 Docker 镜像并加载到本地

docker-build-amd64-backend: ## 构建 linux/amd64 平台的后端 Docker 镜像
	docker buildx build --platform $(PLATFORM) --load -t $(BACKEND_IMAGE) -f backend/Dockerfile ./backend

docker-build-amd64-frontend: ## 构建 linux/amd64 平台的前端 Docker 镜像
	docker buildx build --platform $(PLATFORM) --load -t $(FRONTEND_IMAGE) -f frontend/Dockerfile .

# ==============================================================================
# Docker 镜像推送与离线导出
# ==============================================================================

docker-push: ## 推送已构建的本地 Docker 镜像到镜像仓库
	docker push $(BACKEND_IMAGE)
	docker push $(FRONTEND_IMAGE)

docker-push-amd64: ## 构建并直接推送 linux/amd64 平台镜像到镜像仓库
	docker buildx build --platform $(PLATFORM) --push -t $(BACKEND_IMAGE) -f backend/Dockerfile ./backend
	docker buildx build --platform $(PLATFORM) --push -t $(FRONTEND_IMAGE) -f frontend/Dockerfile .

docker-save-amd64: docker-build-amd64 ## 构建 linux/amd64 镜像并导出为离线 tar 包（用于无外网服务器部署）
	@mkdir -p $(EXPORT_DIR)
	docker save -o $(EXPORT_DIR)/$(IMAGE_PREFIX)-amd64-$(TAG).tar $(BACKEND_IMAGE) $(FRONTEND_IMAGE)
	@echo "已导出镜像包: $(EXPORT_DIR)/$(IMAGE_PREFIX)-amd64-$(TAG).tar"

# ==============================================================================
# Docker Compose 快捷管理
# ==============================================================================

up: ## 使用 compose.yaml 本地构建并后台启动服务
	docker compose up -d --build

down: ## 停止并移除容器（保留数据卷）
	docker compose down

dev: ## 启动前后端热更新开发模式
	docker compose -f compose.yaml -f compose.dev.yaml up --build

logs: ## 实时查看所有容器日志
	docker compose logs -f

migrate: ## 执行 Alembic 数据库迁移
	docker compose run --rm migrate
