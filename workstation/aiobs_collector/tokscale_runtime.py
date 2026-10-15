"""Offline, pinned native tokscale execution with bounded process-group cleanup."""
import json
import os
from pathlib import Path
import platform
import signal
import subprocess


def installed_binary(version: str) -> Path:
    """Resolve only an already-installed matching native npm package.

    A bounded npm-cache lookup avoids registry access and the npm/node wrappers.
    Missing versions fail closed; installation is an explicit operator action.
    """
    system = platform.system().lower()
    machine = {'aarch64': 'arm64', 'x86_64': 'x64'}.get(platform.machine(), platform.machine())
    package_name = f'@tokscale/cli-{system}-{machine}'
    if system == 'linux':
        libc = platform.libc_ver()[0]
        musl = libc == 'musl' or (not libc and any(Path('/lib').glob('ld-musl-*.so.1')))
        package_name += '-musl' if musl else '-gnu'
    cache = Path(os.environ.get('npm_config_cache') or Path.home() / '.npm').expanduser()
    for manifest in sorted((cache / '_npx').glob(f'*/node_modules/{package_name}/package.json')):
        try:
            metadata = json.loads(manifest.read_text())
        except (OSError, ValueError):
            continue
        binary = manifest.parent / 'bin' / 'tokscale'
        if (metadata.get('name') == package_name and metadata.get('version') == version
                and binary.is_file() and os.access(binary, os.X_OK)):
            return binary
    raise RuntimeError(
        f'No installed {package_name}@{version} binary in {cache}. '
        f'Install the pin once with `npx -y tokscale@{version} --version`; '
        'the collector never downloads packages.'
    )


def run_command(command: list[str], timeout: float = 300) -> subprocess.CompletedProcess:
    """Own the child's session so timeout cannot leave wrapper descendants alive."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # A process in uninterruptible kernel I/O may not reap immediately.
        # Never let cleanup turn a bounded lane timeout into an endless wait.
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.stdout.close()
            process.stderr.close()
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def run_tokscale(version: str) -> subprocess.CompletedProcess:
    return run_command([str(installed_binary(version)), 'graph', '--no-spinner'])
