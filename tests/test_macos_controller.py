import importlib.util
import json
import os
from pathlib import Path
import plistlib
import socket
import tempfile
import unittest

from research_runtime.service import peer_uid, load_config

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class MacOSControllerTests(unittest.TestCase):
    def test_peer_identity_is_obtained_from_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            listener = socket.socket(socket.AF_UNIX)
            self.addCleanup(listener.close)
            listener.bind(str(Path(tmp) / 'control.sock'))
            listener.listen()
            client = socket.socket(socket.AF_UNIX)
            self.addCleanup(client.close)
            client.connect(str(Path(tmp) / 'control.sock'))
            connection, _ = listener.accept()
            self.addCleanup(connection.close)
            self.assertEqual(peer_uid(connection), os.getuid())

    def test_staging_protects_identity_and_preserves_existing_config(self):
        installer = module('macos_installer', 'install-controller-macos.py')
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            installer.stage(stage, ROOT / 'config.macos.example.json', None, None, None, 501, 'staff')
            configuration = load_config(stage / 'etc/research-compute/config.json')
            self.assertIsNone(configuration['limits']['budget_usd'])
            self.assertEqual(configuration['client_uids'], [501])
            for kind in ('daemon', 'watchdog'):
                path = stage / f'Library/LaunchDaemons/io.aniket.research-compute.{kind}.plist'
                data = plistlib.loads(path.read_bytes())
                self.assertEqual(data['UserName'], '_researchcompute')
                self.assertTrue(data['KeepAlive'])
                self.assertTrue(data['ProgramArguments'][0].startswith('/opt/research-agents/'))
                self.assertIn('/var/lib/research-compute', data['ProgramArguments'])
            config = stage / 'etc/research-compute/config.json'
            before = config.read_bytes()
            with self.assertRaisesRegex(ValueError, 'Existing deployment differs'):
                installer.stage(stage, ROOT / 'config.macos.example.json', None, None, None, 502, 'staff')
            self.assertEqual(config.read_bytes(), before)

    def test_key_entry_is_private_refuses_overwrite_and_links(self):
        store = module('key_entry', 'store-controller-key.py')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'private/runpod.env'
            store.store_key(path, 'fake-test-key')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.read_text(), 'RUNPOD_API_KEY=fake-test-key\n')
            with self.assertRaises(FileExistsError):
                store.store_key(path, 'replacement')
            self.assertEqual(path.read_text(), 'RUNPOD_API_KEY=fake-test-key\n')
            linked = path.with_name('linked.env')
            linked.symlink_to(path)
            with self.assertRaises(ValueError):
                store.store_key(linked, 'replacement')
            with self.assertRaises(ValueError):
                store.store_key(path.with_name('invalid.env'), 'invalid\nkey')
