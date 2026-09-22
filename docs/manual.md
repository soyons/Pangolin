# 手动部署与接口

常规安装和旧版本升级先看 [README](../README.md)。当前默认是邮箱密码账号模式；Python 客户端只用于显式启用的旧 Token 模式。

## 本地开发

需要 Python 3.9+、Node.js 22.13+、tmux 3.2+，以及已安装并登录的 Codex / Claude。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt
npm ci --prefix packages/agent --ignore-scripts
cp .env.example .env
chmod 600 .env
```

终端一启动服务端（单 worker），开发时仅监听 loopback：

```bash
source .venv/bin/activate
set -a; source .env; set +a
uvicorn server.main:app --host 127.0.0.1 --port 8000 --workers 1 --ws-max-size 131072
```

打开 http://127.0.0.1:8000 注册账号。终端二以普通账号配置客户端：

```bash
node packages/agent/bin/pangolin-agent.js setup --server http://127.0.0.1:8000 \
  --email "you@example.com" --project "/项目绝对路径" --no-start
node packages/agent/bin/pangolin-agent.js run
```

## 服务端配置

| 环境变量 | 默认值 / 用途 |
| --- | --- |
| `PANGOLIN_AUTH_MODE` | `accounts`；`legacy` 单独启用旧 Token 模式，两种认证不能混用 |
| `PANGOLIN_DATABASE` | `.pangolin/server.sqlite3`，进程必须可写所在目录 |
| `PANGOLIN_PUBLIC_URL` | 浏览器使用的站点根地址，例如 `https://agent.example.com`，用于来源校验和 Secure Cookie |
| `PANGOLIN_REGISTRATION` | `open` 允许注册；`closed` 仅本机命令创建账号 |
| `PANGOLIN_HISTORY_DAYS` | `30`，已停止且超过期限的对话会删除，启动及每小时清理 |

原生安装器从 `<安装目录>/server.json` 加载配置，其中对应字段是 `auth_mode`、`public_url`、`registration`、`history_days`。有 `ip` / `domain` 时自动生成 HTTPS 站点地址。修改后重启服务。

服务端继续保持单进程，设备连接保存在内存；账号、设备凭据哈希、对话和同步进度在 SQLite WAL 中。数据库目录与程序目录分离。不要使用多个 Uvicorn worker 指向同一个设备连接池。

公网接入 Caddy 或已有 HTTPS 代理，保留 Host 与协议头，并禁用 `/api/updates` 的响应缓存、缓冲。仅信任受控代理的转发头；原生安装监听 loopback。Docker 反向代理场景应明确设置 `PANGOLIN_PUBLIC_URL` 为外部 HTTPS 地址。无需开放内网机器入站端口。

## 本机账号管理与备份

源码运行时：

```bash
.venv/bin/python -m server.admin create-user --email "you@example.com"
.venv/bin/python -m server.admin reset-password --email "you@example.com"
.venv/bin/python -m server.admin import-device --email "you@example.com" --device devbox
.venv/bin/python -m server.admin backup --output /私有备份目录/server.sqlite3
```

使用 `--database` 指定已运行服务端的同一个数据库。原生安装推荐使用 `<安装目录>/pangolin`，它自动选择数据库和对应运行用户。Docker 用 `docker compose --env-file .pangolin/compose.env exec relay python -m server.admin ...`，备份目标应在 `/data`，再通过容器复制到受控备份目录。

创建、重置时密码从终端隐藏读取，也可传 `--password-file /私有文件`，不用明文命令行参数。密码重置撤销全部浏览器和设备登录。没有“首个用户是管理员”的规则，也没有远程管理员读取所有对话的接口。

`backup` 使用 SQLite backup API，包含已提交的 WAL 内容。不要在运行中仅复制主数据库文件，也不要把备份放入网页静态目录。升级前原生安装器自动备份服务端数据库；客户端首次导入旧历史前会备份本机库。Docker 使用持久化 `pangolin-data` 卷，升级前需自行执行备份命令。

恢复时先停止服务端和客户端，保留现有文件副本，恢复到同一路径并保持运行用户和权限（目录 700、文件 600），清理旧 WAL/SHM 后再启动。服务端与客户端的同步进度需要一致；只把一端回滚到旧快照可能触发“同步序号不连续”，此时保持原库副本，恢复匹配的数据库备份，不要手动修改同步序号。删除操作不追溯清除已有备份。

## 客户端配置

配置位于 `~/.local/share/pangolin/agent.json`，`--prefix` 可以修改位置。配置包含服务端地址、账号 ID、邮箱、设备 ID 和设备凭据；不保存账号密码。模型 API key 只保存在内网配置中，不上传服务端。

多个项目可将以下 `projects` 字段合并到已有配置，保留连接凭据，再重启客户端：

```json
{"projects":{"frontend":{"path":"/绝对路径/frontend","agents":["codex","claude"]},"backend":{"path":"/绝对路径/backend","agents":["codex"]}}}
```

只向服务端上传项目名和允许使用的 CLI，不上传项目路径。`state_path` 可指定本地 SQLite 位置，绑定后不要改为另一个空库。新设备的 `tmux_socket` 默认 `pangolin-<设备ID>`；迁移设备保留原 socket。

本机查看 CLI：

```bash
tmux -L '替换为配置中的tmux_socket' list-sessions
tmux -L '替换为配置中的tmux_socket' attach -t 'rp-替换为完整会话ID'
```

用 `Ctrl-B D` 退出查看并保留会话。不要让多个 Agent 进程同时管理相同数据库和 tmux socket。模型认证改变后，已有 tmux server 不会自动更新环境，需在适合停止任务时重新启动专用 server。

## 账号接口

浏览器通过 Cookie 鉴权。登录状态为 30 天；Cookie 使用 HttpOnly、SameSite=Strict，HTTPS 下加 Secure。变更操作需要 `X-CSRF-Token`，来自登录/注册响应或 `/api/auth/me`。密码使用 Argon2id 哈希，登录和注册有频率限制，校验错误不会回显输入密码。

| 请求 | 用途 |
| --- | --- |
| GET `/api/auth/config` | 当前认证方式、注册开关、历史保留期限 |
| POST `/api/auth/register` | `{"email":"you@example.com","password":"12–128位密码"}`，成功后登录 |
| POST `/api/auth/login` | 同上，返回 `user` 和 `csrf`，并设置 Cookie |
| GET `/api/auth/me` | 当前账号、未验证邮箱状态和 CSRF |
| POST `/api/auth/logout` | 撤销当前浏览器登录 |
| POST `/api/auth/password` | `current_password`、`new_password`，撤销所有浏览器与设备登录 |
| POST `/api/auth/device-login` | `email`、`password`、`name`，自动分配设备 ID；重新登录可带原 `device_id` 和 `expected_account` |
| POST `/api/agent/refresh` | 设备凭据续期，需要设备 Authorization 与 X-Device-ID |
| DELETE `/api/agent/binding` | 内网客户端退出设备登录 |
| DELETE `/api/machines/{device}/binding` | 浏览器撤销自己设备的登录 |
| GET `/api/updates` | SSE 账号变更通知，事件 `change` / `logout`，客户端据此重新读取数据 |

设备凭据仅允许连接该设备和续期、退出，不授予网页账号权限。凭据有效期 90 天，在线客户端在连接时及每天续期。长期离线过期后重新登录。所有资源访问按服务端记录的账号归属校验，客户端提交账号 ID 不能改变归属。

## 会话接口

GET `/api/machines` 返回自己的设备，GET `/api/machines/{device}/projects` 返回项目白名单。以下基于 `/api/machines/{device}/sessions`：

| 请求 | 用途 |
| --- | --- |
| GET / POST | 已同步会话列表 / 创建；body 为 `{"project":"example","agent":"codex"}` |
| POST `/{session}/send` | `{"message":"任务","request_id":"32位小写十六进制ID"}` |
| GET `/{session}/state?after=0` | 消息、状态、终端快照、实时审批信息 |
| POST `/{session}/input` | 选择或有限终端按键 |
| POST `/{session}/stop` | 结束 CLI，保留历史 |
| GET `/{session}/logs` | 终端快照兼容接口 |
| DELETE `/{session}` | 删除历史并结束 CLI；设备离线时延迟清除本机数据 |

账号模式的 `cursor` 是服务端变更游标，不能当作事件 ID。消息状态从 pending 变成 sent/uncertain 时，相同事件 ID 会再次返回；界面需按 ID 更新，而非追加重复消息。每次最多 100 条并限制报文大小，`has_more` 为真时继续传回 `cursor`。

`source` 为 `user` 或 `interaction`。终端在 `terminal.text`，最多 8000 字符。`live=false` 表示仅能查看历史，此时 `screen_id` 和 `interaction` 为空；在线时现查 Agent 的终端以处理审批。快照不构成完整滚动日志。

交互 body：

```json
{"screen_id":"最近state返回的64位摘要","choice":"2","request_id":"32位小写十六进制ID"}
```

或将 `choice` 替换为 `key`，仅允许 `Up/Down/Left/Right/Enter/Escape/Tab/Space/BSpace/C-c`。两者只能提供一个。过期画面返回 409；同一审批被其他页面处理后也会拒绝重复操作。发送和交互重试必须复用相同请求 ID 与内容；不确定是否执行时不能自动重发。

## 同步协议

Agent 的 WebSocket 使用 `Authorization: Bearer <设备凭据>` 与 `X-Device-ID`。RPC 继续使用 `id/action` 请求及 `id/ok/result` 响应。同步使用独立 `type=sync` 帧，携带持久化 `stream`、递增 `seq` 的事件；服务端事务提交后返回 `sync.ack` 的已接收游标。

本地 SQLite 触发器将新消息和状态变化与原记录原子写入 outbox。服务端按设备/stream/序号去重，消息按设备/会话/事件 ID 更新。ACK 丢失时重传同一批次；未 ACK 的事件不删除。进程中断留下的 pending 操作转为 uncertain，不自动再次执行终端输入。

删除生成服务端 tombstone，后续旧事件被忽略；ACK 同时下发待清除会话。Agent 清理本机数据并回传 deleted，保持序号连续。停止保留本地消息和最终快照。同步数据只覆盖本工具管理的会话。

## 旧 Token 模式

显式设置 `PANGOLIN_AUTH_MODE=legacy`、`USER_TOKEN` 和 `DEVICE_TOKENS` 后，浏览器显示旧 Token 入口。两类 Token 必须不同且至少 32 位。Node 客户端可用 `setup --token-file`；Python 客户端仍可按 `config/agent.yaml` 和 `deploy/pangolin-agent.service` 运行。

旧模式不使用账号数据库或离线历史接口，停止会话仍清除本地记录。旧模式 API 使用 Authorization Bearer；账号模式不接受旧共享 Token。迁移步骤见 README，旧设备的归属只能通过服务端本机 `import-device` 设置，不能被注册用户自行领取。
