# Claude Singularity

进入已有的 Linux 项目目录，运行 `claude-singularity`，Claude Code 就能读写项目文件、调用现有 Conda 工具，并通过指定代理联网。

**共同作者**　[PingWang98](https://github.com/PingWang98) 和 [Codex（OpenAI）](https://github.com/codex)

## 为什么要做这个

我们最初想用 Claude Cage，在服务器上给 Claude 一个独立的工作环境。这台机器使用 cgroup v1/hybrid，也缺少 Cage 容器部署所需的 UID 映射工具。项目、数据和已配好的 Conda 环境都在这里，于是我们用主机已有的 Singularity 做了这个替代入口。整个部署用普通用户权限完成，镜像、缓存和会话数据都放在 `/mnt`。[与 Cage 的关系和防护对照](docs/DESIGN.md#与-cage-的关系)

## 日常使用

在已部署的服务器上运行

```bash
cd /mnt/data/my-project
claude-singularity
```

当前目录直接挂载为 `/workspace`，Claude 的编辑和删除立即作用于原文件。一次会话可以共享多个项目。已有 Conda 环境只读，默认进入 base。临时安装的包随会话清理，长期依赖可以放在主项目的专用环境里。[日常使用说明](docs/USAGE.md)

Claude 和子进程通过会话专用代理联网。启动器按代理出口设置时区和语言。运行中检测到出口 IP 变化、代理退出或监测失效，就会切断连接并结束会话。可以单独检查网络，也可以从宿主启动无界面 Chromium 并保存截图和报告。

```bash
claude-singularity --check
claude-browser https://ip.net.coffee/claude/
```

## 验收摘要

- 108 项回归测试通过，覆盖实际容器、故障清理、项目挂载、Conda 和 Chromium。
- Claude 在第一轮会话通过代理安装并运行了 `cowsay 6.1`，第二轮确认临时包、命令和标记已清理。在项目持久环境里安装的测试命令则可以跨会话直接运行。
- 公共 DNS 的六次 IPv4/IPv6 UDP/TCP 直连尝试均发送 0 字节。经代理访问 DoH 和 HTTPS 均返回 HTTP 200。
- 浏览器在日本出口显示 `Asia/Tokyo` 和 `ja-JP`。评测页报告 DNS 未检测到泄露。WebRTC 成功创建 offer，采样期间收到 0 个 ICE 候选。

各项实测的日期、版本和测量值见[验收记录](docs/VALIDATION.md)。

## 源码与维护

本仓库保存从已验收部署版整理的[脱敏源码](network-guard/)、[Claude 协作技能](skills/claude-relay/)和[公开源码校验清单](records/runtime-manifest.json)。在仓库根目录运行以下命令可以核对归档。

```bash
python3 scripts/check_snapshot.py
```

主机路径、软件版本和发布步骤见[部署说明](docs/DEPLOYMENT.md)。依赖项目见[第三方项目说明](NOTICE.md)。
