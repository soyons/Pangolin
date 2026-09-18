#!/usr/bin/env bash
# Pangolin bootstrap. Supports: curl .../install.sh | bash -s -- server|agent
set -euo pipefail
umask 077
ROLE="${1:-}"
case "$ROLE" in
  server|agent) shift ;;
  -h|--help|'')
    echo 'Usage: bash install.sh server [--ip PUBLIC_IP | --domain HOST] [--no-start]'
    echo '       bash install.sh agent --relay URL --project PATH [--no-start]'
    echo '       bash install.sh agent --python ...  (legacy Python Agent)'
    echo '       bash install.sh server --docker [--port 18080] [--no-start]'
    echo 'Local checkout: bash install.sh server; remote bootstrap: curl -fsSL URL | bash -s -- server'
    exit 0 ;;
  *) echo 'Choose server or agent.' >&2; exit 1 ;;
esac
# Agent installation is now an independent Node package. Keep --python for existing deployments.
LEGACY_AGENT=false
if [[ "$ROLE" == agent && "${1:-}" == --python ]]; then LEGACY_AGENT=true; shift; fi
if [[ "$ROLE" == agent && "$LEGACY_AGENT" == false ]]; then
  AGENT_INSTALLER=''
  if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    AGENT_INSTALLER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/packages/agent/install.sh"
  fi
  if [[ -n "$AGENT_INSTALLER" && -f "$AGENT_INSTALLER" ]]; then
    exec bash "$AGENT_INSTALLER" "$@"
  fi
  AGENT_TMP="$(mktemp -d)"
  trap 'rm -rf -- "$AGENT_TMP"' EXIT
  REPO="${PANGOLIN_REPO:-soyons/Pangolin}"
  REF="${PANGOLIN_REF:-main}"
  if [[ ! "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ || ! "$REF" =~ ^[A-Za-z0-9_./-]+$ ]]; then
    echo 'Invalid GitHub repository or ref.' >&2; exit 1
  fi
  if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
    gh api -H 'Accept: application/vnd.github.raw+json' "repos/$REPO/contents/packages/agent/install.sh?ref=$REF" > "$AGENT_TMP/install.sh"
  else
    curl --proto '=https' --tlsv1.2 -fsSL --retry 2 --connect-timeout 15 --max-time 180 "https://raw.githubusercontent.com/$REPO/$REF/packages/agent/install.sh" -o "$AGENT_TMP/install.sh"
  fi
  bash "$AGENT_TMP/install.sh" "$@"
  exit 0
fi
NATIVE=true
if [[ "$ROLE" == server ]]; then
  case "${1:-}" in
    --native) shift ;; # Keep the previous explicit native spelling working.
    --docker) NATIVE=false; shift ;;
  esac
fi
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  if [[ "$ROLE" == agent ]]; then
    echo 'Usage: bash install.sh agent --relay URL --project PATH [--device ID] [--token-file FILE] [--prefix PATH] [--no-start]'
  elif [[ "$NATIVE" == false ]]; then
    echo 'Usage: bash install.sh server --docker [--port 18080] [--network pangolin] [--no-start]'
  else
    echo 'Usage: bash install.sh server [--ip PUBLIC_IP | --domain HOST] [--port 8000] [--device ID] [--prefix PATH] [--no-start]'
  fi
  exit 0
fi
if [[ "$ROLE" == agent && "$(id -u)" == 0 ]]; then
  echo '请用已登录 Codex/Claude 的普通账号安装 Agent，不要 sudo 整条命令。' >&2
  exit 1
fi
# Use the local checkout when invoked from disk, otherwise clone via existing GitHub auth.
SOURCE=''
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
as_admin() { if [[ "$(id -u)" == 0 ]]; then "$@"; else sudo "$@"; fi; }
PACKAGES=()
command -v python3 >/dev/null || PACKAGES+=(python3)
if [[ "$ROLE" == agent ]]; then command -v tmux >/dev/null || PACKAGES+=(tmux); fi
if [[ -z "$SOURCE" || ! -f "$SOURCE/scripts/install.py" ]]; then
  command -v git >/dev/null || PACKAGES+=(git)
fi
if [[ "$(uname -s)" == Linux ]] && command -v apt-get >/dev/null; then
  # Python on Debian splits the venv/ensurepip module into an extra package.
  python3 -c 'import ensurepip, venv' 2>/dev/null || PACKAGES+=(python3-venv)
  if ((${#PACKAGES[@]})); then
    as_admin apt-get update
    as_admin apt-get install -y "${PACKAGES[@]}"
  fi
elif ((${#PACKAGES[@]})); then
  if command -v brew >/dev/null; then
    for pkg in "${PACKAGES[@]}"; do
      [[ "$pkg" == python3 ]] && pkg=python
      brew install "$pkg"
    done
  else
    echo "Please install these prerequisites, then retry: ${PACKAGES[*]} (macOS: Homebrew; Linux: apt)." >&2
    exit 1
  fi
fi
python3 -c 'import sys; assert sys.version_info >= (3,9), "Python 3.9+ required"'
TMP_SOURCE=''
trap '[[ -z "$TMP_SOURCE" ]] || rm -rf -- "$TMP_SOURCE"' EXIT
if [[ -z "$SOURCE" || ! -f "$SOURCE/scripts/install.py" ]]; then
  TMP_SOURCE="$(mktemp -d)"
  SOURCE="$TMP_SOURCE/source"
  REPO="${PANGOLIN_REPO:-soyons/Pangolin}"
  REF="${PANGOLIN_REF:-main}"
  if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
    gh repo clone "$REPO" "$SOURCE" -- --depth 1 --branch "$REF"
  elif GIT_TERMINAL_PROMPT=0 git clone --depth 1 --branch "$REF" "https://github.com/$REPO.git" "$SOURCE"; then
    :
  else
    echo 'HTTPS download unavailable; trying your existing GitHub SSH login.' >&2
    GIT_SSH_COMMAND='ssh -o BatchMode=yes' git clone --depth 1 --branch "$REF" "git@github.com:$REPO.git" "$SOURCE"
  fi
fi
if [[ "$ROLE" == server && "$NATIVE" == false ]]; then
  if [[ -n "$TMP_SOURCE" ]]; then
    DEST="${PANGOLIN_HOME:-$HOME/Pangolin}"
    if [[ -e "$DEST" ]]; then
      echo "Destination already exists: $DEST. Update and run its install.sh, or set PANGOLIN_HOME to an empty path." >&2
      exit 1
    fi
    mv "$SOURCE" "$DEST"
    SOURCE="$DEST"
  fi
  bash "$SOURCE/scripts/docker-server.sh" "$@"
else
  python3 "$SOURCE/scripts/install.py" "$ROLE" --source "$SOURCE" "$@"
fi
