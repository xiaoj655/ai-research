---
title: 源码怎么到线上：容器、镜像与两条「CNB」的分工
date: 2026-10-28T01:00:00+08:00
author: AI Research
---

构建日志的最后一行是 `push succeeded`。Pod 起来三秒后退出，`kubectl logs` 只有一句 `exec format error`。

构建成功、镜像存在、容器能跑，是三件事。它们分别由构建工具、镜像仓库和容器运行时负责。中间任何一段交接物不对，前一环再成功也没用。

这篇想把这条链讲完整：从一次 git push 到 Pod running，中间有哪些格子。每个格子由什么机制填，国内常被叫做「CNB」的两个东西又分别站在哪一格。

## 一、一条链：从 git push 到 Pod running

先把全景摆出来。下面这张表是通用架构，不代表某家公司已经这样集成。

| 段 | 交接物 | 谁在做 | 典型故障 |
| --- | --- | --- | --- |
| 1 代码托管 | commit、tag | Git 平台 | 分支保护、仓库过大 |
| 2 触发与编排 | 一次构建任务 | CI 平台 | 排队、并发抢占、缓存未命中 |
| 3 构建 | OCI 镜像 | Dockerfile + BuildKit，或 Buildpacks | 依赖源不通、层缓存失效 |
| 4 制品仓 | `image@sha256:...` | registry | 鉴权、清理策略误删 |
| 5 发布 | 一个新 revision | PaaS、Argo CD | 拉不到镜像、探针失败 |
| 6 运行 | 一个 Pod、一组进程 | kubelet + 容器运行时 | 入口命令错、资源不足、架构不符 |
| 7 观测 | 指标、日志、事件 | 可观测体系 | 采集不到、告警噪音 |

这条链上有个容易忽略的性质：镜像不可变。registry 里的 `latest` 标签换了内容，已经跑着的 Pod 不会跟着变。要更新就得产生新 revision，让 kubelet 重新拉一次。所以「我 push 了新镜像」和「线上是新版本」之间，永远隔着一次发布动作。

另一件事是责任分界。第 3 段决定「镜像里有什么」，第 6 段决定「进程怎么起」，第 5 段决定「哪个版本替换哪个」。排障时先确定问题落在哪一段，比读日志顺序更重要。

拿一个 Python Flask 应用走一遍会更具体。研发把代码推到 Git，CI 收到 push 事件，起一个构建容器，在源码里装依赖、生成入口，产出一个镜像推到 registry。发布系统拿这个镜像新建一个 revision。kubelet 收到调度结果后拉镜像、建命名空间、起进程，探针通过后流量才进来。这七步里，前四步都在造一个东西，后三步都在让这个东西活着。

## 二、概念底座（一）：容器、镜像、仓库

### 2.1 内核里没有「容器」这个对象

容器是一个被限制了视野和资源的普通进程。

限制来自三样东西。namespace 决定进程能看见什么：自己的 PID 树、网络栈、挂载表、主机名。cgroup 决定进程能用多少：CPU、内存、IO、进程数。联合文件系统（常见实现是 OverlayFS）决定进程看到的文件系统长什么样。

Docker 文档描述 overlay2 驱动时讲得直白：镜像的每一层是只读的 `lowerdir`，容器的可写层是 `upperdir`，两者合并成容器看到的 `merged` 目录。容器要改一个来自镜像的文件，内核先把该文件复制到 `upperdir`，再让写落在副本上。这个动作叫 copy-up。

所以 `docker run` 实际做的事是：从仓库拉层、用联合挂载拼出 rootfs、建一组命名空间、在 cgroup 里放一个进程、exec 它。没有一步需要「容器」这个内核对象。

这里有个实操上很常见的坑。容器里的第一个进程是 PID 1。Linux 内核对 PID 1 有特殊待遇：它不响应默认的信号处理，也不会自动回收孤儿进程。你写 `CMD ["python", "app.py"]`，SIGTERM 到了 python 手上，行为取决于它有没有注册处理器。你写 shell 形式的 `CMD python app.py`，信号先到 shell，很可能传不到 python。于是 `docker stop` 要等十秒超时再强杀。这也是镜像里常配 tini 这类极小的 init 程序的原因。

还有一件事要记住：容器共享宿主机内核。隔离强度取决于你怎么配（capabilities、seccomp、只读 rootfs、user namespace），不取决于它叫不叫容器。要更强边界得上沙箱运行时（gVisor、Kata），那是另一个话题。

### 2.2 OCI 把「造镜像」和「跑镜像」拆成两份规范

OCI 有两个核心规范，各管一头（官方仓库）。

image-spec 管镜像长什么样：一个 manifest 指向 config 和若干 layer，全部用内容摘要寻址。runtime-spec 管怎么跑：一个 bundle 目录加 `config.json`，里面写清要执行哪个二进制、挂哪些目录、用哪些 namespace 和 cgroup，交给 runc 这类运行时消费。

这个拆分的价值在于解耦。写构建工具的人不需要懂 runc 的细节，写运行时的人不需要懂 Dockerfile。两份规范之间只有一个交接物：镜像。

### 2.3 层、镜像、registry：挂和跑是两回事

镜像是一叠只读层，加一份元数据。元数据里有环境变量、默认入口（Entrypoint/Cmd）、运行用户。层用内容摘要命名，内容一样摘要就一样，所以不同镜像能共享底下的层。

「挂镜像」和「跑镜像」要分开看。

挂，是往 registry 推 manifest 和 blob，再按摘要拉回来。registry 只认 media type，不关心这些层是 Dockerfile 编出来的、Buildpacks 组出来的，还是别的工具产的。

跑，是运行时把层挂成 rootfs，读 config 里的入口信息，起进程。

分界线就在这里。手写 Dockerfile、Buildpacks、平台自带的构建任务，三种方式能长期并存，因为它们的出口是同一个东西：一个能 push、能按摘要拉取的 OCI 镜像。共同的底座是镜像格式和 registry，Dockerfile 只是其中一种造法。

顺带解释 tag 和 digest 的区别。tag 是可变指针，digest 是内容指纹。要可追溯就写 digest。

## 三、概念底座（二）：编排、三张合约、控制器

### 3.1 编排在解决什么

没有编排时，部署一个服务要人登机器、起进程、配端口、挂盘、盯着别挂。编排把这个过程变成声明：你写「我要三个副本、镜像用这个、要 500m CPU」，系统自己收敛。

收敛靠的是控制器模式。Kubernetes 官方文档用的例子是恒温器：你设 25 度是期望态，室温是现实态，控制器在中间不停开关设备。集群里每个控制器都在跑这种循环，比对「spec 里写的」和「现在实际是」，然后发出请求把差距抹平。

这解释了一件让新手困惑的事：`kubectl apply` 之后没有一个「执行者」在跑你的指令。你只是改了 API server 里的期望态，剩下的由一堆循环完成。

### 3.2 三张插件合约：CRI、CNI、CSI

Kubernetes 核心代码里没有容器运行时、没有网络实现、没有存储驱动。它只定义接口和对象，具体实现挂在接口上。三张合约是主干的三个。

| 合约 | 谁调谁 | 解决什么 | 常见实现 |
| --- | --- | --- | --- |
| CRI | kubelet 通过 gRPC 调运行时 | 拉镜像、起 Pod、管容器生命周期 | containerd、CRI-O |
| CNI | 容器运行时调网络插件（1.24 后不由 kubelet 管） | 给 Pod 分配 IP、配通网络 | Calico、Cilium、云厂商插件 |
| CSI | kubelet 通过 Unix socket 调 node 插件 | 把块设备、文件系统挂进 Pod | 各存储厂商驱动 |

CRI 是协议不是软件。kubelet 是客户端，用 `--container-runtime-endpoint` 指定连哪个 socket；从 1.26 起运行时必须实现 v1 接口，否则节点注册不上（官方文档）。

Pod 这个概念也值得说清。同一个 Pod 里的容器共享网络命名空间，所以它们用同一个 IP、能通过 localhost 互相访问；它们也共享挂进来的卷。这带来一个后果：Pod 里的容器同生共死，重启策略也是 Pod 级别的。想单独重启一个容器，得用 sidecar 那种可重启的形式，或者干脆拆成两个 Pod。

CNI 有个常被记错的地方：从 1.24 起，kubelet 不再管 CNI 的二进制目录和插件加载，这件事归容器运行时。Pod 起了但网络不通，先看 CNI，而不是先看 kubelet。

还要区分 CNI 和 Service。CNI 解决的是「Pod 有没有 IP、能不能通」。Service 是另一套东西：它给一组 Pod 一个稳定虚拟 IP，靠 kube-proxy 写 iptables 或 IPVS 规则做转发。所以「Pod 之间通、Service 不通」通常不是 CNI 的问题，而是 Endpoints 没选到 Pod、端口名对不上，或者 kube-proxy 没在该节点生效。

CSI 的形态更绕。kubelet 直接调 node 插件挂载卷；控制面组件不直连驱动，只跟 API 打交道。所以「创建卷」「挂载到节点」「做快照」这类需要控制器参与的动作，得由驱动自己 watch API 再触发。这就是 external-provisioner、external-attacher 这些 sidecar 存在的原因（Kubernetes CSI 开发者文档写得很明确）。

### 3.3 sidecar 和 init 容器

sidecar 是跟主容器同 Pod、共享网络命名空间和卷的辅助容器，用来放日志采集、代理、证书轮转这类横切能力。上面 CSI 的例子是它最正当的用法：驱动不方便塞进主容器，就把「跟 API 对话」的部分做成独立容器。

Kubernetes 1.33 把 sidecar 定为 stable（官方文档）。实现方式是在 `initContainers` 里写 `restartPolicy: Always`，这个能力从 1.29 起默认开启。它同时拿到了 init 容器的启动顺序保证和「一直运行」的行为。

init 容器是另一件事：按顺序执行，每个都必须成功退出，主容器才开始。适合等依赖就绪、初始化数据目录。

两者差别的实用版本：需要「先于主容器完成」用 init，需要「陪着主容器一直活着」用 sidecar。

### 3.4 Operator

Operator 是把「人怎么运维这个软件」写成代码。做法是定义一个自定义资源（CRD）描述期望，再写一个控制器去实现它。数据库的主从切换、备份、版本升级，都可以这样做（官方文档的 Operator 模式页）。

它跟 Helm 不是一类东西。Helm 渲染模板、把清单 apply 一次，命令结束就退场。Operator 常驻在集群里，持续把实际状态往期望态推。一个负责装，一个负责守。

蓝鲸 PaaS 仓库的 README 里就列了 operator 组件，负责把应用模型翻译成工作负载。这类组件的位置在链条第 5、6 段之间。

## 四、概念底座（三）：构建与制品链

### 4.1 Dockerfile 是描述，BuildKit 是引擎

Dockerfile 是一串指令：从哪个基础镜像起、复制什么、跑什么命令、用什么入口。它本身不能执行任何东西。

真正执行的是构建引擎。BuildKit 把构建定义编译成 LLB，一种内容寻址的依赖图。然后它按图并行执行，跳过跟最终产物无关的阶段，按内容哈希判断缓存是否命中（Docker 官方文档）。层缓存能导出到 registry，下次在别的机器上拉回来用。

顺带纠正一个常见印象：BuildKit 不是 Docker 的附属品。它可以作为 `buildkitd` 单独跑，客户端远程连过去，`docker build` 只是最常见的调用方式之一。云上把构建能力做成独立服务时，用的就是这套形态。

这里有个概念值得单独拎出来：frontend。frontend 负责把人类写的格式翻译成 LLB，Dockerfile 只是其中一种。Buildpacks 走的是另一条路，它的 lifecycle 直接调 buildpack 的 detect/build 接口，不经过 Dockerfile。所以「Buildpacks 内部先生成 Dockerfile 再执行」这个说法不成立；能用 Dockerfile 的是它的 image extensions 机制，属于可选路径。

### 4.2 制品链：SCM → CI → registry → CD

CI 的职责是「把源码变成不可变制品」，CD 的职责是「把这个制品放到环境里」。两者中间通常隔着 registry，否则回滚就只能靠重新构建。

GitOps 是把 CD 的期望态也放进 Git。Argo CD 是一个 Kubernetes 控制器，持续比对 Git 里的清单和集群里的实际状态，不一致就标记 `OutOfSync`，可以自动同步也可以等人点（官方文档）。它不管镜像怎么造，只管「集群应该长成什么样」。

滚动更新是第 5 段里最值得讲清的机制。你改 Deployment 的镜像，控制器建一个新的 ReplicaSet，按 maxSurge 起新 Pod，等新 Pod 的 readiness 探针通过、进了 Endpoints，才按 maxUnavailable 删旧 Pod。所以「发布卡住」的典型形态是新 Pod 一直不 ready，旧的还在跑，两边并存。这跟构建失败是两种故障：构建失败没有新 Pod，发布失败有新 Pod 但流量不进来。

这套分工带来一条实践约束：CI 产出的引用要用 digest，CD 只搬引用。用 tag 会导致「同一个 tag 指向不同内容」，回滚时对不上。

### 4.3 主角们各站在哪一格

把前面几个对象放回链条：

| 对象 | 站的格子 | 产出 |
| --- | --- | --- |
| 腾讯 CNB（cnb.cool） | 1、2、3 段，另加云开发环境与制品管理 | 构建任务、镜像、开发容器 |
| Cloud Native Buildpacks | 第 3 段里的一种构建方式 | OCI 镜像 |
| Dockerfile + BuildKit | 第 3 段里的另一种构建方式 | OCI 镜像 |
| 蓝鲸 PaaS | 第 5、6 段的发布与运行治理 | 运行中的工作负载 |
| Argo CD | 第 5 段 | 集群状态与 Git 同步 |

两个 CNB 在表里占的位置不一样，也不是同一类东西。这个结论后面两节会展开。

## 五、Dockerfile 的实践边界

这一节不背指令，只讲成本落在哪。

多阶段构建解决的是「构建工具不要留在运行镜像里」。编译器和依赖装在 builder 阶段，最终阶段只 COPY 产物。镜像小一截，可扫的漏洞也少一截。

层顺序决定缓存命中率。变化频繁的放后面，稳定的放前面；`COPY . .` 放在依赖安装之后，否则改一行代码就要重装依赖。

一个能同时体现这几点的最小例子：

```dockerfile
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install -r requirements.txt

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /install /usr/local
COPY . .
RUN useradd -u 10001 app && chown -R app /app
USER 10001
EXPOSE 8000
CMD ["python", "-m", "gunicorn", "app:app"]
```

`requirements.txt` 先复制、先安装，依赖没变时这一层直接命中缓存。`--mount=type=cache` 让 pip 的下载物跨构建保留。最终镜像里没有编译工具，用户非 root，入口用 exec 形式保证信号能收到。

`.dockerignore` 决定构建上下文大小。上下文里塞了 `node_modules` 或 `.git`，每次构建都要传一遍。

非 root 运行是标配，`USER` 一行的事。凭据不要写进层：`RUN export TOKEN=...` 会永久留在镜像历史里，要用 `--mount=type=secret`。

缓存要分四类看，它们解决的开销不一样：

- 层缓存：指令加输入内容的哈希命中，省的是重复执行。
- 包管理器缓存：apt、pip、npm 的下载物，省的是网络。
- BuildKit 的 cache mount：把某个目录跨构建保留，省的是重复解包和编译。
- 远端缓存：把上述缓存推到 registry，让另一台机器也能用。

混为一谈会出现「我们加了缓存但还是慢」这种讨论。

基础镜像的选择要按取舍看，不是越瘦越好：

- `slim`：发行版的精简包集合，还留着 shell 和包管理器，排障方便。
- `alpine`：musl libc 加 busybox，体积最小，代价是预编译轮子和 glibc 依赖可能踩坑。
- `distroless`：只有应用运行时和依赖，官方描述是「minus the operating system」，没有 shell、没有包管理器。代价是进不去容器，排障要靠 ephemeral container 或另建 debug 镜像；好处是可扫的包极少，攻击面小。

选哪个取决于两件事：谁负责补丁，以及出事时你能不能接受「进不去容器」。

## 六、Cloud Native Buildpacks：把构建规则交给平台

Buildpacks 的思路是：语言和框架的构建套路是重复的，让平台维护一份，应用只交源码。

机制有五个阶段（官方 lifecycle 文档）。analyze 读上一次构建的元数据；detect 判断哪些 buildpack 适用；restore 恢复缓存层；build 把源码变成可运行产物；export 组装成 OCI 镜像。lifecycle 是编排者，本身不做语言相关工作。

应用方交什么？一份源码，加一个可选的项目描述文件。构建命令通常就是一行：

```
pack build my-app --buildpack paketo-buildpacks/python \
  --builder paketobuildpacks/builder-jammy-base
```

版本钉在 `project.toml` 里（比如 `BP_CPYTHON_VERSION = "3.12.*"`），不需要写 Dockerfile，也不需要知道基础镜像是什么。你省下的配置，平台替你担了。

三个阶段要分清。buildpack 是单个构建单元，有 detect 和 build 两个接口。builder 是打包好的镜像，里面装着 lifecycle、一组 buildpack 和构建期基础镜像。run image 是应用镜像的运行时底座。旧文档里还有个 stack，指 build 和 run 共用的一组基础镜像。Platform API 0.12 已经把它移除，改用 OS、发行版、架构这些容器生态的原生概念描述兼容性。所以在老配置里看到 stack 不必惊讶，但讲机制时不该再拿它当核心。

导出之后的镜像有个约定：入口是 `launcher`，参数是 process type。launcher 负责把 buildpack 写的启动配置翻译成真正的进程。这个约定解释了为什么用 Buildpacks 构建的应用，`docker run image` 直接就能起来，而 `docker run image bash` 往往失败——镜像里可能根本没有 bash。

还有个安全设计值得知道。analyze、restore、export 需要访问镜像仓库；detect 和 build 要执行 buildpack 代码，而代码可能来自第三方。官方建议把这两组放在不同容器里跑，避免 buildpack 拿到仓库凭据。把五个阶段塞进一个容器、用 `creator` 一条命令跑完，只适合可信 builder 的场景。

rebase 是这套体系里最容易被高估的能力。它把应用层原样叠到新版本的 run image 上，不需要重跑构建。适合的场景是基础 OS 出了漏洞，平台统一换底座。它管不到应用自己装的依赖：pip 包、npm 包、JDK 小版本的漏洞，还得靠重新构建。rebase 还有前置条件——架构、发行版要对得上，镜像得是可 rebase 的，加了扩展层的镜像可能不安全。最后一步同样不能省：镜像换了，生产 Pod 不会自己变，仍要触发一次发布。

谁在用：CNCF 2026 年 8 月 11 日的毕业公告提到 Google、GitLab、Spring、VMware、Bloomberg 等采用和贡献方。项目 2018 年 10 月进 CNCF，2020 年 11 月进入孵化，2026 年 7 月 17 日毕业（CNCF 项目页）。这些是生态采用证据，不能推出国内普及率。

代价也要写清楚。构建环境由平台给，语言版本、系统包、私有依赖源的支持范围都得逐个验证。出问题时你在 lifecycle 日志里找原因，而不是在自己写的 Dockerfile 里。

## 七、蓝鲸 PaaS 里的三条路

蓝鲸的公开代码里，构建不是一个入口，是三个。发布侧还有第四个入口：直接用已有镜像，不经过构建。

`building.py` 按 `RuntimeType` 分流：`BUILDPACK` 走 `ApplicationBuilder`，`DOCKERFILE` 走 `DockerBuilder`（commit `f88962f`，L88-100）。

三条路的差别落在环境变量上（`bg_build/utils.py`，L60-131）。走 Dockerfile 时传 `OUTPUT_IMAGE` 和 `CACHE_REPO`；走 CNB 时传 `OUTPUT_IMAGE`、`CACHE_IMAGE`、`CNB_REGISTRY_AUTH`；走传统 slug 时传对象存储的签名 URL，产物是 `slug.tgz`。前两条产出镜像，第三条产出压缩包。产物类型不同，后面启动方式也不同。

缓存放在哪也跟着路线变。slug 路线的缓存是对象存储里的一个路径，靠签名 URL 读写；CNB 路线把缓存推成 registry 里的一个 cache image；Dockerfile 路线用 `CACHE_REPO`。运维含义很实际：前者的故障先查对象存储配额和 URL 有效期，后者的故障先查 registry 能不能推、凭据过没过期。

阶段名也对得上。CNB 路线的步骤是 ANALYZE、DETECT_BUILDPACK、BUILD_APP、UPLOAD_IMAGE（`step_meta_data.py`），和 lifecycle 的阶段一一对应；Dockerfile 路线是 DOWNLOAD_DOCKER_BUILD_CTX、BUILD_IMAGE，用的是 kaniko 系执行器（`settings/workloads.py` 的 `KANIKO_IMAGE`）。

这里的 CNB 指 Cloud Native Buildpacks，不是 cnb.cool。证据在 `build.py`：CNB runner 的默认入口写死为 `launcher`，注释直接链到 buildpacks.io 的 lifecycle launch 文档，还引用了 lifecycle 仓库的源码位置。slug runner 的入口则是 `bash /runner/init`。两套入口对应两种产物，没有歧义。

三条路并存的现实原因是存量（我的推断）。slug 产物是一个压缩包，启动靠平台注入的 runner；换成镜像，启动方式和发布流程都要跟着改。平台同时支持，老应用可以不动，新应用走新路。这个判断从代码结构能看出来，具体的迁移进度未核。

真正跑构建的是 Pod。构建任务起一个 builder Pod，注入源码下载地址、输出镜像名、缓存镜像名和 registry 凭据，容器入口就是 builder 镜像自带的 entrypoint。

这个 entrypoint 是什么，在 `builder-stack` 分支能看到。仓库里那个 `cnb-builder-shim` 自称「衬垫层」：它 FROM 一个标准 CNB builder 镜像，把自己的二进制复制进去，覆盖 ENTRYPOINT，并写死 `CNB_PLATFORM_API=0.11`（`docker-build/heroku-builder/Dockerfile`）。

它做的是平台侧的那部分活。先从 `SOURCE_GET_URL` 拉源码包，解到 `/app`。再把平台传来的 buildpack 列表写成 `/cnb/order.toml`，决定 detect 的顺序，需要远程下载的顺手拉下来。环境变量按 lifecycle 协议落到 `/platform/env`，一个文件一个变量。校验完 registry 的读写权限，把控制权交给 lifecycle 的 detect、build、export，最后把镜像推到 `OUTPUT_IMAGE`（`cmd/build-init/main.go`）。

为什么需要这一层？标准 CNB 工具链里，`pack` 是给本地或 CI 用的命令行。蓝鲸的构建发生在集群的一个 Pod 里，源码在对象存储、buildpack 可能在内网仓库、每个应用能用哪些 buildpack 由平台配置决定。这些是平台职责，pack 不管，所以要有一个自己的入口程序把它们串起来。

builder 的定义在 `heroku-24.toml`：buildpack 混着 Paketo 的 go、java、nodejs、procfile 和蓝鲸自己的 `bk-buildpack-{apt,python,nodejs,go}`，lifecycle 用的是 `lifecycle-v0.17.6+blueking`，底部 `[stack] id = "heroku-24"`，run image 和 build image 都指向内部镜像仓库。cnb-builder-shim 的 Go 依赖里写的是 `buildpacks/lifecycle v0.18.3`，跟 builder 里那份 0.17.6 不是同一个来源——一个是库，一个是平台下载的执行文件。

镜像的注册方式决定默认行为。初始化脚本 `init.sh` 注册了四个镜像：`blueking`（legacy，slug）、`blueking-cloudnative`（cnb，带 `--default`）、`blueking-noble`（cnb，heroku-24，不带 default）、`legacy-blueking`。判断一个应用走不走 CNB，看的是 runner 镜像上的 `isCloudNativeBuilder` 标签（`helpers.py`），不是看仓库里有没有 Dockerfile。

这里要克制。代码能证明的是：平台支持三条路，并且初始化脚本把 CNB 镜像标成默认。代码不能证明的是：你的集群装的是哪个版本、存量应用迁没迁、线上跑的是不是这条路。`init.sh` 里还有一段注释和 FIXME：默认仍维持 heroku-18 时代的镜像，原因是兼容大量低版本 Python 应用，等框架升到 3.11+ 再切 heroku-24。注释说的是兼容存量，默认值跟着存量走（我的推断）。

排障上最有用的一条：先确认构建产物是什么。slug 的应用，入口是 `/runner/init`；CNB 的应用，入口是 `launcher` 加 process type；直接发布的镜像，入口由镜像自己决定。入口对不上，日志只会给你一句「找不到命令」。

## 八、腾讯 CNB：跨了好几个格子的平台

另一个 CNB 是腾讯的研发协作平台，域名 cnb.cool。官方产品动态页写明：2024 年 11 月社区版公测，2025 年 2 月 26 日上线腾讯云官网。当前的产品简介（2026 年 5 月 29 日更新）自称「新一代 AI Native Git 平台」，其中的 NPC 能回复 Issue、PR，也能自己写代码、建分支。产品面还包括代码托管、制品库（npm、maven、docker、helm）、任务集与安全扫描（官方文档索引）。

它的构建模型是一份放在仓库根目录的 `.cnb.yml`。结构是分支 → 事件（push、pull_request）→ 流水线 → 阶段。同一事件下可以有多条流水线，它们并发执行；一条流水线里的阶段在同一个构建环境里顺序执行。`docker.image` 指定执行环境，所以不同阶段可以跑在不同镜像里。`volumes` 声明缓存目录，支持 copy-on-write，并发任务不会互相写坏。`runner.cpus` 最高能申请 64 核（官方构建文档）。

缓存这块要拆开看，官方讲了三层。代码准备用的是基于 OverlayFS 的 `git-clone-yyds`，官方说法是百 GB 级仓库秒级完成。依赖缓存靠 `volumes` 的写时复制。跨节点共享则用 `docker:cache` 内置任务，把缓存打成镜像再由各节点拉取。这三层解决的开销各不相同，跟 Dockerfile 的层缓存也不是一回事。

企业版交付边界比较清楚：部署到指定的腾讯云 VPC，数据留在客户资源里，需要 TKE、CLB、MySQL、COS 和独立的构建机（企业安装指南）。计费按核时算，构建和开发分开计（官方计费说明，具体额度随政策变动，本文不抄价格表）。

跟 GitLab CI、Jenkins 这类自建方案比，差别不在功能清单，在谁维护什么：

| 维度 | 托管型平台（腾讯 CNB 社区版） | 自建 CI（GitLab CI、Jenkins） |
| --- | --- | --- |
| 基础设施 | 平台方维护 | 自己维护 runner、缓存、升级 |
| 网络与数据 | SaaS 出网；企业版进 VPC | 完全在内网，边界自己划 |
| 缓存实现 | 平台内置三层 | 自己配对象存储与 cache 策略 |
| 迁移成本 | 流水线要按 `.cnb.yml` 重写 | 已有 Jenkinsfile、`.gitlab-ci.yml` 可留 |
| 开发环境 | 与流水线同源声明的云开发空间 | 另建，或用容器本地跑 |

这张表只能当比较维度，不能当结论。本轮没有找到可复现的第三方性能评测，也没找到独立的、具名的企业生产案例。官方那个 AOSP 从 1 小时压到 2 分钟的例子，是平台方自己选的样本，冷热缓存条件和构建内容都没公开。同样，腾讯 CNB 与蓝鲸 PaaS 的组织关系，本轮公开材料里没有可用的说明。同属腾讯不构成同团队或共享内核的证据，代码里的 CNB 也不指向 cnb.cool。

## 九、怎么选，以及哪些话不能说

选型可以按「谁维护」来分，不必按功能打分。

想完全控制构建细节、能承担维护责任，就写 Dockerfile，配好引擎和缓存。自由度最高，补丁和依赖也全归你。

语言栈多、希望基础镜像补丁集中推、能接受平台管构建环境，就评估 Buildpacks。前提是先验证你的语言版本、系统包、私有源都在支持范围内，并且接受排障在 lifecycle 日志里做。

想减少「代码托管、CI、开发环境、制品」四处拼装，就评估腾讯 CNB。评估时仍要单独决定两件事：镜像怎么造，发布怎么发。它替不了这两个决定。

已经有 PaaS 在管应用生命周期，需求是发布流程、环境隔离、运行治理，那就用 PaaS。镜像从哪来都行，蓝鲸那条「直接发布已有镜像」的路就是为此存在的。

最后两条不能推出的关系，值得单独写下来：

一是蓝鲸代码里的 `use_cnb`、`CNB_REGISTRY_AUTH` 指的是 Cloud Native Buildpacks，不是腾讯 cnb.cool。同名不同物，证据是入口 `launcher` 和 buildpacks.io 的注释。

二是「代码支持」不等于「生产在用」。本文对蓝鲸的结论止于公开代码：三条路都在，初始化脚本把 CNB 镜像标为默认。某个集群实际怎么装、存量应用什么状态，未核。

本轮没查到的部分也列清楚。腾讯 CNB 的任务队列与调度架构、独立企业案例、可复现压测，都没有可用材料。蓝鲸各版本的构建路线覆盖比例、Buildpacks 在国内的采用情况，同样没有。这些留白不影响前面的判断，但也不该被填上想象。

## 十、讲给同事听，可以这样说

第一句：容器就是一个被 namespace 限住视野、被 cgroup 限住资源、用联合文件系统拼出根目录的普通进程。内核里没有「容器」这个东西。

第二句：镜像是一叠只读层加一份元数据，registry 只存不跑。挂镜像和跑镜像是两件事，中间只认摘要。

第三句：构建工具的任务是产出一个 OCI 镜像。Dockerfile 是配方，BuildKit 是引擎，Buildpacks 是另一条不用写配方的路。三种方式的出口一样，能长期并存。

第四句：镜像到了集群，kubelet 通过 CRI 让运行时起进程，CNI 给 Pod 配网络，CSI 给它挂盘。这三张都是插件合约，K8s 核心不带实现。

第五句：编排就是写期望态，让一堆控制器循环把现实拉回来。`kubectl apply` 只是改了个记录，没有谁在「执行」你的命令。

第六句：两个 CNB 不在同一层。腾讯 CNB 是包住代码托管、构建、开发环境、制品的平台；Cloud Native Buildpacks 是构建那一格里的一种规范和工具链。蓝鲸代码里的 CNB 是后者，证据是镜像入口用的是它的 `launcher`。

再往下就是细节了。每一段都能对上一个机制，出了问题先定位段，再找机制。

## 来源

官方文档与规范：

- OCI image-spec：https://github.com/opencontainers/image-spec
- OCI runtime-spec：https://github.com/opencontainers/runtime-spec
- Docker OverlayFS 驱动：https://docs.docker.com/engine/storage/drivers/overlayfs-driver/
- Docker BuildKit：https://docs.docker.com/build/buildkit/
- Docker 构建最佳实践：https://docs.docker.com/build/building/best-practices/
- Kubernetes CRI：https://kubernetes.io/docs/concepts/architecture/cri/
- Kubernetes 网络插件（CNI）：https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/network-plugins/
- Kubernetes cgroup v2：https://kubernetes.io/docs/concepts/architecture/cgroups/
- Kubernetes 控制器：https://kubernetes.io/docs/concepts/architecture/controller/
- Kubernetes Operator：https://kubernetes.io/docs/concepts/extend-kubernetes/operator/
- Kubernetes sidecar 容器：https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/
- Kubernetes Pods：https://kubernetes.io/docs/concepts/workloads/pods/
- Kubernetes CSI 开发者文档：https://kubernetes-csi.github.io/docs/
- Argo CD：https://argo-cd.readthedocs.io/en/stable/
- distroless：https://github.com/GoogleContainerTools/distroless
- Paketo Python buildpack：https://paketo.io/docs/howto/python/

Buildpacks：

- lifecycle 概念：https://buildpacks.io/docs/for-platform-operators/concepts/lifecycle/
- rebase：https://buildpacks.io/docs/for-platform-operators/concepts/lifecycle/rebase/
- Platform API 0.11→0.12 迁移：https://buildpacks.io/docs/for-platform-operators/how-to/migrate/platform-api-0.11-0.12
- CNCF 项目页（2026-07-17 毕业）：https://www.cncf.io/projects/buildpacks/
- CNCF 毕业公告（2026-08-11）：https://www.cncf.io/announcements/2026/08/11/cncf-announces-graduation-of-cloud-native-buildpacks-advancing-the-standard-for-container-builds/

腾讯 CNB：

- 产品动态（2025-02-26）：https://cloud.tencent.com/document/product/1785/116258
- 产品简介（2026-05-29）：https://cloud.tencent.com/document/product/1785/116261
- 企业安装指南：https://cloud.tencent.com/document/product/1785/116275
- 计费说明：https://cloud.tencent.com/document/product/1785/116265
- 构建介绍：https://docs.cnb.cool/zh/build/intro.html
- 配置文件：https://docs.cnb.cool/zh/build/configuration.html
- 第三方上手记录（非独立评测）：https://www.mintimate.cn/2025/03/15/first2Cnb

蓝鲸 PaaS（公开仓库，commit `f88962f`，2026-09-09；builder-stack 分支 commit `1581537`）：

- `apiserver/paasng/paasng/platform/engine/deploy/building.py`（L88-100）
- `apiserver/paasng/paasng/platform/engine/deploy/bg_build/utils.py`（L60-131）
- `apiserver/paasng/paasng/platform/engine/phases_steps/step_meta_data.py`
- `apiserver/paasng/paas_wl/bk_app/applications/models/build.py`（`launcher` 入口）
- `apiserver/paasng/paasng/platform/modules/helpers.py`（`isCloudNativeBuilder` 标签）
- `apiserver/init.sh`（镜像注册与 `--default`）
- `cnb-builder-shim/README.md`、`cmd/build-init/main.go`、`docker-build/heroku-builder/Dockerfile`
- `cloudnative-buildpacks/builders/heroku-builder/heroku-24.toml`

仓库地址：https://github.com/TencentBlueKing/blueking-paas
