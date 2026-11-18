# 使用完成品

## 默认入口与目录

在宿主的项目目录运行 `claude-singularity`。主项目直接挂载为容器 `/workspace`，不复制；创建、修改、删除实时作用于原文件。退出或断网不会回滚已经完成的写入。

```bash
cd /mnt/data/project-a
claude-singularity

claude-singularity --project "$PWD" --project /mnt/data/project-b
```

重复 `--project` 替换默认目录集合。首个是 `/workspace`，其余为 `/projects/目录名`，重名自动加后缀；启动器为 Claude 添加对应 `--add-dir`。

主项目的绝对路径决定持久 HOME。保持同一主项目路径，下一次仍使用同一份 HOME；额外共享项目共用它。容器 HOME 为 `/home/claude`，宿主实际状态放在 `/mnt` 的私有 `state/`，不挂载真实 HOME。

需要工作副本时使用 `--copy`；该模式仅支持一个项目。退出后按编号用 `--changes` 查看、`--apply` 检查冲突并回写。默认直接挂载不使用这两步。

## Claude 参数与中转

```bash
claude-singularity -- --model your-model
```

默认使用入口配置的模型，显式模型设置优先。启动器在宿主读取现有中转配置，仅传递所需变量；不把 profile 挂进容器。预检失败时启动器报告原因并退出。

`claude-singularity --check` 检查当前网络与区域配置，不调用模型。普通启动自动执行相同预检。

## 默认 base 与临时软件

现有 Anaconda 以原绝对路径只读挂载，默认 `python`、`python3`、`pip` 来自 base；不复制整套环境。非交互 Bash 提供 Conda 初始化，例如 `conda activate example-env` 可选择根前缀 `envs/` 中的已有环境。未整体导入宿主 `.bashrc`、其他软件目录或别名。

以下命令应在 Claude 容器内运行：

```bash
python -m pip install cowsay==6.1
cowsay -t hello

conda create -n scratch python=3.12 -y
conda activate scratch
```

base 和已有环境只读，普通 pip 自动选择可写的临时用户目录。Python 用户包及命令、pip 缓存、新 Conda 环境和包缓存默认位于容器 `/tmp/claude-*`，实际由宿主 `/mnt` 的会话目录承载；退出后清理。不能向原 base 执行 `conda install`，也不提供 sudo 系统安装。

## 跨会话保留软件

优先把长期依赖放在主项目的持久 HOME。首次在容器内运行：

```bash
python -m venv --system-site-packages "$HOME/.venvs/project"
"$HOME/.venvs/project/bin/python" -m pip install cowsay==6.1
```

新环境复用 base 已有包，新增依赖写入自己的目录。下次进入同一主项目，无需再次安装：

```bash
"$HOME/.venvs/project/bin/cowsay" -t hello
```

当前入口仍默认 base，不自动激活该环境。需要短命令时可在当前 shell `source "$HOME/.venvs/project/bin/activate"`；Claude 不同 Bash 调用应优先使用环境内程序的绝对路径。

持久 Conda 环境同样显式指定持久前缀：`conda create --prefix "$HOME/.conda/envs/project" ...`，之后按完整前缀激活。若要把 venv 放在项目 `/workspace/.venv`，创建时使用 `--copies`，避免默认解释器绝对符号链接触发项目预检。

## 浏览器

从宿主启动：

```bash
claude-browser https://ip.net.coffee/claude/
claude-singularity --project /mnt/data/project-a --browser https://example.com/
```

无界面 Chromium 输出到项目 `browser-results/时间戳/page.png` 和 `report.json`。只接受 HTTP/HTTPS URL，不接受任意 Chromium 参数或本地文件地址。每次使用新浏览器配置，持久浏览器 HOME 单独存于 `browser-state/`，不接收中转 API 凭据。

浏览器是宿主单独启动的受保护入口。普通 Claude 子进程仍继承严格 seccomp，不能自行切换成浏览器配置。

## 时区、语言和安装下载

新会话通过本次专用代理查询已测出口，设置并验证时区、locale 和 Node Intl；浏览器另在空白页核验 Intl 与语言。运行期间不静默切换区域；出口 IP 漂移会停止会话。

例如日本出口采用 `Asia/Tokyo` 与 `ja_JP.UTF-8`。未收录国家使用其实际时区、英语默认语言并记录回退。模型仍按用户指定语言回答。包下载也必须经受控代理，不能通过清除代理变量获得直连。
