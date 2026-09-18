#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PORT=18080
NETWORK=pangolin
START=true
while (($#)); do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --network) NETWORK="$2"; shift 2 ;;
    --no-start) START=false; shift ;;
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
python3 - "$PORT" "$NETWORK" <<'PY'
import json, pathlib, re, secrets, sys, os
port=int(sys.argv[1]); network=sys.argv[2]
if not 1024 <= port <= 65535 or not re.fullmatch('[A-Za-z0-9_-]{1,80}',network):
 raise SystemExit('Invalid port or network')
root=pathlib.Path('.pangolin')
config=root/'server.json'
if config.exists():data=json.loads(config.read_text())
else:data={'USER_TOKEN':secrets.token_urlsafe(32),'DEVICE_TOKENS':'devbox:'+secrets.token_urlsafe(32)}
for key in ['USER_TOKEN','DEVICE_TOKENS']:
 if not re.fullmatch('[A-Za-z0-9_:,-]{32,}',data[key]):raise SystemExit('Invalid saved token configuration')
for file,text in [(config,json.dumps(data,indent=2)+'\n'),(root/'server.env',''.join(k+'='+data[k]+'\n' for k in data)),(root/'compose.env',f'PANGOLIN_PORT={port}\nPANGOLIN_NETWORK={network}\n')]:
 file.write_text(text);file.chmod(0o600)
PY
if [[ "$START" == true ]]; then
  docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null
  docker compose --env-file .pangolin/compose.env up -d --build --wait --wait-timeout 90
fi
echo "Pangolin Docker 配置已完成。本机地址：http://127.0.0.1:$PORT"
echo "查看状态：docker compose --env-file .pangolin/compose.env ps"
echo "查看日志：docker compose --env-file .pangolin/compose.env logs -f relay"
echo "查看凭据：python3 scripts/docker-credentials.py"
echo '首次安装后请配置 HTTPS 入口；不会把控制接口直接暴露到公网 HTTP。'
