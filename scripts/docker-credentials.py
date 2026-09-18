"""Show saved credentials only on explicit local invocation."""
import json
from pathlib import Path
p = Path(__file__).resolve().parents[1] / '.pangolin/server.json'
config = json.loads(p.read_text())
print('浏览器 Token:', config['USER_TOKEN'])
for pair in config['DEVICE_TOKENS'].split(','):
    device, token = pair.split(':', 1)
    print('设备 ID:', device)
    print('设备 Token:', token)
