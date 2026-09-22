"""Local-only account administration. No administrator bypass exists in the public API."""
import argparse
import getpass
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys

from pydantic import ValidationError
from .accounts import PASSWORDS
from .models import Credentials, email_address
from .storage import Store, digest


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Pangolin 本机账号管理')
    parser.add_argument('action', choices=['create-user', 'reset-password', 'import-device', 'backup'])
    parser.add_argument('--database', default=os.environ.get('PANGOLIN_DATABASE', '.pangolin/server.sqlite3'))
    parser.add_argument('--email')
    parser.add_argument('--password-file', type=Path)
    parser.add_argument('--device')
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    if args.action != 'backup' and not args.email:
        parser.error('--email is required')
    store = Store(args.database)
    try:
        if args.action == 'backup':
            if not args.output:
                parser.error('--output is required')
            store.backup(args.output)
            print('数据库备份完成：' + args.output)
            return
        email = email_address(args.email)
        existing = store.user(email)
        if args.action == 'import-device':
            if not existing:
                raise ValueError('请先创建目标账号')
            if not args.device or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', args.device):
                raise ValueError('请通过 --device 指定旧设备 ID')
            with store.db:
                row = store.db.execute('SELECT user_id FROM devices WHERE id=?', (args.device,)).fetchone()
                if row and row['user_id'] != existing['id']:
                    raise ValueError('设备已归属其他账号')
                store.db.execute('''INSERT OR IGNORE INTO devices(id,user_id,name,token_hash,expires_at,revoked)
                                  VALUES(?,?,?,?,0,1)''', (args.device, existing['id'], args.device, digest(secrets.token_urlsafe(32))))
            print('已设置旧设备归属。在内网机器执行 pangolin-agent login --sync-existing 上传旧历史。')
            return
        if args.action == 'create-user' and existing:
            raise ValueError('账号已存在；修改密码请使用 reset-password')
        if args.action == 'reset-password' and not existing:
            raise ValueError('账号不存在')
        if args.password_file:
            password = args.password_file.read_text().removesuffix('\n').removesuffix('\r')
        else:
            with open('/dev/tty', 'r+') as terminal:
                password = getpass.getpass('新密码（至少 12 位）: ', stream=terminal)
                if password != getpass.getpass('再次输入新密码: ', stream=terminal):
                    raise ValueError('两次密码不一致')
        credentials = Credentials(email=email, password=password)
        encoded = PASSWORDS.hash(credentials.password)
        if args.action == 'create-user':
            store.create_user(email, encoded)
            print('账号已创建：' + email)
        else:
            store.change_password(existing['id'], encoded)
            print('密码已重置，网页与设备登录已撤销；运行中的连接将在数秒内断开。')
    finally:
        store.close()


if __name__ == '__main__':
    try:
        main()
    except ValidationError:
        print('账号参数无效：邮箱格式需正确，密码长度为 12–128 位。', file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError, sqlite3.Error) as error:
        print('账号管理失败：' + str(error), file=sys.stderr)
        sys.exit(1)
