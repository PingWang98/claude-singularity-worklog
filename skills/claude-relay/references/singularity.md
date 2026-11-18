# 项目、软件与会话

## 项目挂载

默认直接挂载当前目录。重复 `--project` 会替换默认项目集合，需要同时共享当前目录时将 `"$PWD"` 列为首项。其余项目按目录名挂载，遇到重名自动添加编号。容器内可以访问本次共享的所有项目。

启动前检查项目内的硬链接、绝对或越界符号链接、嵌套挂载及特殊文件。单项目副本模式使用 `--copy`，退出后用 `--changes 会话编号` 查看变动，再用 `--apply 会话编号` 检查冲突并回写。

## 临时安装与跨会话复用

在容器内新增 Python 包时，普通 pip 默认写到当前会话的临时用户目录。

```bash
python -m pip install cowsay==6.1
cowsay -t hello
```

要跨会话保留依赖，在主项目的持久 HOME 中创建环境。下次从同一主项目启动，直接运行最后一行即可。

```bash
python -m venv --system-site-packages "$HOME/.venvs/project"
"$HOME/.venvs/project/bin/python" -m pip install cowsay==6.1
"$HOME/.venvs/project/bin/cowsay" -t hello
```

现有 Conda 环境可以用 `conda activate 环境名` 切换。新建命名环境默认是临时的，持久 Conda 环境可用 `conda create --prefix "$HOME/.conda/envs/project" python` 创建。

## 批量任务与结果

脚本默认输出 JSON。需要逐步查看结果时使用 `--output-format stream-json`。启动诊断写入 stderr，模型输出写入 stdout。长任务可用 `--timeout` 调整等待时间。超时后脚本终止本次进程组。

默认使用入口配置的模型，显式选择可用 `--model`。连接失败时查看启动诊断，区分容器预检和模型调用。排查 DNS 时，分别核对直连探测和通过代理的 HTTPS 或 DoH 响应。

## 网页检查

`claude-browser` 从宿主启动独立的无界面 Chromium。运行前核验浏览器沙箱、时区和语言，随后访问指定的 HTTP 或 HTTPS 页面，将 `page.png` 和 `report.json` 写入主项目的 `browser-results/`。浏览器使用独立 HOME，禁用 QUIC 和非代理 WebRTC UDP。
