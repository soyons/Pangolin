# @soyons/pangolin-agent

Pangolin 内网客户端：通过 WSS 主动连接公网 Relay，在本机 tmux 中管理 Codex / Claude 会话。用户消息和终端输出分开显示，支持审批、选项、按键和断线重连。服务端与网页见 [Pangolin](https://github.com/soyons/Pangolin)。

## 一条命令安装

在已安装并登录 Codex / Claude 的 macOS 或 Linux 电脑上，以日常开发账号执行：

```bash
set -o pipefail
curl -fsSL https://raw.githubusercontent.com/soyons/Pangolin/main/packages/agent/install.sh | bash
```

按提示填写 Relay HTTPS 地址、设备 ID、服务端提供的 DEVICE_TOKEN 和项目目录。Token 隐藏输入；脚本通过终端读取输入，支持 curl 管道。也可传入参数：

```bash
set -o pipefail
curl -fsSL https://raw.githubusercontent.com/soyons/Pangolin/main/packages/agent/install.sh | bash -s -- \
  --relay "https://agent.example.com" --device devbox --project "/你的项目绝对路径"
```

需要 macOS / Linux x64 或 arm64、tmux 3.2+，以及至少一个可用的 `codex` / `claude`。脚本复用 Node.js 22.13+ 和 npm；缺失时自动下载官方 Node.js 22 并校验 SHA-256，安装到当前用户目录。tmux 缺失时使用 Linux 的 sudo apt 或 macOS 已有的 Homebrew 安装。不要 sudo 整条安装命令。

**包尚未发布到 npm registry。** curl 默认下载 GitHub 源码，运行 `npm pack`，再安装生成的 npm 包，无需等待 npm 发布。安装需访问 GitHub、npm registry，准备 Node 时还需访问 nodejs.org；无需 Python 或 git。需要固定版本时设置 `PANGOLIN_REF` 为已存在的 tag 或提交 SHA。

## 使用与升级

```bash
~/.local/bin/pangolin-agent status
~/.local/bin/pangolin-agent logs
~/.local/bin/pangolin-agent stop
~/.local/bin/pangolin-agent start
```

将 `~/.local/bin` 加入 PATH 后可直接输入 `pangolin-agent`。macOS 使用 launchd，Linux 使用 systemd 用户服务；Linux 退出 SSH 后仍要运行时，按安装提示设置 `loginctl enable-linger`。

- `setup`：保存配置并启动后台服务。再次执行可修改连接或项目。
- `setup --no-start`：仅保存配置，适用于没有 systemd 的环境；之后用 `run` 前台运行。
- `--token-file /私有文件`：从文件读取设备 Token，适合无人值守安装。
- `--prefix /配置目录`：修改配置目录，后续管理命令也需传入。

默认配置为 `~/.local/share/pangolin/agent.json`，历史为其下的 `state/sessions.sqlite3`，权限均为 600。程序安装在 `~/.local/share/pangolin-node`，可用 `PANGOLIN_INSTALL_HOME` 修改；管理入口目录可用 `PANGOLIN_BIN_DIR` 修改。模型 API key 只保存在本机配置中，登录认证由 Codex / Claude 自行处理。

重复安装命令即可升级，沿用 Token、配置、历史和 tmux 会话。Node 版与 Python 安装器使用相同服务名称，会替换原服务；手动运行的旧 Agent 需先停止。更换模型凭据后，已有 tmux server 的环境不会自动改变，需在适合停止任务时重启专用 tmux server。

## 打包与分发

在仓库根目录执行：

```bash
npm ci --prefix packages/agent --ignore-scripts
npm test --prefix packages/agent
npm pack ./packages/agent
```

用生成的 tarball 安装（仍需能获取 `ws` 依赖）：

```bash
PANGOLIN_PACKAGE="$PWD/soyons-pangolin-agent-0.1.0.tgz" bash packages/agent/install.sh
```

已有兼容 Node.js / npm / tmux 时，也可直接使用 npm：

```bash
npm install -g ./soyons-pangolin-agent-0.1.0.tgz
pangolin-agent setup
```

维护者在 npm 发布包后，用户才可使用 `npm install -g @soyons/pangolin-agent`，或给 curl 脚本设置 `PANGOLIN_PACKAGE=@soyons/pangolin-agent@版本`。包没有安装生命周期脚本，`setup` 是显式配置入口。
