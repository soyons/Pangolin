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
- 创建、列出、发送 prompt、读取最近 200 行输出、停止会话。
- 设备 heartbeat、断线重连、离线错误及请求超时处理。
- 本机配置项目及 Agent 白名单；没有任意 shell.exec 接口。
- 使用独立的 tmux socket `pangolin`，只管理 `rp-<uuid>` 会话。
- 浏览器 token 只留在页面内存；日志以文本展示。

这是单用户、单进程 MVP。所有持有 USER_TOKEN 的客户端可控制全部配置设备。Relay 不持久化任务、日志或设备状态；tmux 会话可在 Agent 重连后重新列出。超时不等于命令未执行，请先刷新状态，勿盲目重试创建或发送操作。

远程权限审批、终端控制键、实时流输出、任务完成判定暂未实现。首次登录、项目 trust 与交互式审批请在内网机器上处理。CLI 保留自身的权限与沙箱设置，不自动跳过审批。项目白名单只限制启动目录，不能替代 CLI 或操作系统沙箱；Agent 运行权限等同于其本机用户。

## 目录

```text
server/main.py       REST API、鉴权与 WebSocket 转发
agent/main.py        出站连接、白名单、tmux 管理
web/index.html       手机可用的简易控制页
config/agent.yaml    无密钥的项目配置模板
deploy/             Linux systemd 示例
.env.example         环境变量模板
Caddyfile.example    HTTPS/WSS 反向代理示例
tests/               鉴权、转发与会话测试
```

## 本地运行

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

其余操作基于 `/api/machines/{device}/sessions`：GET 列表、POST 创建、POST `/{session}/send`（`{"message":"任务"}`）、GET `/{session}/logs`、DELETE `/{session}`。

## 验证

```bash
pip install -r requirements-lock.txt
python -m pytest -q
```

`.gitignore` 排除 `.env`、本地 YAML、私钥、日志、虚拟环境。仅提交示例配置。Relay 内存中会接触 prompt 与终端输出，部署在可信服务器并保护该服务器的访问权限。

`requirements-lock.txt` 记录本次 Python 3.9 验证的完整依赖（含测试依赖）；GitHub Actions 使用该文件复现测试。
