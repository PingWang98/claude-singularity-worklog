# Claude Singularity

进入已有的 Linux 项目目录，运行 `claude-singularity`，Claude Code 就能读写项目文件、调用现有 Conda 工具，并通过指定代理联网。

**共同作者**　[PingWang98](https://github.com/PingWang98) 和 [Codex（OpenAI）](https://github.com/codex)

## 为什么要做这个

我们最初想用 Claude Cage，在服务器上给 Claude 一个独立的工作环境。这台机器使用 cgroup v1/hybrid，也缺少 Cage 容器部署所需的 UID 映射工具。项目、数据和已配好的 Conda 环境都在这里，于是我们用主机已有的 Singularity 做了这个替代入口。整个部署用普通用户权限完成，镜像、缓存和会话数据都放在 `/mnt`。
