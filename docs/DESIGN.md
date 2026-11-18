# 实现边界

## 唯一网络路径

```text
Claude / 子进程 / 独立 Chromium 会话
  → 私有网络命名空间（仅 lo，无对外路由）
  → 容器内回环 HTTP 代理
  → 会话专用 Unix socket 桥
  → 宿主专用 Mihomo 实例
  → 启动时固定的 SOCKS5 节点
```

Mihomo 只使用固定节点，没有 DIRECT 回退或备用直连，不更改用户共享代理。HTTP 代理变量负责兼容应用；强制边界来自网络命名空间，清除变量不会恢复直连。

启动前验证代理出口、网络、实际挂载来源/模式、私有 PID 与 procfs、空 sysfs、零 capabilities 和 no_new_privs，再核验区域配置。运行中检查出口 IP、代理进程和监测租约；退出、漂移、探测失败或超时会撤销桥接并结束会话树。

节点凭据经匿名内存文件传入专用代理，不写额外节点配置文件。网络日志只记录验证事件，不记录请求内容。启动器早期被强杀仍可能留下无活跃进程的私有目录及失效桥接口令。

## 项目及工具

项目外的可信运行版本以只读方式发布，不在可写项目中执行保护程序。宿主传入的任意 Singularity 绑定、动态加载和 Python 启动注入设置被过滤，可信 Python 助手使用 `-I`。

默认项目直接读写，启动预检拒绝已有硬链接、绝对或越界符号链接、嵌套挂载和特殊文件；普通项目内相对符号链接允许。预检发生在挂载前。会话内的写入和删除直接作用于宿主项目。

Anaconda 只读原地共享；持久 HOME 按主项目路径分开。默认新增软件进入会话临时目录，显式创建的持久环境保留。真实 HOME、SSH agent、Docker socket 和其他宿主服务不自动共享。

## Claude 与浏览器配置差异

普通 Claude 的 seccomp 只允许新建 IPv4、IPv6 和 Netlink socket，并限制命名空间变更、挂载、ptrace 和跨进程内存访问。零 capabilities 也限制了需要特权的原始套接字。socketpair、普通线程和子进程仍可用。

Chromium 需要本地 Unix IPC 与嵌套命名空间，因此只有单独的浏览器配置开放这些操作。外层仍无外网路由、capabilities 为零、no_new_privs 开启，并接受同一个代理/出口监测。浏览器驱动必须先确认 Chromium 自身 namespace/PID/network 和 Seccomp-BPF 沙箱开启，再访问外部网站。

Chromium 使用显式代理和原生 UA，启用证书验证和同源保护。QUIC 和非代理 WebRTC UDP 已禁用，网页仍可调用 RTC API。

## 运行条件

本部署使用 Linux x86_64、内核 user namespace 和主机已有的 Singularity。依赖宿主 Unix socket、GPU 或系统服务的工具需要相应设备或服务接口。区域设置采用定位服务返回的时区，并按国家选择默认语言。

## 与 Cage 的关系

我们最初选用 Claude Cage。其云端容器路线需要 cgroup v2、systemd 用户服务和多 UID/GID 映射。本机使用 cgroup v1/hybrid，缺少 UID 映射工具，因此我们改用已有的 Singularity，在普通用户权限下实现项目共享、受控代理和出口区域设置。[Cage 云端部署说明](https://github.com/Lee-xeo/Claude-Cage/blob/main/deploy/cloud/README.md)

### 防护对照

2026-11-18 核对 Cage 公开文档与本机部署版 `4971490d2323c4a4`。Cage 一侧依据公开资料，本机一侧依据运行源码与已有实测。[验收范围和证据](VALIDATION.md#2026-11-18-复核)

| 范围 | Cage | 本部署 |
| --- | --- | --- |
| 强制代理与故障处理 | 由网关管理客机出站，关键检查失败时关闭外网，保留本地任务并可复检。支持按节点保存固定 IP 或地区范围。 | 私有网络空间仅有回环接口，每次会话使用专用代理。检测到出口 IPv4 变化、代理退出或监测失效时，切断连接并结束会话。 |
| DNS、IPv6 与浏览器 | 检查公共 DNS UDP/TCP、DoT、IPv6、宿主管理接口，并分别采样宿主、终端和浏览器出口。 | 实测所选 IPv4/IPv6 直连与公共 DNS UDP/TCP 被阻断，代理 DoH 正常。Chromium 启用自身沙箱，限制非代理 WebRTC UDP。 |
| 时区与语言 | 支持跟随出口或手动设置，并核对客机与浏览器环境。 | 按出口设置并核验系统与 Node 的区域设置，浏览器访问目标前另行核验时区和语言。 |
| 进程与文件 | 云端提供完整的 rootless 客机环境，可按目录选择共享方式。 | 普通 Claude 进程额外限制 Unix socket、命名空间和挂载操作。镜像与原 Conda 只读，按主项目分配 HOME，默认临时安装随会话清理。 |
| 宿主资源 | 云端配置 CPU、内存和进程数硬上限，桌面路线可用独立 VM。 | 复用现有 Singularity 与宿主内核，当前缺少这三类硬限额。直挂项目内的写入与删除立即生效。 |

上游资料见 [Cage 网络与代理](https://github.com/Lee-xeo/Claude-Cage/blob/main/docs/guide/network.md)、[浏览器环境检查](https://github.com/Lee-xeo/Claude-Cage/blob/main/docs/guest-profile.md)、[云端部署](https://github.com/Lee-xeo/Claude-Cage/blob/main/deploy/cloud/README.md)
