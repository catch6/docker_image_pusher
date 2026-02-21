# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

基于 GitHub Actions 的 Docker 镜像同步工具，将国外 Docker 镜像（DockerHub、gcr.io、ghcr.io 等）自动同步到阿里云容器镜像服务，解决国内拉取困难的问题。同时支持构建自定义 Docker 镜像并推送到阿里云。

## 架构

项目包含两套独立的 CI/CD 流水线：

### 镜像同步流水线
- `schedule.txt` — 定时同步的镜像列表（每周日 UTC 18:00 触发）
- `quick.txt` — 快速同步的镜像列表（push 到 main 时触发，仅监听 quick.txt 变更）
- `.github/workflows/schedule.yaml` — 定时同步入口，读取 schedule.txt
- `.github/workflows/quick.yaml` — 快速同步入口，读取 quick.txt
- `.github/workflows/push-images.yaml` — 可复用 workflow，执行实际的 pull/tag/push 操作，最大并行 10 个

### 自定义镜像构建流水线
- `docker/<镜像名>/<版本号>/Dockerfile` — 自定义镜像定义，严格遵循此目录结构
- `.github/workflows/build-custom-images.yaml` — 自动检测 `docker/` 下变更的 Dockerfile 进行增量构建；手动触发（workflow_dispatch）时构建全部镜像

## 镜像列表格式（schedule.txt / quick.txt）

```bash
# 注释行
nginx                                            # 基础格式
mysql:8.0                                        # 指定版本
--platform=linux/arm64 redis:latest              # 指定架构
k8s.gcr.io/kube-state-metrics/kube-state-metrics # 私有仓库
```

命名转换规则：路径中 `/` 转为 `-`，`@sha256:` 后缀会被移除，指定平台时追加 `-linux-arm64` 等后缀。

## 自定义镜像目录约定

添加新自定义镜像时，必须按 `docker/<name>/<tag>/Dockerfile` 结构组织。构建矩阵通过 `find docker -type f -name "Dockerfile"` 自动发现，目录名直接对应最终镜像名和标签。

### 版本不可变原则

已发布的版本目录**禁止修改**。需要改动时，必须将当前最新版本目录完整复制为新的 patch 版本，在新目录中进行修改。例如当前最新版本为 `docker/ffmpeg-converter/1.2.3/`，则：

1. `cp -r docker/ffmpeg-converter/1.2.3 docker/ffmpeg-converter/1.2.4`
2. 在 `docker/ffmpeg-converter/1.2.4/` 中进行修改
3. 提交新版本目录，不要触碰旧版本

## 所需 GitHub Secrets

- `ALIYUN_REGISTRY` — 阿里云仓库地址（如 registry.cn-beijing.aliyuncs.com）
- `ALIYUN_NAME_SPACE` — 命名空间
- `ALIYUN_REGISTRY_USER` — 登录用户名
- `ALIYUN_REGISTRY_PASSWORD` — 登录密码
