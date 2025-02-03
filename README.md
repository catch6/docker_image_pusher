# Docker 镜像推送工具

这是一个基于 Github Action 的工具，可以自动将国外的 Docker 镜像同步到阿里云私有仓库，方便国内服务器使用。

## 特点

- 支持多种镜像源：DockerHub、gcr.io、k8s.io、ghcr.io 等
- 支持大型镜像：单个最大支持 40GB
- 使用阿里云官方线路：传输速度快
- 支持多架构：可指定 arm64、amd64 等
- 支持定时同步：可配置自动更新

## 使用教程

### 1. 配置阿里云

1. 登录[阿里云容器镜像服务](https://cr.console.aliyun.com/)
2. 开通个人实例服务
3. 创建命名空间（将作为 **ALIYUN_NAME_SPACE**）
   ![](/doc/命名空间.png)

4. 获取访问凭证：
   - 在"访问凭证"页面获取以下信息：
     - 用户名（**ALIYUN_REGISTRY_USER**）
     - 密码（**ALIYUN_REGISTRY_PASSWORD**）
     - 仓库地址（**ALIYUN_REGISTRY**）
       ![](/doc/用户名密码.png)

### 2. 配置 Github

1. Fork 本项目到您的账号下

2. 启用 Actions：

   - 进入您的项目
   - 点击 Actions 标签页
   - 确认启用 Github Actions

3. 配置密钥：
   - 进入 Settings -> Secrets and variables -> Actions
   - 点击 New repository secret
   - 添加以下四个密钥：
     - ALIYUN_NAME_SPACE
     - ALIYUN_REGISTRY_USER
     - ALIYUN_REGISTRY_PASSWORD
     - ALIYUN_REGISTRY
       ![](doc/配置环境变量.png)

### 3. 添加镜像

编辑 schedule.txt 文件，按需添加镜像。支持以下格式：

```bash
# 基础格式
nginx
# 指定版本
mysql:8.0
# 指定架构
--platform=linux/arm64 redis:latest
# 指定私有仓库
k8s.gcr.io/kube-state-metrics/kube-state-metrics
# 使用注释
# 这是一个注释
```

### 4. 使用镜像

1. 在阿里云镜像仓库中查看同步状态
2. 可选择将镜像设为公开（无需登录即可拉取）
   ![](doc/开始使用.png)

3. 在服务器上拉取镜像：

```bash
docker pull registry.cn-hangzhou.aliyuncs.com/your-namespace/image-name
```

### 镜像命名规则

原始镜像名将按以下规则转换：

```bash
# 基础镜像
mysql:8.0 => mysql:8.0

# 带路径的镜像
bitnami/mysql:8.0 => bitnami-mysql:8.0

# 带 SHA 的镜像
bitnami/mysql:8.0@sha256:xxx => bitnami-mysql:8.0

# 指定架构的镜像
--platform=linux/arm64 bitnami/mysql:8.0 => bitnami-mysql:8.0-linux-arm64

# 私有仓库镜像
gcr.io/cadvisor/cadvisor:v0.39.3 => gcr.io-cadvisor-cadvisor:v0.39.3
```

### 定时更新

编辑 `.github/workflows/schedule.yaml` 文件中的 schedule 部分可设置定时执行：

```yaml
schedule:
  # UTC 时间星期日 18 点（北京时间周一早上 2 点）
  - cron: '0 18 * * 0'
```

> 注意：cron 表达式使用 UTC 时区
