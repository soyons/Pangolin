"""Ubuntu/Debian IP HTTPS using Certbot's shortlived profile and Caddy."""
import argparse
import ipaddress
from pathlib import Path
import subprocess
import sys
import tempfile


def public_ip(value):
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise ValueError('Use the public IP assigned to your server')
    return str(address)


def host(value):
    return '[' + value + ']' if ':' in value else value


def site_block(address, port, tls=False):
    site = host(address)
    block = (f'http://{site} {{\n'
             '    handle /.well-known/acme-challenge/* {\n'
             '        root * /var/lib/pangolin-acme\n'
             '        file_server\n'
             '    }\n    handle {\n        respond "Use HTTPS" 404\n    }\n}\n')
    if tls:
        block += (f'https://{site} {{\n'
                  '    tls /var/lib/caddy/pangolin-cert/fullchain.pem /var/lib/caddy/pangolin-cert/privkey.pem\n'
                  f'    reverse_proxy 127.0.0.1:{port}\n}}\n')
    return block


def merge_caddy(old, block, address):
    import re
    start, end = '# BEGIN PANGOLIN', '# END PANGOLIN'
    if start in old:
        if old.count(start) != 1 or old.count(end) != 1:
            raise ValueError('Unrecognized Pangolin block in Caddyfile')
        old = re.sub(re.escape(start) + r'.*?' + re.escape(end), '', old, flags=re.S)
    if address in old:
        raise ValueError('IP already appears in Caddyfile; review the existing site first')
    return old.rstrip() + '\n\n' + start + '\n' + block + end + '\n'


def run(*args):
    subprocess.run([str(a) for a in args], check=True)


def write(path, text, mode=0o644):
    path = Path(path)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as f:
        f.write(text)
        temp = Path(f.name)
    temp.chmod(mode)
    temp.replace(path)


def apply_caddy(text):
    with tempfile.TemporaryDirectory(prefix='pangolin-caddy-') as directory:
        candidate = Path(directory) / 'Caddyfile'
        candidate.write_text(text)
        run('caddy', 'validate', '--config', candidate, '--adapter', 'caddyfile')
        write('/etc/caddy/Caddyfile', text)
        run('systemctl', 'enable', '--now', 'caddy')
        run('systemctl', 'reload', 'caddy')


RENEW_HOOK = '''#!/bin/sh
set -eu
# Only this Pangolin certificate lineage may be copied to the proxy.
[ "${RENEWED_LINEAGE:-}" = /etc/pangolin-acme/live/pangolin-ip ] || exit 0
install -d -o root -g caddy -m 750 /var/lib/caddy/pangolin-cert
install -o root -g caddy -m 640 "$RENEWED_LINEAGE/fullchain.pem" /var/lib/caddy/pangolin-cert/fullchain.pem
install -o root -g caddy -m 640 "$RENEWED_LINEAGE/privkey.pem" /var/lib/caddy/pangolin-cert/privkey.pem
systemctl reload caddy
'''


def main():
    import os
    p = argparse.ArgumentParser()
    p.add_argument('--ip', required=True)
    p.add_argument('--port', type=int, required=True)
    args = p.parse_args()
    address = public_ip(args.ip)
    if os.geteuid() != 0 or sys.version_info < (3, 10):
        raise ValueError('IP HTTPS requires root and Python 3.10+ (Ubuntu 22.04+ / Debian 12+)')
    if not 1024 <= args.port <= 65535:
        raise ValueError('Invalid port')
    run('apt-get', 'update')
    run('apt-get', 'install', '-y', 'caddy', 'python3-venv')
    if not Path('/opt/pangolin-certbot/bin/python').exists():
        run(sys.executable, '-m', 'venv', '/opt/pangolin-certbot')
    run('/opt/pangolin-certbot/bin/python', '-m', 'pip', 'install', 'certbot>=5.4,<6')
    run('install', '-d', '-m', '755', '/var/lib/pangolin-acme')
    config_dir = Path('/etc/pangolin-acme')
    config_dir.mkdir(mode=0o700, exist_ok=True)
    hook = config_dir / 'deploy.sh'
    write(hook, RENEW_HOOK, 0o700)
    target = Path('/etc/caddy/Caddyfile')
    old = target.read_text()
    import time
    write('/etc/caddy/Caddyfile.pangolin-backup-' + str(time.time_ns()), old)
    certbot = ['/opt/pangolin-certbot/bin/certbot', '--config-dir', '/etc/pangolin-acme',
               '--work-dir', '/var/lib/pangolin-acme-work', '--logs-dir', '/var/log/pangolin-acme']
    try:
        # Keep existing HTTPS working during repeat installations/renewal attempts.
        have_cert = Path('/var/lib/caddy/pangolin-cert/privkey.pem').exists()
        apply_caddy(merge_caddy(old, site_block(address, args.port, tls=have_cert), address))
        # Certbot itself asks the user to accept the CA terms on first issuance.
        # A curl pipe has consumed stdin, so reconnect stdin to the controlling TTY.
        tty = open('/dev/tty') if not sys.stdin.isatty() else None
        try:
            subprocess.run([*certbot, 'certonly', '--preferred-profile', 'shortlived',
                            '--webroot', '--webroot-path', '/var/lib/pangolin-acme',
                            '--ip-address', address, '--cert-name', 'pangolin-ip',
                            '--register-unsafely-without-email', '--deploy-hook', str(hook)],
                           check=True, stdin=tty)
        finally:
            if tty:
                tty.close()
        # Ensure copies also exist if Certbot kept a still-valid certificate.
        subprocess.run([str(hook)], env={**os.environ, 'RENEWED_LINEAGE': '/etc/pangolin-acme/live/pangolin-ip'}, check=True)
        apply_caddy(merge_caddy(old, site_block(address, args.port, tls=True), address))
        write('/etc/systemd/system/pangolin-certbot.service',
              '[Unit]\nDescription=Renew Pangolin IP certificate\n[Service]\nType=oneshot\n'
              'ExecStart=' + ' '.join(certbot) + ' renew --cert-name pangolin-ip --quiet\n')
        write('/etc/systemd/system/pangolin-certbot.timer',
              '[Unit]\nDescription=Check Pangolin IP certificate every hour\n[Timer]\n'
              'OnCalendar=hourly\nRandomizedDelaySec=300\nPersistent=true\n[Install]\nWantedBy=timers.target\n')
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', '--now', 'pangolin-certbot.timer')
    except Exception:
        write(target, old)
        subprocess.run(['systemctl', 'reload', 'caddy'])
        raise


if __name__ == '__main__':
    main()
