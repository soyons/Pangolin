# Pangolin

精简的远程 Codex / Claude 控制器：浏览器通过 HTTPS 给公网 Relay 派任务，内网 Agent 主动建立 WSS 连接，在本机 tmux 中运行 CLI。

这是根据原 remote-agent-controller 会话的 MVP 设计重新实现的版本；原会话引用的源码附件未能取回，并非原附件的逐字复制。

```text
浏览器 ── HTTPS + USER_TOKEN ──> Relay (FastAPI)
                                    ↑
                              WSS + DEVICE_TOKEN
                                    │
                              内网 Agent → tmux → Codex / Claude
```

## 功能与边界

- 用户与设备分别使用随机 token；设备 token 绑定设备 ID。
- 创建、列出、发送 prompt、读取终端输出、停止会话。
- 用户消息、交互操作与终端快照分开显示；每 2 秒轮询更新。
- 常见审批/编号选项按钮，以及有限的终端按键、自由文本回复。
- 设备 heartbeat、断线重连、离线错误及请求超时处理。
- 本机配置项目及 Agent 白名单；没有任意 shell.exec 接口。
- 使用独立的 tmux socket `pangolin`，只管理 `rp-<uuid>` 会话。
- 浏览器 token 只留在页面内存；日志以文本展示。

这是单用户、单进程 MVP。所有持有 USER_TOKEN 的客户端可控制全部配置设备。Relay 不持久化任务、日志或设备状态；tmux 会话可在 Agent 重连后重新列出。超时不等于命令未执行，请先刷新状态，勿盲目重试创建或发送操作。

交互卡片根据当前可见终端识别，不是原生结构化审批协议；不同 CLI 版本和复杂布局可能无法识别，可使用按键面板或在内网终端处理。多选问题可用方向键、空格和 Enter，自由文本在消息框输入。首次认证可能仍需在本机完成。未实现模型原生流事件和自动任务完成判定。CLI 保留自身的权限与沙箱设置，不自动跳过审批。项目白名单只限制启动目录，不能替代 CLI 或操作系统沙箱；Agent 运行权限等同于其本机用户。

Agent 在配置中的 `state_path` 保存 SQLite 消息记录（文件权限 600）；Node 客户端默认使用配置目录下的 `state/sessions.sqlite3`，手动运行 Python 客户端则默认使用 YAML 配置旁的 `.pangolin/sessions.sqlite3`。记录可跨 Agent 重启恢复，停止会话会清除对应消息；CLI 自行退出的历史不会自动删除。终端快照仍由 tmux 提供，不写入消息库。

## 目录

```text
server/main.py       REST API、鉴权与 WebSocket 转发
packages/agent/      默认 Node.js 客户端、npm 包、独立 curl 安装脚本
agent/main.py        兼容 Python 客户端、白名单、tmux 管理
web/index.html       手机可用的简易控制页
web/app.js           轮询、独立消息区、交互卡片与按键
agent/interactions.py 当前终端提示识别
agent/state.py       本地消息记录与请求去重
config/agent.yaml    无密钥的项目配置模板
deploy/             Linux systemd 示例
.env.example         环境变量模板
Caddyfile.example    HTTPS/WSS 反向代理示例
tests/               鉴权、转发与会话测试
```

## Node 客户端本地运行

常规安装直接使用 [README 的 curl 指令](../README.md)。源码开发需要 Node.js 22.13+、npm 和 tmux 3.2+：

```bash
npm ci --prefix packages/agent --ignore-scripts
node packages/agent/bin/pangolin-agent.js setup --relay http://127.0.0.1:8000 --project "/项目绝对路径" --no-start
node packages/agent/bin/pangolin-agent.js run
```

`setup` 使用普通账号，提示输入服务端设备 Token；本机 Relay 可用 HTTP，远程 Relay 必须使用 HTTPS。服务端仍使用下方 Python 部署步骤。

多个项目可在 `~/.local/share/pangolin/agent.json` 中配置 `projects`（无需向 Relay 公开本地路径）：

```json
{"projects":{"frontend":{"path":"/绝对路径/frontend","agents":["codex","claude"]},"backend":{"path":"/绝对路径/backend","agents":["codex"]}}}
```

将该字段合并到已有配置，保留连接凭据；重启 Agent 后生效。`tmux_socket` 默认 `pangolin`，`state_path` 可指定已有 SQLite 数据库。Node 和 Python 共用消息协议、数据库格式及服务名称。更多选项见 [Node 包文档](../packages/agent/README.md)。

## Python 客户端与服务端手动运行

需要 Python 3.9+、tmux 3.2+。在内网机器安装并登录 `codex` / `claude`，确认对应命令在 PATH 中可用。API key 只在内网机器配置，不传给 Relay。

```bash
cd /path/to/Pangolin
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp config/agent.yaml config/agent.local.yaml
chmod 600 .env
```

编辑 `config/agent.local.yaml`，把 `example.path` 改为已有项目的绝对路径。
运行下列命令两次，分别生成 USER_TOKEN 和 DEVICE_TOKEN：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

将值填入本地 `.env`。`DEVICE_TOKENS=devbox:<设备token>` 与 `DEVICE_TOKEN=<设备token>` 的设备 token 要一致，用户 token 必须不同。多个设备用逗号分隔，每台使用独立 token。模板占位符无法启动服务。

终端一启动 Relay（必须单 worker）：

```bash
source .venv/bin/activate
set -a; source .env; set +a
uvicorn server.main:app --host 127.0.0.1 --port 8000 --workers 1 --ws-max-size 131072
```

终端二启动 Agent：

```bash
source .venv/bin/activate
set -a; source .env; set +a
python -m agent.main
```

打开 http://127.0.0.1:8000，输入 USER_TOKEN，选设备，填写项目 `example`，创建会话。需要本机交互时：

```bash
tmux -L pangolin list-sessions
tmux -L pangolin attach -t 'rp-替换为实际会话ID'
```

上面 attach 目标应直接替换为列表输出的完整 `rp-...` ID；退出查看但保留会话用 `Ctrl-B D`。停止网页上的会话会终止 CLI 及其正在运行的任务。

## 公网部署

1. 在 VPS 安装 Python 依赖，Relay 仅监听 `127.0.0.1:8000`，只设置 USER_TOKEN 与 DEVICE_TOKENS。
2. 将 Caddyfile.example 中域名改为自己的域名，DNS 指向 VPS；允许入站 443，自动证书签发按 Caddy 配置开放 80。
3. 内网 Agent 设置 `RELAY_WS_URL=wss://你的域名/ws/agent`、DEVICE_ID、DEVICE_TOKEN、AGENT_CONFIG。非 loopback 地址强制 WSS。
4. 浏览器访问 `https://你的域名`。不要把 token 放在 URL、截图或版本库里。

Linux 示例在 `deploy/`。服务端需先创建专用 `pangolin` 用户，把项目放到 `/opt/Pangolin` 并安装虚拟环境，在 `/etc/pangolin/server.env` 写入两项服务端变量，限制权限为 600。将 server unit 安装到 `/etc/systemd/system/` 后执行 `sudo systemctl daemon-reload` 和 `sudo systemctl enable --now pangolin-server`。

Agent 示例是 user unit：项目位于 `~/Pangolin`，环境文件位于 `~/.config/pangolin/agent.env`（权限 600），unit 放入 `~/.config/systemd/user/`。按 CLI 实际安装位置修改 unit 的 PATH，然后运行 `systemctl --user daemon-reload` 和 `systemctl --user enable --now pangolin-agent`。需要退出登录后运行时由管理员配置 linger。macOS 本地运行无需 systemd。

模型认证仅在 Agent 用户环境配置；不要复制包含服务端 USER_TOKEN 的环境文件到其他设备。tmux server 会继承首次启动时的环境，变更模型凭据后需在维护时停止会话并重新启动专用 tmux server。

## API 示例

令牌已从私有环境文件加载时：

```bash
curl -H "Authorization: Bearer $USER_TOKEN" http://127.0.0.1:8000/api/machines
curl -H "Authorization: Bearer $USER_TOKEN" -H 'Content-Type: application/json' \
  -d '{"project":"example","agent":"codex"}' \
  http://127.0.0.1:8000/api/machines/devbox/sessions
```

设备项目列表：GET `/api/machines/{device}/projects`，返回项目名和允许的 Agent，不返回本地路径。

其余操作基于 `/api/machines/{device}/sessions`，均需浏览器 Token：

| 请求 | 用途 |
| --- | --- |
| GET / POST | 列出 / 创建会话；创建 body 为 `{"project":"example","agent":"codex"}` |
| POST `/{session}/send` | 发送 `{"message":"任务","request_id":"32位小写十六进制ID"}` |
| GET `/{session}/state?after=0` | 读取用户/交互事件、终端快照、`screen_id` 和可识别选项 |
| POST `/{session}/input` | 发送选项或有限按键，见下方示例 |
| GET `/{session}/logs` | 兼容旧客户端的终端快照接口 |
| DELETE `/{session}` | 停止会话并清除本地消息记录 |

`state.events` 按事件 ID 升序返回，每次最多 100 条并限制报文大小；下次把 `state.cursor` 作为 `after`，直到没有新事件。`source` 为 `user` 或 `interaction`，`status` 为 `sent`、`pending` 或 `uncertain`；终端独立在 `terminal.text`，最多保留最近 8000 字符。`interaction` 为空表示没有识别到菜单，不代表无需用户操作。

```json
{"screen_id":"从最近一次 state 原样取得的64位摘要","choice":"2","request_id":"32位小写十六进制ID"}
```

或者将 `choice` 替换为 `key`，只允许 `Up/Down/Left/Right/Enter/Escape/Tab/Space/BSpace/C-c`。两者只能提供一个，选项 ID 必须来自当前 `interaction.options`。终端已变化时返回 409，刷新并核对后再操作。`send` 和 `input` 都支持请求去重，重试应复用相同 `request_id` 与 body；未提供 ID 时由服务端生成。操作是否送达无法确认时保留待核对记录，不自动重新发送。

## 验证

```bash
pip install -r requirements-lock.txt
python -m pytest -q
```

`.gitignore` 排除 `.env`、本地 YAML、私钥、日志、虚拟环境。仅提交示例配置。Relay 内存中会接触 prompt 与终端输出，部署在可信服务器并保护该服务器的访问权限。

`requirements-lock.txt` 记录本次 Python 3.9 验证的完整依赖（含测试依赖）；GitHub Actions 使用该文件复现测试。
