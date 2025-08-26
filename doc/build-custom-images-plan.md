# 自定义 Docker 镜像构建工作流计划

## 工作流目标

创建一个 GitHub Actions 工作流，用于自动构建 `docker/` 目录下的自定义 Docker 镜像并推送到阿里云 registry。

## 工作流功能

1. 遍历 `docker/` 目录下的所有子目录
2. 找到包含 Dockerfile 的目录
3. 根据目录结构确定镜像名称和标签
4. 构建镜像并推送到阿里云 registry

## 工作流触发条件

- 在 push 到 main 分支时触发
- 支持手动触发

## 实现细节

### 1. 目录结构解析

对于 `docker/caddy-dns/2.10.0/Dockerfile` 这样的目录结构：

- 镜像名称：caddy-dns
- 镜像标签：2.10.0

### 2. 工作流步骤

1. 检出代码
2. 查找所有需要构建的 Docker 镜像
3. 为每个镜像执行构建和推送操作
4. 清理构建产物

## 工作流文件路径

`.github/workflows/build-custom-images.yaml`
