"""Real subprocess regression tests: offline pin resolution and descendant cleanup."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from aiobs_collector import lane_tokscale


class RuntimeTests(unittest.TestCase):
    def test_uses_only_matching_installed_native_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / '.npm/_npx/cached/node_modules/@tokscale/cli-darwin-arm64'
            (package / 'bin').mkdir(parents=True)
            (package / 'package.json').write_text(json.dumps({'name': '@tokscale/cli-darwin-arm64', 'version': '4.14.0'}))
            binary = package / 'bin/tokscale'
            binary.write_text('#!/bin/sh\nprintf \'{"contributions":[]}\'\n')
            binary.chmod(0o755)
            with patch.dict(os.environ, {'PATH': '', 'npm_config_cache': str(root / '.npm')}), \
                    patch('platform.system', return_value='Darwin'), \
                    patch('platform.machine', return_value='arm64'):
                lane_tokscale.clear_tokscale_graph_cache()
                self.assertEqual(lane_tokscale.tokscale_graph('4.14.0'), {'contributions': []})
                with self.assertRaisesRegex(RuntimeError, '4.15.0'):
                    lane_tokscale.tokscale_graph('4.15.0')
        lane_tokscale.clear_tokscale_graph_cache()

    def test_linux_resolves_the_matching_libc_package(self):
        from aiobs_collector.tokscale_runtime import installed_binary
        for libc, suffix in [('glibc', 'gnu'), ('musl', 'musl')]:
            with self.subTest(libc=libc), tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp) / f'_npx/cached/node_modules/@tokscale/cli-linux-x64-{suffix}'
                (package / 'bin').mkdir(parents=True)
                (package / 'package.json').write_text(json.dumps({
                    'name': f'@tokscale/cli-linux-x64-{suffix}', 'version': '4.14.0'}))
                binary = package / 'bin/tokscale'
                binary.write_text('#!/bin/sh\nexit 0\n')
                binary.chmod(0o755)
                with patch.dict(os.environ, {'npm_config_cache': tmp}), \
                        patch('platform.system', return_value='Linux'), \
                        patch('platform.machine', return_value='x86_64'), \
                        patch('platform.libc_ver', return_value=(libc, '')):
                    self.assertEqual(installed_binary('4.14.0'), binary)

    def test_timeout_kills_descendant_even_if_it_ignores_term(self):
        from aiobs_collector.tokscale_runtime import run_command
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / 'child.pid'
            child = 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'
            parent = ('import subprocess,sys,time; from pathlib import Path; '
                      f'p=subprocess.Popen([sys.executable,"-c",{child!r}]); '
                      f'Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(60)')
            started = time.monotonic()
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    run_command([sys.executable, '-c', parent], timeout=1)
                self.assertLess(time.monotonic() - started, 8)
                pid = int(pidfile.read_text())
                for _ in range(40):
                    status = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
                    if status.returncode or status.stdout.strip().startswith('Z'):
                        break
                    time.sleep(.05)
                else:
                    self.fail('timeout left the descendant running')
            finally:
                if pidfile.exists():
                    try:
                        os.kill(int(pidfile.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_runner_preserves_output_and_exit_failure(self):
        from aiobs_collector.tokscale_runtime import run_command
        self.assertEqual(run_command([sys.executable, '-c', 'print("ok")']).stdout, 'ok\n')
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            run_command([sys.executable, '-c', 'import sys; print("bad",file=sys.stderr); sys.exit(7)'])
        self.assertEqual(caught.exception.returncode, 7)
        self.assertIn('bad', caught.exception.stderr)
