"""Exercise the CI connection lifecycle without network or root privileges."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('deploy-wireguard.sh')
KEY = base64.b64encode(bytes(range(32))).decode()
CONFIG = f'''[Interface]
PrivateKey = {KEY}
Address = 10.8.0.240/32
MTU = 1280
[Peer]
PublicKey = {KEY}
AllowedIPs = 10.8.0.1/32
Endpoint = example.test:51048
PersistentKeepalive = 25
'''


class VpnDeploymentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        binaries = self.root / 'bin'
        binaries.mkdir()
        program = '''import json,os,sys
from pathlib import Path
with open(os.environ['CALLS'], 'a') as f:
 f.write(json.dumps([Path(sys.argv[0]).name,*sys.argv[1:]])+'\\n')
if 'latest-handshakes' in sys.argv:
 print('test-public-key\\t1')
'''
        for command in ('sudo', 'ssh'):
            target = binaries / command
            target.write_text('#!' + sys.executable + '\n' + program)
            target.chmod(0o755)
        self.env = {**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
                    'RUNNER_TEMP': str(self.root), 'CALLS': str(self.root / 'calls.jsonl'),
                    'DEPLOY_WIREGUARD_CONFIG': CONFIG, 'DEPLOY_SSH_PRIVATE_KEY': 'synthetic-key-only', 'DEPLOY_TAG': 'dev-123abcd'}

    def run_script(self, operation, role='backend', **patch):
        return subprocess.run(['bash', str(SCRIPT), operation, role], env={**self.env, **patch}, capture_output=True, text=True)

    def calls(self):
        path = self.root / 'calls.jsonl'
        return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []

    def test_up_creates_private_files_and_never_prints_credentials(self):
        result = self.run_script('up')
        self.assertEqual(result.returncode, 0, result.stderr)
        for filename in ('semantic-ci.conf', 'deploy-key', 'known_hosts'):
            self.assertEqual((self.root / 'semantic-home-vpn' / filename).stat().st_mode & 0o777, 0o600)
        self.assertNotIn(KEY, result.stdout + result.stderr)
        self.assertNotIn('synthetic-key-only', result.stdout + result.stderr)
        self.assertEqual(self.calls()[-1][1:3], ['wg-quick', 'up'])

    def test_rejects_hooks_and_network_wide_routes_before_privileged_commands(self):
        for value in (CONFIG + 'PostUp = touch /tmp/not-allowed\n', CONFIG.replace('AllowedIPs = 10.8.0.1/32', 'AllowedIPs = 0.0.0.0/0'),
                      CONFIG.replace('Address = 10.8.0.240/32', 'Address = 10.8.0.240/24')):
            with self.subTest(config=value):
                self.assertNotEqual(self.run_script('up', DEPLOY_WIREGUARD_CONFIG=value).returncode, 0)
                self.assertEqual(self.calls(), [])

    def test_check_authenticates_with_pinned_host_and_lists_permission_without_deploying(self):
        result = self.run_script('check', role='frontend')
        self.assertEqual(result.returncode, 0, result.stderr)
        ssh = self.calls()[0]
        self.assertIn('StrictHostKeyChecking=yes', ssh)
        self.assertIn('deploy@10.8.0.1', ssh)
        self.assertIn('sudo -n -l /opt/home-automation/bin/deploy-frontend.sh', ssh[-1])
        self.assertNotIn('sudo -n /opt/home-automation/bin', ssh[-1])

    def test_deploy_validates_service_and_tag_before_ssh(self):
        for role, tag in [('backend', 'latest; touch nope'), ('untrusted', 'dev-123abcd')]:
            self.assertNotEqual(self.run_script('deploy', role, DEPLOY_TAG=tag).returncode, 0)
            self.assertEqual(self.calls(), [])
        self.assertEqual(self.run_script('deploy').returncode, 0)
        self.assertEqual(self.calls()[-1][-1], 'sudo -n /opt/home-automation/bin/deploy-backend.sh')

    def test_cleanup_removes_only_the_ephemeral_vpn_and_key_files(self):
        self.assertEqual(self.run_script('up').returncode, 0)
        self.assertEqual(self.run_script('down').returncode, 0)
        self.assertFalse(any((self.root / 'semantic-home-vpn').iterdir()))
        self.assertTrue(any(call[1:3] == ['wg-quick', 'down'] for call in self.calls()))


if __name__ == '__main__':
    unittest.main()
