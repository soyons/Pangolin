#!/usr/bin/env bash
# curl -fsSL https://raw.githubusercontent.com/soyons/Pangolin/main/packages/agent/install.sh | bash
set -euo pipefail
umask 077
if [[ "${1:-}" == agent ]]; then shift; fi
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  echo 'Usage: bash install.sh [--server https://HOST] [--email EMAIL] [--device NAME] [--project PATH] [--password-file FILE] [--prefix PATH] [--no-start]'
  echo 'Installs the Node.js Agent for the current user; no Python or git required.'
  echo 'PANGOLIN_PACKAGE=/path/to/package.tgz or @soyons/pangolin-agent@VERSION selects an npm package.'
  exit 0
fi
if [[ "$(id -u)" == 0 ]]; then
  echo '请用拥有项目和 Codex/Claude 登录信息的普通账号执行，不要 sudo 整条命令。' >&2
  exit 1
fi
case "$(uname -s)" in Linux) PLATFORM=linux ;; Darwin) PLATFORM=darwin ;; *) echo '仅支持 macOS / Linux。' >&2; exit 1 ;; esac
case "$(uname -m)" in x86_64|amd64) ARCH=x64 ;; arm64|aarch64) ARCH=arm64 ;; *) echo '仅支持 x64 / arm64。' >&2; exit 1 ;; esac
INSTALL_HOME="${PANGOLIN_INSTALL_HOME:-$HOME/.local/share/pangolin-node}"
mkdir -p "$INSTALL_HOME"
INSTALL_HOME="$(cd "$INSTALL_HOME" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
download() { curl --proto '=https' --tlsv1.2 -fsSL --retry 2 --connect-timeout 15 --max-time 180 "$1" -o "$2"; }
as_admin() { sudo "$@"; }

# Keep an existing compatible Node; otherwise install the official runtime privately.
if ! command -v node >/dev/null || ! node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>22 || (a===22 && b>=13) ? 0 : 1)' || ! command -v npm >/dev/null; then
  echo '准备 Node.js 22 运行环境（安装到当前账号目录）…'
  download 'https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt' "$TMP/SHASUMS256.txt"
  ARCHIVE="$(awk -v suffix="-$PLATFORM-$ARCH.tar.gz" 'index($2, suffix) == length($2)-length(suffix)+1 { print $2; exit }' "$TMP/SHASUMS256.txt")"
  if [[ ! "$ARCHIVE" =~ ^node-v22\.[0-9]+\.[0-9]+-(linux|darwin)-(x64|arm64)\.tar\.gz$ ]]; then
    echo '未找到适合当前系统的 Node.js 22 安装包。' >&2; exit 1
  fi
  download "https://nodejs.org/dist/latest-v22.x/$ARCHIVE" "$TMP/$ARCHIVE"
  EXPECTED="$(awk -v file="$ARCHIVE" '$2 == file { print $1 }' "$TMP/SHASUMS256.txt")"
  if command -v sha256sum >/dev/null; then
    ACTUAL="$(sha256sum "$TMP/$ARCHIVE" | awk '{print $1}')"
  elif command -v shasum >/dev/null; then
    ACTUAL="$(shasum -a 256 "$TMP/$ARCHIVE" | awk '{print $1}')"
  else
    echo '请安装 sha256sum 或 shasum 后重试。' >&2; exit 1
  fi
  if [[ ! "$EXPECTED" =~ ^[0-9a-f]{64}$ || "$EXPECTED" != "$ACTUAL" ]]; then
    echo 'Node.js 下载校验失败，安装已停止。' >&2; exit 1
  fi
  mkdir -p "$INSTALL_HOME/runtime"
  tar -xzf "$TMP/$ARCHIVE" -C "$INSTALL_HOME/runtime"
  export PATH="$INSTALL_HOME/runtime/${ARCHIVE%.tar.gz}/bin:$PATH"
fi
NODE="$(command -v node)"
NODE="$("$NODE" -p 'process.execPath')"
if ! command -v tmux >/dev/null; then
  if [[ "$PLATFORM" == linux ]] && command -v apt-get >/dev/null; then
    as_admin apt-get update
    as_admin apt-get install -y tmux
  elif [[ "$PLATFORM" == darwin ]] && command -v brew >/dev/null; then
    brew install tmux
  else
    echo '请先安装 tmux（macOS 可用 Homebrew；Linux 可用系统包管理器），然后重试。' >&2; exit 1
  fi
fi

SPEC="${PANGOLIN_PACKAGE:-}"
SOURCE=''
if [[ -z "$SPEC" && -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  [[ -f "$SOURCE/package.json" ]] || SOURCE=''
fi
if [[ -z "$SPEC" ]]; then
  if [[ -z "$SOURCE" ]]; then
    REPO="${PANGOLIN_REPO:-soyons/Pangolin}"
    REF="${PANGOLIN_REF:-main}"
    if [[ ! "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then echo 'Invalid GitHub repository.' >&2; exit 1; fi
    REF_ENCODED="$("$NODE" -p 'encodeURIComponent(process.argv[1])' "$REF")"
    echo "下载 $REPO 的 Node Agent 源码包…"
    if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
      gh api "repos/$REPO/tarball/$REF_ENCODED" > "$TMP/source.tar.gz"
    else
      download "https://api.github.com/repos/$REPO/tarball/$REF_ENCODED" "$TMP/source.tar.gz"
    fi
    mkdir "$TMP/source"
    tar -xzf "$TMP/source.tar.gz" -C "$TMP/source" --strip-components=1
    SOURCE="$TMP/source/packages/agent"
  fi
  # Install a real tarball: npm's directory symlink would break when TMP is removed.
  PACKED="$(npm pack "$SOURCE" --ignore-scripts --silent --pack-destination "$TMP")"
  SPEC="$TMP/$PACKED"
fi
NPM_PREFIX="$INSTALL_HOME/npm"
npm install --global --prefix "$NPM_PREFIX" --ignore-scripts --omit=dev --no-audit --no-fund "$SPEC"
CLI="$NPM_PREFIX/lib/node_modules/@soyons/pangolin-agent/bin/pangolin-agent.js"
if [[ ! -f "$CLI" ]]; then echo '安装包中没有 Pangolin Agent CLI。' >&2; exit 1; fi
BIN_DIR="${PANGOLIN_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$BIN_DIR"
BIN_DIR="$(cd "$BIN_DIR" && pwd)"
LAUNCHER="$BIN_DIR/pangolin-agent"
{
  printf '#!/usr/bin/env bash\n'
  printf 'export PATH=%q:"$PATH"\n' "$(dirname "$NODE"):$NPM_PREFIX/bin"
  printf 'exec %q %q "$@"\n' "$NODE" "$CLI"
} > "$LAUNCHER"
chmod 700 "$LAUNCHER"
export PATH="$BIN_DIR:$(dirname "$NODE"):$NPM_PREFIX/bin:$PATH"
"$LAUNCHER" setup "$@"
printf '\n安装完成。管理命令：%s\n' "$LAUNCHER"
printf '查看状态：%q status\n查看日志：%q logs\n' "$LAUNCHER" "$LAUNCHER"
printf '若希望直接输入 pangolin-agent，请将 %s 加入 PATH。\n' "$BIN_DIR"
