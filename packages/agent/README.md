# @soyons/pangolin-agent

Pangolin 内网 Node.js 客户端：邮箱密码登录并绑定设备，通过出站 WSS 管理本机 Codex / Claude，将用户消息、交互操作和最近终端快照同步到自己的服务端账号。

## 安装

先在 [Pangolin 服务端](https://github.com/soyons/Pangolin) 网页注册邮箱账号，再在已安装并登录 Codex / Claude 的电脑上以普通账号执行：

```bash
set -o pipefail
curl -fsSL https://raw.githubusercontent.com/soyons/Pangolin/main/packages/agent/install.sh | bash
```

或者指定参数，密码仍隐藏输入：

```bash
set -o pipefail
curl -fsSL https://raw.githubusercontent.com/soyons/Pangolin/main/packages/agent/install.sh | bash -s -- \
  --server "https://agent.example.com" --email "you@example.com" --project "/项目绝对路径"
```

支持 macOS / Linux x64、arm64，要求 tmux 3.2+。脚本复用 Node.js 22.13+ / npm，缺失时下载官方 Node 22 并校验 SHA-256，放入用户目录；tmux 缺失时使用 sudo apt 或已有 Homebrew 安装。不要 sudo 整条安装命令。

**尚未发布到 npm registry。** 默认通过 GitHub 源码执行 `npm pack` 后安装，无需 Python、git，也不依赖 npm 发布。安装需访问 GitHub、npm registry，准备 Node 时还需访问 nodejs.org。`PANGOLIN_REF` 可指定已存在的 tag 或提交。

## 管理

将 `~/.local/bin` 加入 PATH，或使用完整路径 `~/.local/bin/pangolin-agent`：

```bash
pangolin-agent login --server https://agent.example.com --email you@example.com
pangolin-agent status
pangolin-agent logs
pangolin-agent stop
pangolin-agent start
pangolin-agent logout
```

`setup` 首次登录并配置项目、后台服务；重复安装保留配置。`login` 重新输入密码并更新设备凭据，随后重启后台服务。`logout` 撤销设备授权、停止 Agent 后台服务，已有本机 CLI 会话和历史保留。网页解除绑定或修改密码后需重新登录。

- `--device "我的电脑"`：设备显示名称，ID 自动生成。
- `--prefix /配置目录`：独立配置目录，后续所有命令也需带上。
- `--no-start`：只准备配置，再用 `run` 前台启动，适合无 systemd 的环境。
- `--password-file /私有文件`：无人值守读取密码；不支持把密码作为命令行明文参数。
- `--sync-existing`：确认把旧 Pangolin 历史上传到账号；旧设备需由服务端管理员先设置归属。
- `setup --token-file /设备Token文件`：仅用于旧 Token 服务端。

新账号绑定后，设备凭据以权限 600 保存，不保存密码、不传给 CLI。macOS 使用 launchd，Linux 使用 systemd 用户服务，退出 SSH 后持续运行需设置 `loginctl enable-linger`。一个系统用户共享一个后台服务名称；多账号同时运行请使用独立系统用户，或独立 `--prefix` 前台运行。

## 同步与升级

默认配置 `~/.local/share/pangolin/agent.json`，本地记录 `state/sessions.sqlite3`。程序默认 `~/.local/share/pangolin-node`，可用 `PANGOLIN_INSTALL_HOME` 修改；入口目录默认 `~/.local/bin`，可用 `PANGOLIN_BIN_DIR` 修改。

账号模式自动同步消息、审批记录和最新终端快照。断网时本地队列保留，重连后补传；相同请求 ID 不重复执行。设备离线后网页仍可读取已同步历史，但不能发送操作。停止会话保留历史，删除对话会同步清除；服务端默认 30 天后清理已停止会话，终端快照最多 8000 字符。当前只同步 Pangolin 管理的会话，快照不保证捕获完整 CLI 滚动输出。

重复安装即可升级。旧 Token 配置默认保持旧模式；迁移时先按服务端 README 设置旧设备账号归属，再执行 `pangolin-agent login --sync-existing`。沿用原数据库和 tmux 会话，上传前备份本地历史。已绑定历史不能转给另一账号或服务端，新账号请使用独立 `--prefix`。改换模型凭据后，已有 tmux server 环境不会自动改变，需要在适合停止任务时重启该设备的专用 tmux server。

## 打包

仓库根目录执行：

```bash
npm ci --prefix packages/agent --ignore-scripts
npm test --prefix packages/agent
npm pack ./packages/agent
PANGOLIN_PACKAGE="$PWD/soyons-pangolin-agent-0.2.0.tgz" bash packages/agent/install.sh
```

已有兼容 Node / npm / tmux 时：

```bash
npm install -g ./soyons-pangolin-agent-0.2.0.tgz
pangolin-agent setup
```

tarball 仍需获取 `ws` 依赖。维护者发布 npm 后才可使用 `npm install -g @soyons/pangolin-agent` 或 `PANGOLIN_PACKAGE=@soyons/pangolin-agent@版本`。包没有安装生命周期脚本。
