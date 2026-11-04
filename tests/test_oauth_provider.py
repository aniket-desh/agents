"""Hosted OAuth transport checks without credentials, browser or provider access."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_provider import SERVER
from research_runtime.provider import MCPProvider, ProviderError, _StdioMCP


class OAuthProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token = self.root / 'tokens.json'
        self.token.write_text(json.dumps({'access_token': 'browser-fixture-credential', 'token_type': 'Bearer'}))
        self.token.chmod(0o600)
        self.config = {'auth': 'oauth', 'oauth_token_file': str(self.token),
                       'command': [sys.executable, '-u', '-c', SERVER]}

    def test_oauth_uses_private_cache_without_api_key_or_inherited_credentials(self):
        script = 'import os\nassert not any(k in os.environ for k in ("RUNPOD_API_KEY", "ANTHROPIC_API_KEY", "UNRELATED_SECRET"))\n' + SERVER
        self.config['command'] = [sys.executable, '-u', '-c', script]
        with patch.dict(os.environ, {'RUNPOD_API_KEY': 'not-the-authorization',
                                     'ANTHROPIC_API_KEY': 'unrelated', 'UNRELATED_SECRET': 'unrelated'}):
            provider = MCPProvider(self.config)
            try:
                self.assertEqual([p['id'] for p in provider.list()], ['p1'])
                self.assertEqual(provider.get('p1')['name'], 'aniket-test')
            finally:
                provider.close()

    def test_missing_invalid_or_public_cache_fails_before_starting_process(self):
        for data in ({}, {'access_token': ''}, {'access_token': 'bad token'},
                     {'access_token': 'a', 'token_type': 'Basic'}, ['unexpected']):
            self.token.write_text(json.dumps(data))
            with self.subTest(data_type=type(data).__name__), patch('research_runtime.provider.subprocess.Popen') as proc:
                with self.assertRaises(ProviderError):
                    _StdioMCP(self.config)
                proc.assert_not_called()
        self.token.write_text('{not json')
        with self.assertRaises(ProviderError):
            _StdioMCP(self.config)
        self.token.write_text(json.dumps({'access_token': 'private'}))
        self.token.chmod(0o644)
        with self.assertRaises(ProviderError):
            _StdioMCP(self.config)
        self.token.unlink()
        with patch.dict(os.environ, {'RUNPOD_API_KEY': 'must-not-fallback'}):
            with self.assertRaises(ProviderError):
                _StdioMCP(self.config)

    def test_symlink_and_permissionless_network_volume_are_refused(self):
        link = self.root / 'link.json'
        link.symlink_to(self.token)
        with self.assertRaises(ProviderError):
            _StdioMCP({**self.config, 'oauth_token_file': str(link)})
        with self.assertRaisesRegex(ProviderError, 'local storage'):
            _StdioMCP({**self.config, 'oauth_token_file': '/workspace/forged-tokens.json'})

    def test_auth_errors_never_leak_provider_details_and_close_transport(self):
        script = r'''
import json,sys
for line in sys.stdin:
 msg=json.loads(line)
 if 'id' in msg:
  print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'error':{'message':'browser-fixture-credential other-private-response'}}),flush=True)
'''
        self.config['command'] = [sys.executable, '-u', '-c', script]
        with self.assertRaises(ProviderError) as result:
            _StdioMCP(self.config)
        self.assertNotIn('browser-fixture-credential', str(result.exception))
        self.assertNotIn('other-private-response', str(result.exception))
        self.assertIn('owner browser login', str(result.exception))

    def test_oauth_is_explicit_and_requires_the_hosted_proxy_command(self):
        for config in ({**self.config, 'auth': 'guess'},
                       {'auth': 'oauth', 'oauth_token_file': str(self.token)},
                       {**self.config, 'oauth_token_file': 'relative.json'}):
            with patch('research_runtime.provider.subprocess.Popen') as proc:
                with self.assertRaises(ProviderError):
                    _StdioMCP(config)
                proc.assert_not_called()


if __name__ == '__main__':
    unittest.main()
