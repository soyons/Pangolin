"""Exercise the shell bootstrap with isolated filesystem targets and fake downloads."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'packages/agent/install.sh'


@pytest.fixture
def node_bootstrap(tmp_path):
    bins = tmp_path / 'bin'
    bins.mkdir()
    source = tmp_path / 'source'
    source.mkdir()
    shutil.copy(INSTALLER, source / 'install.sh')
    (source / 'package.json').write_text('{"name":"@soyons/pangolin-agent","version":"0.1.0"}')
    archive = tmp_path / 'source.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(source, arcname='checkout/packages/agent')
    env = {**os.environ, 'PATH': str(bins) + os.pathsep + os.environ['PATH'],
           'PANGOLIN_INSTALL_HOME': str(tmp_path / 'program path'), 'PANGOLIN_BIN_DIR': str(tmp_path / 'launchers'),
           'CAPTURE': str(tmp_path / 'args.json'), 'NPM_CAPTURE': str(tmp_path / 'npm'),
           'DOWNLOAD_CAPTURE': str(tmp_path / 'downloads'), 'FIXTURE_ARCHIVE': str(archive)}
    # Fake only external operations; execute the real installer, shell wrapper and CLI argv forwarding.
    scripts = {
        'id': '#!/bin/sh\necho 1000\n',
        'tmux': '#!/bin/sh\nexit 0\n',
        'gh': '#!/bin/sh\nexit 1\n',
        'python3': '#!/bin/sh\nexit 99\n',
        'git': '#!/bin/sh\nexit 99\n',
        'curl': '''#!/bin/bash
printf '%s\\n' "$@" >> "$DOWNLOAD_CAPTURE"
url=''
while (($#)); do
  case "$1" in
    https:*) url="$1"; shift ;;
    -o) output="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$url" in
  */SHASUMS256.txt) printf '%064d  node-v22.18.0-linux-x64.tar.gz\\n' 0 > "$output" ;;
  */node-v*) printf 'corrupt archive' > "$output" ;;
  *) cp "$FIXTURE_ARCHIVE" "$output" ;;
esac
''',
        'npm': '''#!/bin/bash
set -eu
printf '%s\\n' "$@" >> "$NPM_CAPTURE"
[[ "${FAIL_NPM:-}" != 1 ]] || exit 7
if [[ "$1" == pack ]]; then
  touch "${!#}/soyons-pangolin-agent-0.1.0.tgz"
  echo soyons-pangolin-agent-0.1.0.tgz
else
  while [[ "$1" != --prefix ]]; do shift; done
  target="$2/lib/node_modules/@soyons/pangolin-agent/bin"
  mkdir -p "$target"
  cat > "$target/pangolin-agent.js" <<'JS'
require('node:fs').writeFileSync(process.env.CAPTURE, JSON.stringify(process.argv.slice(2)));
JS
fi
''',
    }
    for name, content in scripts.items():
        path = bins / name
        path.write_text(content)
        path.chmod(0o755)
    return source, bins, env


@pytest.mark.skipif(not shutil.which('node'), reason='Node is required for wrapper smoke')
@pytest.mark.parametrize('piped', [False, True])
def test_node_bootstrap_packs_then_installs_without_python_or_git(node_bootstrap, piped):
    source, bins, env = node_bootstrap
    arguments = ['--relay', 'https://example.com', '--project', '/space $value/quote"', '--no-start']
    if piped:
        env['PANGOLIN_REF'] = 'feature/install'
        result = subprocess.run(['bash', '-s', '--', *arguments], input=INSTALLER.read_text(), text=True,
                                env=env, capture_output=True)
        assert 'feature%2Finstall' in Path(env['DOWNLOAD_CAPTURE']).read_text()
    else:
        result = subprocess.run(['bash', str(source / 'install.sh'), *arguments], env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(Path(env['CAPTURE']).read_text()) == ['setup', *arguments]
    npm = Path(env['NPM_CAPTURE']).read_text()
    assert '--ignore-scripts' in npm and '--global' in npm and '.tgz' in npm
    assert not (Path(env['PANGOLIN_INSTALL_HOME']) / 'npm/lib/node_modules/@soyons/pangolin-agent').is_symlink()


@pytest.mark.skipif(not shutil.which('node'), reason='Node is required')
def test_npm_failure_does_not_run_setup(node_bootstrap):
    source, bins, env = node_bootstrap
    env.update(FAIL_NPM='1', PANGOLIN_PACKAGE='/fake/package.tgz')
    result = subprocess.run(['bash', str(source / 'install.sh'), '--no-start'], env=env, capture_output=True)
    assert result.returncode != 0
    assert not Path(env['CAPTURE']).exists()
    assert not (Path(env['PANGOLIN_BIN_DIR']) / 'pangolin-agent').exists()


def test_corrupt_node_download_never_extracts_or_installs(node_bootstrap):
    source, bins, env = node_bootstrap
    for name, content in {
        'node': '#!/bin/sh\nexit 1\n',
        'uname': '#!/bin/sh\nif [ "$1" = -s ]; then echo Linux; else echo x86_64; fi\n',
        'tar': '#!/bin/sh\ntouch "$CAPTURE"\nexit 99\n',
    }.items():
        path = bins / name
        path.write_text(content)
        path.chmod(0o755)
    result = subprocess.run(['bash', str(source / 'install.sh')], env=env, capture_output=True, text=True)
    assert result.returncode != 0 and '校验失败' in result.stderr
    assert not Path(env['CAPTURE']).exists()
    assert not Path(env['NPM_CAPTURE']).exists()
