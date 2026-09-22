#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PORT=18080
NETWORK=pangolin
START=true
AUTH_MODE=
REGISTRATION=
while (($#)); do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --network) NETWORK="$2"; shift 2 ;;
    --no-start) START=false; shift ;;
    --auth-mode) AUTH_MODE="$2"; shift 2 ;;
    --registration) REGISTRATION="$2"; shift 2 ;;
    -h|--help)
      echo 'Usage: bash install.sh server --docker [--port 18080] [--network pangolin] [--no-start]'
      echo 'Docker server binds localhost; place HTTPS reverse proxy in the same Docker network.'
      exit 0 ;;
    *) echo "Unknown Docker server option: $1 (native installer: server --native ...)" >&2; exit 1 ;;
  esac
done
command -v docker >/dev/null || { echo 'Docker is required. Install Docker Engine + Compose, then retry.' >&2; exit 1; }
docker compose version >/dev/null
mkdir -p .pangolin
chmod 700 .pangolin
python3 - "$PORT" "$NETWORK" "$AUTH_MODE" "$REGISTRATION" <<'PY'
import json, pathlib, re, secrets, sys, os
port=int(sys.argv[1]); network=sys.argv[2]
if not 1024 <= port <= 65535 or not re.fullmatch('[A-Za-z0-9_-]{1,80}',network):
 raise SystemExit('Invalid port or network')
root=pathlib.Path('.pangolin')
config=root/'server.json'
if config.exists():
 data=json.loads(config.read_text())
 data.setdefault('PANGOLIN_AUTH_MODE','legacy')
else:data={'PANGOLIN_AUTH_MODE':'accounts'}
if sys.argv[3]:data['PANGOLIN_AUTH_MODE']=sys.argv[3]
if sys.argv[4]:data['PANGOLIN_REGISTRATION']=sys.argv[4]
data.setdefault('PANGOLIN_REGISTRATION','open')
if data['PANGOLIN_AUTH_MODE'] not in ('accounts','legacy') or data['PANGOLIN_REGISTRATION'] not in ('open','closed'):
 raise SystemExit('Invalid authentication mode or registration policy')
if data['PANGOLIN_AUTH_MODE']=='legacy':
 data.setdefault('USER_TOKEN',secrets.token_urlsafe(32))
 data.setdefault('DEVICE_TOKENS','devbox:'+secrets.token_urlsafe(32))
for key in ['USER_TOKEN','DEVICE_TOKENS']:
 if key in data and not re.fullmatch('[A-Za-z0-9_:,-]{32,}',data[key]):raise SystemExit('Invalid saved token configuration')
for file,text in [(config,json.dumps(data,indent=2)+'\n'),(root/'server.env',''.join(k+'='+str(v)+'\n' for k,v in data.items() if k in ('USER_TOKEN','DEVICE_TOKENS','PANGOLIN_AUTH_MODE','PANGOLIN_REGISTRATION','PANGOLIN_PUBLIC_URL','PANGOLIN_HISTORY_DAYS'))),(root/'compose.env',f'PANGOLIN_PORT={port}\nPANGOLIN_NETWORK={network}\n')]:
 file.write_text(text);file.chmod(0o600)
PY
if [[ "$START" == true ]]; then
  docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null
  docker compose --env-file .pangolin/compose.env up -d --build --wait --wait-timeout 90
fi
echo "Pangolin Docker 配置已完成。本机地址：http://127.0.0.1:$PORT"
echo "查看状态：docker compose --env-file .pangolin/compose.env ps"
echo "查看日志：docker compose --env-file .pangolin/compose.env logs -f relay"
echo "登录方式：python3 scripts/docker-credentials.py"
echo "账号管理：docker compose --env-file .pangolin/compose.env exec relay python -m server.admin create-user --email 你的邮箱"
echo '首次安装后请配置 HTTPS 入口；不会把控制接口直接暴露到公网 HTTP。'
