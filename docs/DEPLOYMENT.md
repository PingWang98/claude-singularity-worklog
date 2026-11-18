# 部署快照与维护

## 已验证基线

| 组成 | 本次部署 |
| --- | --- |
| 平台 | Linux x86_64；普通用户，存储位于 `/mnt` |
| 容器工具 | 主机已有 Singularity 3.5.3；显式 `--userns`，不走 setuid 路线 |
| 目录镜像 | 官方 Python 3.11 / Debian bookworm，经 OCI digest 与层摘要验证 |
| 应用 | Claude Code 2.1.308、Node 22.17.0，使用既有安装 |
| base | 原 Anaconda Python 3.12.2，只读挂载 |
| 区域数据 | Debian 签名仓库的 `locales-all` 私有解包 |
| 浏览器 | Chromium 156.0.8078.79；已有 Playwright 1.57.0 |
| 保护程序 | 只读发布版本 `05edca592a088510` |

公开源码使用示例布局。部署时将 `/mnt/example/runtime-data`、`/mnt/example/conda` 和 `/mnt/example/playwright/package` 换成自己的路径。设置部署默认模型或通过 `ANTHROPIC_MODEL` 指定模型。通过宿主环境提供凭据，或以 `CLAUDE_RELAY_PROFILE` 指定私有配置文件。以 `CLAUDE_PROXY_CONFIG` 指定代理配置。

## 已有部署的发布

在服务器维护源码目录运行：

```bash
python3 -I network-guard/install_runtime.py
```

它根据运行文件内容生成新只读版本，原子切换 `runtime/current` 和命令链接，并更新镜像内的只读助手。发布前先结束使用这套助手的会话。

## 首次部署材料

按以下顺序准备镜像和依赖。

1. `fetch_singularity_base.py` 下载并验证官方 OCI 层，整理为旧 Singularity 可识别的 Docker archive；再以普通用户用 `singularity build --sandbox ... docker-archive://...` 准备用户目录镜像。
2. 镜像必须具有 `/opt/claude`、`/opt/node` 和 `/opt/claude-safe` 布局；Claude/Node 来自主机已有的安装。
3. `prepare_singularity_tools.py` 在独立 APT 状态内下载并解包 Git/curl/iproute2，保留 merged-/usr 布局；不执行维护脚本或修改宿主包数据库。
4. `prepare_region_data.py`、`prepare_browser.py` 准备私有 locale 与浏览器数据。后者复用本机已有 Playwright 路径，其他机器需先核对。
5. 准备既有 Mihomo 配置与 Claude 中转环境；值保留在宿主配置中。随后运行发布脚本和 `claude-singularity --check`。

基础镜像来源/摘要见 [记录](../records/base-image.json)。下载脚本会验证实际摘要，但镜像标签可能变化；严格复现本次镜像需使用记录的固定 digest。正常启动不会运行 APT 或重新安装工具。

## 检查

任意具备 Python 3 的机器可运行静态归档校验，不会调用模型或访问服务器：

```bash
python3 scripts/check_snapshot.py
```

集成测试只应在已配置的部署主机、可写维护目录中运行；它们需要本机 Singularity、Mihomo、私有镜像、Conda 与 locale 等依赖。

```bash
TMPDIR="/mnt/example/runtime-data/tmp" \
  python3 -m unittest discover \
  -s network-guard -p 'test_*.py' -v
```

回归测试使用本机测试代理。真实 Claude 会话通过中转 API 单独验收，按调用计费。
