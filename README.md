# Pangolin · 穿山甲

在浏览器使用邮箱密码登录，给内网机器上的 Codex / Claude 派任务、查看输出并处理审批。内网客户端主动连接公网服务端，无需开放内网端口。同一账号可以在手机、电脑查看自己的设备和已同步对话。

```text
手机 / 电脑浏览器 ── HTTPS ──> 服务端：账号、设备、对话历史
                                   ↑ WSS
                              内网 Node Agent → tmux → Codex / Claude
```

## 一条命令安装

需要安装 **公网服务端** 和 **内网客户端**，浏览器无需安装 App。下面的远程命令需要 GitHub `main` 已包含此版本；已有源码可直接使用后面的本地安装命令。

### 1. 公网服务端

使用 Ubuntu 22.04+ / Debian 12+、固定公网 IP，放行 TCP **80、443**。在 root 或有 sudo 权限的账号终端执行，替换实际公网 IP：

```bash
set -o pipefail
curl -fsSL https://raw.githubusercontent.com/soyons/Pangolin/main/install.sh | bash -s -- server --ip "你的公网IP"
```

已有域名时先设置 DNS，再改用：

```bash
set -o pipefail
curl -fsSL https://raw.githubusercontent.com/soyons/Pangolin/main/install.sh | bash -s -- server --domain "agent.example.com"
```

脚本准备 Python、后台服务和 HTTPS。IP 证书首次申请需按 Certbot 提示确认条款，IP 必须公网可达；已有 HTTPS 反向代理时执行 `bash install.sh server`，代理到 `127.0.0.1:8000`。

新安装默认开启邮箱账号注册。打开 `https://你的公网IP` 或域名，填写邮箱和 **12–128 位密码** 注册。**暂不发送验证邮件，也不提供邮件找回密码**；邮箱只作为账号标识，无需配置 SMTP。

私人部署不想开放注册，可在安装时加 `--registration closed`，然后在服务器本机创建账号：

```bash
# root 安装的管理入口
sudo /opt/pangolin/pangolin create-user --email "you@example.com"
```

普通账号安装则使用 `~/.local/share/pangolin/pangolin create-user --email "you@example.com"`。密码隐藏输入。所有网页账号权限相同，首个注册者不会自动获得管理员权限；账号管理通过服务器本机命令完成。

### 2. 内网客户端

在保存项目的 macOS / Linux 电脑上先安装并登录 `codex` 或 `claude`。用日常开发账号执行，**不要 sudo 整条命令**：

```bash
set -o pipefail
curl -fsSL https://raw.githubusercontent.com/soyons/Pangolin/main/packages/agent/install.sh | bash
```

也可预先指定服务端、邮箱和项目路径：

```bash
set -o pipefail
curl -fsSL https://raw.githubusercontent.com/soyons/Pangolin/main/packages/agent/install.sh | bash -s -- \
  --server "https://你的公网IP" --email "you@example.com" --project "/你的项目绝对路径"
```

按提示输入网页注册的账号密码。客户端自动绑定设备，保存仅限本设备使用的凭据，**不保存账号密码**。`--device "我的开发机"` 可设置显示名称；设备 ID 自动生成，无需复制 Token。

客户端是 Node 包 `@soyons/pangolin-agent`，无需 Python 或 git。脚本复用 Node.js 22.13+ 和 npm；缺失时下载官方 Node.js 22 并校验 SHA-256。需要 tmux 3.2+，缺失时 Linux 使用 sudo apt，macOS 使用已有 Homebrew。支持 x64 / arm64，安装需访问 GitHub、npm registry，准备 Node 时还需访问 nodejs.org。

**包尚未发布到 npm registry。** curl 脚本从 GitHub 下载源码，运行 `npm pack` 后安装，因此不依赖 npm 发布。

### 3. 登录并使用

浏览器登录同一账号，选择在线设备、项目 `example` 和 Codex / Claude，创建会话后发送任务。内网 Agent 离线时仍可查看已经同步的历史，发送和审批会暂停，首版不排队执行离线指令。

用户消息、交互记录与终端输出分别显示。常见编号菜单、`y/n` 和继续提示会显示按钮；未识别的提示可使用方向键、Enter、Tab、空格、Esc 或 Ctrl+C。点击前请核对原文，选项可能包含持续授权。

## 日常管理

客户端默认入口在 `~/.local/bin`，加入 PATH 后可直接输入 `pangolin-agent`：

```bash
~/.local/bin/pangolin-agent status
~/.local/bin/pangolin-agent logs
~/.local/bin/pangolin-agent stop
~/.local/bin/pangolin-agent start
~/.local/bin/pangolin-agent login       # 重新登录并重启后台服务
~/.local/bin/pangolin-agent logout      # 撤销设备登录，保留本机 CLI 会话和历史
```

服务端管理命令：

```bash
sudo /opt/pangolin/pangolin status server
sudo /opt/pangolin/pangolin logs server
sudo /opt/pangolin/pangolin reset-password --email "you@example.com"
sudo /opt/pangolin/pangolin backup --output /opt/pangolin/state/backup.sqlite3
```

普通账号服务端将 `/opt/pangolin/pangolin` 换成 `~/.local/share/pangolin/pangolin` 并去掉 sudo。忘记密码由管理员通过本机 `reset-password` 重置。网页改密码和本机重置都会撤销所有浏览器、设备登录，客户端需重新 `login`。

macOS 使用 launchd，Linux 使用 systemd 用户服务。普通账号退出 SSH 后仍需运行时，按安装提示设置 `sudo loginctl enable-linger 用户名`。无 systemd 环境可安装时加 `--no-start`，再执行 `pangolin-agent run` 前台运行。

## 同步与数据保留

- 一个账号可以绑定多台设备；不同账号的设备、对话和操作相互隔离。网页可解除设备绑定，解绑后保留已同步历史。
- 同步 Pangolin 管理的会话、用户消息、审批记录和**最近一次有变化的终端快照**。暂不导入其他终端独立启动的 Codex / Claude 历史，也不解析结构化模型回复。
- Agent 每约 2 秒采集终端变化，即使没有打开浏览器也继续同步；本地持久化队列支持断线补传，重复传输不重复写入。网页接收变更通知并增量读取历史。
- **停止会话**结束 CLI 并保留历史；**删除对话**删除同步记录并结束 CLI，离线时在重连后清理本机记录，旧补传不会恢复已删除对话。
- 已停止会话默认保留 30 天，清理由服务端启动时和每小时执行。可在安装时用 `--history-days 90` 修改；运行中会话不会自动清除。终端最多保留最近 8000 字符，快照不能保证捕获所有快速滚动内容。
- 任务文字和终端内容会持久化到服务端；项目文件、Codex / Claude 登录信息及模型 API key 留在内网。备份可能包含已删除对话，需单独管理备份保留期限。

服务端数据库默认在安装目录的 `state/server.sqlite3`；客户端配置为 `~/.local/share/pangolin/agent.json`，本地历史为 `state/sessions.sqlite3`，私有文件权限为 600。Node 程序在 `~/.local/share/pangolin-node`。升级时重复安装命令，保留配置和数据库；服务端安装器在更新前备份已有数据库。

## 旧 Token 版本升级

**旧服务端原配置会继续使用 Token 模式**，不会自动把旧设备交给新注册账号。账号模式需要服务端和 Node 客户端一起更新；Python 客户端保留为旧模式兼容实现。

1. 更新两端源码/安装，先在服务端创建目标账号并设置旧设备归属：

```bash
sudo /opt/pangolin/pangolin create-user --email "you@example.com"
sudo /opt/pangolin/pangolin import-device --email "you@example.com" --device devbox
```

2. 服务端重复原安装命令并添加 `--auth-mode accounts`，例如：

```bash
bash install.sh server --ip "你的公网IP" --auth-mode accounts
```

保持原安装账号、原 `--prefix`；root 原安装请以 root 运行。旧 Token 不再被账号模式接受。

3. 内网机器使用新 Node 客户端登录，明确同意上传已有 Pangolin 会话历史：

```bash
~/.local/bin/pangolin-agent login --email "you@example.com" --sync-existing
```

保持原配置目录，后台服务会被替换；手动运行的旧 Agent 请先停止。迁移前自动备份已有本地历史库。不需要导入旧历史时，使用新 `--prefix /新的配置目录` 登录即可。已绑定的历史不能切换给其他账号或服务端，需使用独立配置目录。一个系统用户只有一个默认 Pangolin 后台服务，多账号同时运行需使用独立系统用户或分别前台运行。

## 其他安装与开发方式

已有源码时：

```bash
bash install.sh server --ip "你的公网IP"
bash install.sh agent --server "https://你的公网IP" --email "you@example.com" --project "/你的项目绝对路径"
```

私有仓库已登录 GitHub CLI 时：

```bash
set -o pipefail
gh api -H 'Accept: application/vnd.github.raw+json' 'repos/soyons/Pangolin/contents/install.sh?ref=main' | bash -s -- server --ip "你的公网IP"
gh api -H 'Accept: application/vnd.github.raw+json' 'repos/soyons/Pangolin/contents/packages/agent/install.sh?ref=main' | bash
```

Docker 服务端（需自行配置 HTTPS 代理；默认监听 `127.0.0.1:18080`）：

```bash
bash install.sh server --docker
# SQLite 保存在 pangolin-data 命名卷，勿用 down -v 删除数据
# 本机账号管理：
docker compose --env-file .pangolin/compose.env exec relay python -m server.admin create-user --email "you@example.com"
```

自定义 HTTPS 代理需转发真实 Host 和协议；在配置中设置 `public_url`（原生安装）或 `PANGOLIN_PUBLIC_URL`（环境变量）为浏览器使用的准确站点地址。服务端保持单进程/单 worker。安装参数、开发接口和数据恢复见 [手动部署说明](docs/manual.md)，Node 打包见 [客户端说明](packages/agent/README.md)。

验证：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt
.venv/bin/python -m pytest -q
npm ci --prefix packages/agent --ignore-scripts
npm test --prefix packages/agent
node --check web/app.js
bash -n install.sh scripts/docker-server.sh packages/agent/install.sh
```

浏览器完整流程使用本地模拟 CLI，不调用模型。安装 Playwright 和 Chromium 后运行：

```bash
.venv/bin/pip install playwright
.venv/bin/python -m playwright install chromium
.venv/bin/python tests/account_smoke.py
.venv/bin/python tests/browser_smoke.py --agent node   # 旧 Token 模式兼容检查
```

审批识别基于终端文字；首次 CLI 登录仍可能需要本机处理。项目白名单限制启动目录，不能代替 CLI 或操作系统沙箱。Pangolin 不会自动跳过 CLI 审批，也没有任意远程 shell 执行接口。
