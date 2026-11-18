---
name: claude-relay
description: 通过已部署的 claude-singularity 在当前项目运行 Claude Code，支持多项目共享、批量任务及容器网络检查。
---

# 在项目中使用 Claude

在项目目录运行 `claude-singularity`。当前目录直接挂载到容器的 `/workspace`，修改和删除立即作用于原文件。保护入口检查失败时报告错误并结束。

```bash
claude-singularity
claude-singularity --project "$PWD" --project ../shared-project
```

首个项目决定持久 HOME，额外项目显示在 `/projects/`。用 `--copy` 可以为单个项目创建工作副本。已有 Conda 默认以只读 base 挂载。临时包随会话清理，长期依赖可放在项目专用 HOME。安装示例见[使用细节](references/singularity.md)。

## 批量任务

用户授权 Claude 协作时，通过本技能的 [scripts/run_claude.py](scripts/run_claude.py) 调用同一入口。从技能目录运行下面的命令，将路径替换为当前任务的项目和提示词文件。

```bash
python3 scripts/run_claude.py --cwd /path/to/project --prompt-file /path/to/task.txt
```

脚本默认开放 `Read,Glob,Grep`。通过 `--share` 添加项目，通过 `--tools` 指定任务所需的工具。提示词由宿主读取并经标准输入传入。调用结果结合退出码、工具输出和项目文件核对。

## 检查与浏览器

- `claude-singularity --check` 检查网络和区域设置。技能脚本的 `--check` 则会发送一次最小模型请求。
- 从宿主运行 `claude-browser https://example.com/`，截图和报告写入项目的 `browser-results/`。
- 时区和语言随会话代理出口设置。代理退出、出口 IP 变化或监测失效时关闭连接并结束会话。
