#!/usr/bin/env python3
"""Stage or deploy launchd services under a dedicated macOS identity.

Dependencies must be prepared separately; this installer never downloads,
creates a Runpod resource, or reads a credential into its output.
"""
import argparse
import grp
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import pwd
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
PREFIX = Path('/opt/research-agents')
CONFIG = Path('/etc/research-compute/config.json')
STATE = Path('/var/lib/research-compute')
SOCKET = Path('/var/run/research-compute/control.sock')
IDENTITY = '_researchcompute'


def run(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout


def unit(kind, client_group):
    command = [str(PREFIX / 'deps/python/bin/python3'), str(PREFIX / 'bin/research-compute'),
               kind, '--config', str(CONFIG), '--state-dir', str(STATE)]
    if kind == 'daemon':
        command += ['--socket', str(SOCKET)]
    return {'Label': 'io.aniket.research-compute.' + kind,
            'UserName': IDENTITY, 'GroupName': client_group,
            'ProgramArguments': command, 'RunAtLoad': True, 'KeepAlive': True,
            'ThrottleInterval': 10, 'Umask': 0o007,
            'WorkingDirectory': str(STATE),
            'EnvironmentVariables': {'HOME': str(STATE), 'PATH': str(PREFIX / 'deps') + ':/usr/bin:/bin',
                                     'PYTHONDONTWRITEBYTECODE': '1'},
            'StandardOutPath': str(STATE / (kind + '.log')),
            'StandardErrorPath': str(STATE / (kind + '.err.log'))}


def stage(root, config_path, python_tree, node, provider_tree, client_uid, client_group):
    root = root.resolve()
    spec = importlib.util.spec_from_file_location('installer', SOURCE / 'scripts/install.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    from research_runtime.service import load_config
    config = load_config(config_path)
    config['client_uids'] = [client_uid]
    if config['provider']['kind'] == 'mcp':
        config['provider']['env_file'] = '/etc/research-compute/runpod.env'
        config['provider']['command'] = [str(PREFIX / 'deps/node'),
            str(PREFIX / 'deps/provider/node_modules/@runpod/mcp-server/dist/stdio.mjs')]
    destinations = {(root / CONFIG.relative_to('/')).parent.resolve() / CONFIG.name:
                    (json.dumps(config, indent=2) + '\n').encode()}
    for kind in ('daemon', 'watchdog'):
        destinations[root / f'Library/LaunchDaemons/io.aniket.research-compute.{kind}.plist'] = plistlib.dumps(unit(kind, client_group))
    for path, data in destinations.items():
        installer.reject_symlinks(path)
        if path.exists() and path.read_bytes() != data:
            raise ValueError('Existing deployment differs; pause work and arrange a deliberate upgrade first')
    target = (root / PREFIX.relative_to('/')).resolve()
    files = {'bin/research-compute': ((SOURCE / 'bin/research-compute').read_bytes(), True)}
    for path in (SOURCE / 'research_runtime').rglob('*.py'):
        if '__pycache__' not in path.parts:
            if path.is_symlink():
                raise ValueError('Linked runtime source is not allowed')
            files[str(path.relative_to(SOURCE))] = (path.read_bytes(), False)
    installer.check_owned(target, files)
    dependencies = ((python_tree, target / 'deps/python'), (provider_tree, target / 'deps/provider'))
    for source, destination in dependencies:
        if source is not None and destination.exists():
            raise ValueError('Dependency destination already exists; do not overwrite an active runtime')
    installer.owned_files(target, files)
    for source, destination in dependencies:
        if source is not None:
            # Dereference links: no service executable may point into a user home.
            shutil.copytree(source, destination, symlinks=False,
                            ignore=shutil.ignore_patterns('pkgconfig', 'man') if destination.name == 'python' else None)
    if node is not None:
        (target / 'deps').mkdir(exist_ok=True)
        shutil.copy2(node, target / 'deps/node')
    for path, data in destinations.items():
        installer.write_file(path, data)
        path.chmod(0o640 if path.name == 'config.json' else 0o644)
    return config


def deploy(args):
    if sys.platform != 'darwin' or os.geteuid() != 0:
        raise ValueError('Actual deployment requires macOS and sudo; --stage-root is read-only to the system')
    client = pwd.getpwnam(args.client_user)
    if client.pw_uid == 0 or client.pw_name == IDENTITY:
        raise ValueError('An ordinary client user is required')
    group = grp.getgrgid(client.pw_gid).gr_name
    for kind in ('daemon', 'watchdog'):
        result = subprocess.run(['launchctl', 'print', 'system/io.aniket.research-compute.' + kind], capture_output=True)
        if result.returncode == 0:
            raise ValueError('Services are already registered; pause experiments before upgrading')
    for path in (PREFIX, CONFIG.parent, STATE, SOCKET.parent):
        for parent in (path, *path.parents):
            # /var and /etc are standard macOS aliases, resolved before checking.
            if parent.is_symlink() and str(parent) not in ('/var', '/etc'):
                raise ValueError('Refusing linked deployment path')
    if not args.python_tree or not args.node or not args.provider_tree:
        raise ValueError('Protected Python, Node, and preinstalled MCP source trees are required')
    from research_runtime.provider import _api_key
    _api_key({'env_file': '/etc/research-compute/runpod.env'})
    try:
        service = pwd.getpwnam(IDENTITY)
        if service.pw_uid == 0 or service.pw_shell not in ('/usr/bin/false', '/usr/sbin/nologin'):
            raise ValueError('Existing service identity is not a dedicated non-login user')
    except KeyError:
        used = {p.pw_uid for p in pwd.getpwall()}
        uid = next((number for number in range(450, 500) if number not in used), None)
        if uid is None:
            raise ValueError('No available service UID')
        record = '/Users/' + IDENTITY
        for field, value in (('UniqueID', str(uid)), ('PrimaryGroupID', str(client.pw_gid)),
                             ('UserShell', '/usr/bin/false'), ('NFSHomeDirectory', str(STATE)),
                             ('IsHidden', '1'), ('RealName', 'Research compute controller')):
            run(['dscl', '.', '-create', record, field, value])
        service = pwd.getpwnam(IDENTITY)
    config = stage(Path('/'), args.config, args.python_tree, args.node, args.provider_tree, client.pw_uid, group)
    for directory in (CONFIG.parent, STATE, SOCKET.parent):
        directory.mkdir(parents=True, exist_ok=True)
        os.chown(directory, service.pw_uid, client.pw_gid)
        directory.chmod(0o700 if directory == STATE else 0o750)
    # Config/code stay root-owned; only state and credential belong to the service.
    os.chown(CONFIG.parent, 0, client.pw_gid)
    os.chown(CONFIG, 0, client.pw_gid)
    key = CONFIG.parent / 'runpod.env'
    os.chown(key, service.pw_uid, client.pw_gid)
    key.chmod(0o600)
    for path in (PREFIX, *PREFIX.rglob('*')):
        if path.is_symlink():
            raise ValueError('Protected runtime contains a symlink')
        os.chown(path, 0, 0)
        path.chmod((path.stat().st_mode & 0o777) & ~0o022)
    run([str(PREFIX / 'deps/python/bin/python3'), '-c', 'import sys,sqlite3; assert sys.version_info >= (3,11)'])
    run([str(PREFIX / 'deps/node'), '--version'])
    if args.enable:
        # The independent watchdog must be live before any admission.
        for kind in ('watchdog', 'daemon'):
            run(['launchctl', 'bootstrap', 'system', f'/Library/LaunchDaemons/io.aniket.research-compute.{kind}.plist'])
    return {'services_started': args.enable, 'socket': str(SOCKET), 'client_uid': client.pw_uid,
            'provider': config['provider']['kind']}


if __name__ == '__main__':
    sys.path.insert(0, str(SOURCE))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--client-user', default=os.environ.get('SUDO_USER', 'aniket'))
    parser.add_argument('--python-tree', type=Path)
    parser.add_argument('--node', type=Path)
    parser.add_argument('--provider-tree', type=Path)
    parser.add_argument('--stage-root', type=Path)
    parser.add_argument('--enable', action='store_true')
    args = parser.parse_args()
    try:
        if args.stage_root:
            if args.enable:
                raise ValueError('--enable cannot be combined with --stage-root')
            client = pwd.getpwnam(args.client_user)
            stage(args.stage_root.resolve(), args.config, args.python_tree, args.node, args.provider_tree,
                  client.pw_uid, grp.getgrgid(client.pw_gid).gr_name)
            result = {'staged_root': str(args.stage_root), 'services_started': False}
        else:
            result = deploy(args)
        print(json.dumps(result, indent=2))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print('Deployment failed: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
