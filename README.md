# Docker Images Pusher

使用 Github Action 将国外的 Docker 镜像转存到阿里云私有仓库，供国内服务器使用，免费易用<br>

- 支持 DockerHub, gcr.io, k8s.io, ghcr.io 等任意仓库<br>
- 支持最大 40GB 的大型镜像<br>
- 使用阿里云的官方线路，速度快<br>

## 使用方式

### 配置阿里云

登录阿里云容器镜像服务<br>
https://cr.console.aliyun.com/<br>
启用个人实例，创建一个命名空间（**ALIYUN_NAME_SPACE**）
![](/doc/命名空间.png)

访问凭证–>获取环境变量<br>
用户名（**ALIYUN_REGISTRY_USER**)<br>
密码（**ALIYUN_REGISTRY_PASSWORD**)<br>
仓库地址（**ALIYUN_REGISTRY**）<br>

![](/doc/用户名密码.png)

### Fork 本项目

Fork 本项目<br>

#### 启动 Action

进入您自己的项目，点击 Action，启用 Github Action 功能<br>

#### 配置环境变量

进入 Settings->Secret and variables->Actions->New Repository secret
![](doc/配置环境变量.png)
将上一步的**四个值**<br>
ALIYUN_NAME_SPACE,ALIYUN_REGISTRY_USER，ALIYUN_REGISTRY_PASSWORD，ALIYUN_REGISTRY<br>
配置成环境变量

### 添加镜像

打开 images.txt 文件，添加你想要的镜像
可以加 tag，也可以不用(默认 latest)<br>
可添加 --platform=xxxxx 的参数指定镜像架构<br>
可使用 k8s.gcr.io/kube-state-metrics/kube-state-metrics 格式指定私库<br>
可使用 #开头作为注释<br>
![](doc/images.png)
文件提交后，自动进入 Github Action 构建

### 使用镜像

回到阿里云，镜像仓库，点击任意镜像，可查看镜像状态。(可以改成公开，拉取镜像免登录)
![](doc/开始使用.png)

在国内服务器 pull 镜像, 例如：<br>

```
docker pull registry.cn-hangzhou.aliyuncs.com/shrimp-images/alpine
```

registry.cn-hangzhou.aliyuncs.com 即 ALIYUN_REGISTRY(阿里云仓库地址)<br>
shrimp-images 即 ALIYUN_NAME_SPACE(阿里云命名空间)<br>
alpine 即 阿里云中显示的镜像名<br>

### 多架构

需要在 images.txt 中用 --platform=xxxxx 手动指定镜像架构
指定后的架构会以前缀的形式放在镜像名字前面
![](doc/多架构.png)

### 镜像重命名规则

```
mysql:8.0 => mysql:8.0
bitnami/mysql:8.0 => bitnami-mysql:8.0
bitnami/mysql:8.0@sha256:ec1e8d95b06e7f78c7f4ee0ed91f835dd39afff7c58e36ba1a4878732b60fcf9 => bitnami-mysql:8.0
--platform=linux/arm64 bitnami/mysql:8.0 => bitnami-mysql:8.0-linux-arm64
gcr.io/cadvisor/cadvisor:v0.39.3 => gcr.io-cadvisor-cadvisor:v0.39.3
```

### 定时执行

修改/.github/workflows/docker.yaml 文件
添加 schedule 即可定时执行(此处 cron 使用 UTC 时区)
![](doc/定时执行.png)
