# Pangolin · 穿山甲

用浏览器给内网机器上的 Codex / Claude 派任务，查看输出并处理审批、选项。内网 Agent 主动连接公网 Relay，无需给内网开放端口。

```text
手机 / 浏览器 ── HTTPS ──> 公网 Relay <── WSS ── 内网 Agent → Codex / Claude
```

## 一条命令安装

浏览器就是用户界面，无需安装手机 App。需要安装的是 **公网服务端** 和 **内网 Agent**。默认使用原生后台服务，无需 Docker；配置、随机 Token、依赖和后台服务均由脚本处理。

**下面的命令可复制后替换参数执行。** 将 `你的公网IP`、`agent.example.com`、`/你的项目绝对路径` 替换为实际值；路径保留引号，可包含空格。

内网客户端是独立 Node.js 包 `@soyons/pangolin-agent`。脚本自动准备 Node 环境，从 GitHub 下载源码、打包并安装；**无需 Python、git 或先手动安装 npm 包**。目前尚未发布到 npm registry，下面的 curl 安装不依赖 npm 发布。

### 1. 安装公网服务端

在 **公网服务器** 的 Bash / Zsh 终端执行，使用 root 或有 sudo 权限的账号：

```bash
set -o pipefail
curl -fsSL https://raw.githubusercontent.com/soyons/Pangolin/main/install.sh | bash -s -- server --ip "你的公网IP"
```

已有域名时，先将域名解析到服务器，再用下面这条命令代替上面的 IP 安装命令：

```bash
set -o pipefail
curl -fsSL https://raw.githubusercontent.com/soyons/Pangolin/main/install.sh | bash -s -- server --domain "agent.example.com"
```

安装完成后查看连接凭据。**root 安装**执行：

```bash
/opt/pangolin/pangolin credentials
```

**普通账号安装**执行：

```bash
~/.local/share/pangolin/pangolin credentials
```

记下输出中的 **浏览器 USER_TOKEN**、**设备 ID** 和 **设备 DEVICE_TOKEN**；两种 Token 用途不同。

### 2. 安装内网 Agent（Node.js 客户端）

在 **保存项目、已经安装并登录 Codex / Claude 的电脑** 上执行。使用日常开发的普通账号，不要给整条命令加 sudo。最简安装会依次询问 Relay 地址、设备 ID、设备 Token 和项目目录：

```bash
set -o pipefail
curl -fsSL https://raw.githubusercontent.com/soyons/Pangolin/main/packages/agent/install.sh | bash
```

也可以直接提供连接参数：

```bash
set -o pipefail
curl -fsSL https://raw.githubusercontent.com/soyons/Pangolin/main/packages/agent/install.sh | bash -s -- --relay "https://你的公网IP" --device devbox --project "/你的项目绝对路径"
```

如果服务端使用域名，对应命令为：

```bash
set -o pipefail
curl -fsSL https://raw.githubusercontent.com/soyons/Pangolin/main/packages/agent/install.sh | bash -s -- --relay "https://agent.example.com" --device devbox --project "/你的项目绝对路径"
```

按提示粘贴上一步的 **设备 DEVICE_TOKEN**，输入内容不会显示在终端，也不写入命令历史。默认设备 ID 是 `devbox`；如果服务端显示其他 ID，将命令里的 `devbox` 一起替换。

查看 Agent 的运行状态和连接日志：

```bash
~/.local/bin/pangolin-agent status
~/.local/bin/pangolin-agent logs
```

### 3. 打开网页使用

在手机或电脑浏览器打开 `https://你的公网IP`（域名安装则打开 `https://agent.example.com`），输入 **浏览器 USER_TOKEN**，选择在线设备、项目 `example` 和 Codex / Claude，然后创建会话并发送任务。`example` 对应安装 Agent 时指定的项目目录。

### 安装前需要准备

**服务端要求**：Ubuntu 22.04+ / Debian 12+、固定公网 IP，安全组和防火墙放行 TCP 80、443。Python 缺失时通过 apt 安装。脚本自动配置 Caddy、IP HTTPS 证书和续期；首次申请按 Certbot 提示确认条款。IP 证书需要证书机构验证公网可达性，IP 改变后需重新配置。[证书支持说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)

**Agent 要求**：macOS / Linux，用拥有项目和 CLI 登录信息的账号运行，先安装并登录 `codex` 或 `claude`。脚本自动识别已有 CLI，复用 Node.js 22.13+ 和 npm；缺失时从 nodejs.org 下载校验过 SHA-256 的 Node.js 22 到用户目录。需要 tmux 3.2+；缺失时 Linux 使用 sudo apt 安装，macOS 使用已有 Homebrew。支持 x64 / arm64；需能访问 GitHub、npm registry，下载 Node 时还需访问 nodejs.org。客户端无需入站端口，模型登录认证仍由 CLI 自己处理。

## 其他安装方式

### 私有仓库安装（GitHub CLI）

在需要安装的机器上准备 GitHub CLI，并登录有仓库访问权限的账号；如果已登录可跳过：

```bash
gh auth login
```

**公网服务器**执行：

```bash
set -o pipefail
gh api -H 'Accept: application/vnd.github.raw+json' 'repos/soyons/Pangolin/contents/install.sh?ref=main' | bash -s -- server --ip "你的公网IP"
```

**内网电脑**执行：

```bash
set -o pipefail
gh api -H 'Accept: application/vnd.github.raw+json' 'repos/soyons/Pangolin/contents/packages/agent/install.sh?ref=main' | bash -s -- --relay "https://你的公网IP" --device devbox --project "/你的项目绝对路径"
```

凭据查看、设备 Token 输入和网页访问方式与上面的步骤相同。已登录的 GitHub CLI 也用于下载私有仓库源码。

### 本地源码安装

已有源码时，在项目根目录执行即可，不需要下载远程 `install.sh`。

**公网服务器**执行：

```bash
cd "/源码所在目录/Pangolin"
bash install.sh server --ip "你的公网IP"
```

**内网电脑**执行：

```bash
cd "/源码所在目录/Pangolin"
bash install.sh agent --relay "https://你的公网IP" --device devbox --project "/你的项目绝对路径"
```

`bash install.sh agent` 默认安装 Node 客户端。兼容旧 Python 客户端的入口是 `bash install.sh agent --python --relay "https://你的公网IP" --device devbox --project "/你的项目绝对路径"`。

### Docker 服务端安装（可选）

公网服务器已安装 Docker Engine 和 Compose，并准备接入已有 HTTPS 反向代理时，可执行：

```bash
set -o pipefail
curl -fsSL https://raw.githubusercontent.com/soyons/Pangolin/main/install.sh | bash -s -- server --docker
```

远程 Docker 安装默认将源码保存在 `~/Pangolin`，服务映射到 `127.0.0.1:18080`；安装后仍需配置 HTTPS 代理。查看凭据：

```bash
cd ~/Pangolin
python3 scripts/docker-credentials.py
```

### 其他参数

默认从 GitHub `main` 获取源码；可通过 `PANGOLIN_REF` 指定分支或 tag，例如 `curl -fsSL https://raw.githubusercontent.com/soyons/Pangolin/main/packages/agent/install.sh | PANGOLIN_REF=v0.1.0 bash`（需该 tag 已存在）。Node 客户端使用源码压缩包，服务端使用 git clone。下载失败会退出。

- **已有 HTTPS 反向代理**：`bash install.sh server`，代理至 `127.0.0.1:8000`。不传 `--ip/--domain` 时仅本机访问。
- **Docker 网络**：默认 `pangolin`，可用 `server --docker --network 网络名` 修改。
- **只准备文件**：加 `--no-start`，不注册服务或配置 HTTPS。
- **指定配置位置**：加 `--prefix /绝对路径`，后续管理命令也带该参数。Node 程序默认安装在 `~/.local/share/pangolin-node`；用 `PANGOLIN_INSTALL_HOME` 修改程序位置。
- **已有 Node 包**：`PANGOLIN_PACKAGE=/绝对路径/soyons-pangolin-agent-0.1.0.tgz bash packages/agent/install.sh`。发布 npm 后也可改为 `@soyons/pangolin-agent@版本`。
- **无人值守输入设备 Token**：加 `--device devbox --token-file /私有文件`。IP 证书首次签发仍需确认条款。
- **本机测试**：服务端不传 `--ip/--domain`；Agent 使用 `--relay http://127.0.0.1:8000`。

API key 只在内网安装 Agent 的终端中配置。安装器将已有的 `OPENAI_API_KEY/OPENAI_BASE_URL/ANTHROPIC_API_KEY/ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN` 写入本机权限为 600 的配置，用于后台运行，不发往 Relay。

## 消息与交互

- **我的消息**：单独显示发送的任务、文字答案和按键/选项操作，明确标记用户来源。历史保存在 Agent 本机 SQLite 中；重新连接或重启 Agent 后可恢复，停止会话时清除该会话的记录。
- **CLI 输出**：独立显示终端快照，每 2 秒自动刷新，可暂停或手动刷新。原始输出可能包含 CLI 的输入回显，不将这些回显当作新的用户消息或结构化模型回复。
- **审批和选项**：常见 Codex / Claude 编号菜单、`y/n`、继续提示可显示按钮，保留原始选项文字，包括“本次允许”“持续允许”“拒绝”等含义，由用户决定。
- **其他交互**：可展开终端按键，使用方向键、Tab、空格多选、Enter、Esc、退格或 Ctrl+C；自由文本答案在消息框发送。
- **过期与重试**：审批和按键先校验当前终端画面；画面变化时拒绝旧操作并要求重新核对。同一请求 ID 不重复发送；失败后会显示执行状态待核对。

交互识别基于当前可见的终端文字，不是 CLI 的原生结构化审批协议。CLI 升级、复杂布局或多选问题可能需要使用按键面板，首次登录也可能需要在内网机器完成。不会启用跳过审批或放宽 CLI 沙箱的参数。建议服务端与 Agent 一起更新。

## 日常管理

Agent 配置及历史默认保存在 `~/.local/share/pangolin`，Node 程序在 `~/.local/share/pangolin-node`，管理入口在 `~/.local/bin/pangolin-agent`。将 `~/.local/bin` 加入 PATH 后可直接输入 `pangolin-agent`。root 服务端安装在 `/opt/pangolin`，Relay 以专用低权限 `pangolin` 用户运行。

```bash
~/.local/bin/pangolin-agent status
~/.local/bin/pangolin-agent logs
~/.local/bin/pangolin-agent stop
~/.local/bin/pangolin-agent start
```

服务端使用 `/opt/pangolin/pangolin status server`、`logs server`、`stop server`、`start server`；普通账号安装则使用 `~/.local/share/pangolin/pangolin`。macOS 使用 launchd，Linux 使用 systemd。普通账号的 Linux 服务若需退出 SSH 后持续运行，按安装结束提示执行 `sudo loginctl enable-linger 用户名`。

升级时重复相同安装命令，保留原 Token 并重启服务。Node Agent 沿用 Python 安装器的配置、SQLite 历史和服务名称，安装后替换原后台服务并保留现有 tmux 会话。手动启动的旧 Agent 需先停止，避免相同设备 ID 同时连接；自定义配置目录需继续传入相同 `--prefix`。Docker 安装在原目录更新源码后重跑 `bash install.sh server --docker`。Agent 是否连上 Relay 以日志和网页在线状态为准。

Relay 是单用户、单进程应用，持有浏览器 Token 可操作全部配置设备。项目白名单限制启动目录，不能替代 CLI 或操作系统沙箱；没有任意 `shell.exec` 接口。Relay 会接触指令和终端输出，模型凭据保留在内网。超时不代表未执行，核对终端和消息记录后再重试。

## 开发与验证

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt
.venv/bin/python -m pytest -q
node --check web/app.js
bash -n install.sh scripts/docker-server.sh packages/agent/install.sh
npm ci --prefix packages/agent --ignore-scripts
npm test --prefix packages/agent
```

可选的浏览器完整流程测试使用本地假 CLI，不调用模型：安装 `playwright` 以及 Chrome（或运行 `python -m playwright install chromium`）后，执行 `.venv/bin/python tests/browser_smoke.py --agent node`。`--agent python` 验证兼容客户端，`--agent migration` 验证 Python 切换到 Node 后会话和历史恢复。覆盖消息发送与重试、选项、历史恢复、中断和手机布局。

`install.sh` 为统一入口，`scripts/` 负责安装、HTTPS 和服务管理，`packages/agent/` 是可打包发布的 Node 客户端（[包说明](packages/agent/README.md)），`agent/` 保留 Python 兼容实现，`server/` 为鉴权中继，`web/` 为浏览器界面。

[手动部署与 API 说明](docs/manual.md)。项目按原 remote-agent-controller 会话设计重建；未取回原源码附件。仅提交模板，本地凭据、日志和私钥不入库。
