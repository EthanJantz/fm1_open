#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only WITH GPL-3.0-linking-exception
"""Build the FM1 browser preview or the complete workbench page.

Portable port of tools/Build-Web.ps1: runs on Linux, macOS and Windows with
only Python 3 and a WASI SDK 27 installation (downloaded on first use).
"""
import argparse
import base64
import hashlib
import html
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WASI_RELEASE = 'https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-27/'
# Archive digests. The Windows digest is the one pinned by the vendor's
# Build-Web.ps1; the Linux digest was recorded from the GitHub release.
WASI_ARCHIVES = {
    ('Windows', 'AMD64'): ('wasi-sdk-27.0-x86_64-windows', '4a576c13125c91996d8cc3b70b7ea0612c2044598d2795c9be100d15f874adf6'),
    ('Linux', 'x86_64'): ('wasi-sdk-27.0-x86_64-linux', 'b7d4d944c88503e4f21d84af07ac293e3440b1b6210bfd7fe78e0afd92c23bc2'),
}
EXE = '.exe' if os.name == 'nt' else ''


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def run(program, arguments):
    result = subprocess.run([str(program)] + [str(a) for a in arguments])
    if result.returncode != 0:
        raise SystemExit(f'Command failed: {program} ({result.returncode})')


def relative(path):
    return path.relative_to(ROOT).as_posix()


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8', newline='\n')


def locate_wasi_sdk(explicit):
    if explicit:
        return Path(explicit).resolve()
    key = (platform.system(), platform.machine())
    if key not in WASI_ARCHIVES:
        raise SystemExit('No known WASI SDK 27 archive for this platform. Set FM1_WASI_SDK to an extracted WASI SDK 27 directory.')
    name, expected = WASI_ARCHIVES[key]
    cache = ROOT / 'build' / 'tools'
    sdk = cache / name
    if not (sdk / 'bin' / ('clang' + EXE)).exists():
        cache.mkdir(parents=True, exist_ok=True)
        archive = cache / (name + '.tar.gz')
        if not archive.exists() or sha256_file(archive) != expected:
            print(f'Downloading WASI SDK 27 ({name}). Set FM1_WASI_SDK to reuse an extracted compiler.')
            with urllib.request.urlopen(WASI_RELEASE + name + '.tar.gz') as response, open(archive, 'wb') as out:
                for block in iter(lambda: response.read(1 << 20), b''):
                    out.write(block)
        if sha256_file(archive) != expected:
            raise SystemExit('WASI SDK archive SHA-256 mismatch.')
        with tarfile.open(archive, 'r:gz') as tar:
            tar.extractall(cache, filter='data')
    return sdk.resolve()


def build_simulator(wasi_sdk):
    out = ROOT / 'build' / 'web'
    out.mkdir(parents=True, exist_ok=True)
    if not (ROOT / 'web' / 'engine' / 'Simulator.c').exists():
        raise SystemExit('Missing browser sources. Download the complete source tree.')
    inputs = {}
    for folder in ('app', 'web', 'third_party/synth'):
        for file in (ROOT / folder).rglob('*'):
            if not file.is_file():
                continue
            name = relative(file)
            if not name.startswith('app/board/') and file.suffix in ('.c', '.h', '.inc'):
                inputs[name] = sha256_file(file)
    for name in ['tools/build_web.py'] + sorted('sdk/include/' + f.name for f in (ROOT / 'sdk' / 'include').glob('*.h')):
        inputs[name] = sha256_file(ROOT / name)
    names = sorted(inputs)
    source_hash = sha256_text('\n'.join(f'{n}:{inputs[n]}' for n in names))
    wasm = out / 'simulator.wasm'
    metadata = out / 'simulator.json'

    compiler = wasi_sdk / 'bin' / ('clang' + EXE)
    sysroot = wasi_sdk / 'share' / 'wasi-sysroot'
    if not compiler.exists() or not sysroot.exists():
        raise SystemExit('Set FM1_WASI_SDK to an extracted WASI SDK 27 directory.')
    # Switching SDKs or changing their files must invalidate the build cache.
    stamp_items = [str(wasi_sdk)]
    for file in sorted(p for p in wasi_sdk.rglob('*') if p.is_file()):
        stat = file.stat()
        stamp_items.append(f'{file}:{stat.st_size}:{stat.st_mtime_ns}')
    tool_stamp = sha256_text('\n'.join(stamp_items))
    if wasm.exists() and metadata.exists():
        try:
            cached = json.loads(metadata.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            cached = None
        if cached and cached.get('schema') == 2 and cached.get('toolchain_stamp') == tool_stamp and \
                cached.get('source_sha256') == source_hash and cached.get('wasm_sha256') == sha256_file(wasm):
            return {'file': wasm, 'source_sha256': source_hash, 'cache_hit': True}
    for file in (wasm, metadata):
        if file.exists():
            file.unlink()
    options = [f'--sysroot={sysroot}',
               '-std=c11', '-O2', '-fwrapv', '-fno-fast-math', '-ffp-contract=off',
               '-DFM1_HOST_TEST', '-DFM1_OPEN_WEB', '-Wall', '-Wextra', '-Werror',
               '-Wno-unused-function', '-Wno-array-parameter', '-mexec-model=reactor']
    for name in ('web/engine', 'web/synth', 'third_party/synth', 'sdk/include', 'app/synth', 'app'):
        options.append('-I' + str(ROOT / name))
    options += [str(ROOT / n) for n in names if n.endswith('.c') and not n.startswith('sdk/')]
    options += ['-Wl,--strip-all', '-Wl,-z,stack-size=131072', '-Wl,--initial-memory=4194304',
                '-Wl,--max-memory=33554432', '-o', str(wasm)]
    print('Compile the browser application')
    try:
        run(compiler, options)
        record = {'schema': 2, 'source_sha256': source_hash, 'toolchain_stamp': tool_stamp, 'wasm_sha256': sha256_file(wasm)}
        write_text(metadata, json.dumps(record, indent=2))
    except BaseException:
        for file in (wasm, metadata):
            if file.exists():
                file.unlink()
        raise
    return {'file': wasm, 'source_sha256': source_hash, 'cache_hit': False}


def validate_source_url(url):
    if not url or not url.strip():
        raise SystemExit('Set FM1_SOURCE_URL or --source-url to the HTTPS URL of the matching source archive.')
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.netloc or '@' in parts.netloc or re.search(r'[\r\n]', url):
        raise SystemExit('FM1_SOURCE_URL must be an HTTPS source URL without credentials.')


def release_version():
    text = (ROOT / 'app' / 'version.h').read_text(encoding='utf-8')
    match = re.search(r'(?m)^#define FM1_RELEASE_VERSION "(V(?:0[1-9]|[1-9][0-9]{1,2}))"\s*$', text)
    if not match:
        raise SystemExit('Set FM1_RELEASE_VERSION in app/version.h.')
    return match.group(1)


def build(wasi_sdk=None, package_json=None, source_url=None):
    """Build the preview (package_json None) or the integrated workbench.

    package_json is the compact JSON text of the firmware package record.
    Returns a report dict for the integrated build, otherwise None.
    """
    output = ROOT / ('build/official-web/FM1-Official-Open-Source.html' if package_json else 'build/preview/index.html')
    # Invalidate old output before source URL validation can stop this build.
    for file in (output, output.parent / 'SHA256SUMS.txt'):
        if file.exists():
            file.unlink()
    validate_source_url(source_url)
    version = release_version()
    notices = ['APP: GPLv3 with the FM1 SDK linking exception; no warranty. SDK and firmware resources have separate '
               f'redistribution terms in THIRD_PARTY.md. Source: {source_url}']
    if package_json:
        package = json.loads(package_json)
        if package.get('schema') != 1 or package.get('release_version') != version:
            raise SystemExit('Firmware and web release versions differ.')
        if not re.fullmatch(r'[0-9a-f]{64}', str(package.get('build_id', ''))):
            raise SystemExit('Firmware source identifier is invalid.')
        notices.append('FM-1 V15 recovery firmware: https://www.m-vave.com/download (PC Firmware / FM-1 V15). '
                       'Distributed by M-VAVE under Apache-2.0. Retain applicable licenses and notices; components '
                       'with separate licenses retain their terms. See THIRD_PARTY.md.')
    compiled = build_simulator(locate_wasi_sdk(wasi_sdk))
    wasm_bytes = compiled['file'].read_bytes()
    simulation = json.dumps({'schema': 1, 'release_version': version, 'sha256': sha256_file(compiled['file']),
                             'source_sha256': compiled['source_sha256'], 'sample_bank': 'synthesized-demo',
                             'data': base64.b64encode(wasm_bytes).decode('ascii')}, separators=(',', ':'))
    web = (ROOT / 'sdk' / 'Update.html.in').read_text(encoding='utf-8')
    sdk = ROOT / 'sdk'
    replacements = {
        '/*FM1_SOURCE_URL*/': html.escape(source_url, quote=True),
        '/*FM1_PAYLOAD*/': 'const FM1_PACKAGE = ' + (package_json if package_json else 'null') + ';',
        '/*FM1_SIMULATOR*/': 'const FM1_SIMULATOR = ' + simulation + ';',
        '/*FM1_I18N*/': (sdk / 'I18n.js').read_text(encoding='utf-8'),
        '/*FM1_SCRIPT*/': (sdk / 'WebUpdate.js').read_text(encoding='utf-8'),
        '/*FM1_SIM_SCRIPT*/': (sdk / 'Simulator.js').read_text(encoding='utf-8'),
        '/*FM1_SAMPLE_SCRIPT*/': (sdk / 'Samples.js').read_text(encoding='utf-8'),
    }
    for name in ('THIRD_PARTY.md', 'LICENSE.md'):
        notice = (ROOT / name).read_text(encoding='utf-8')
        if name == 'THIRD_PARTY.md' and not notice.startswith('# Third-party notices'):
            raise SystemExit('Use the public distribution notices.')
        notices.append(f'\n===== {name} =====\n' + notice)
    replacements['/*FM1_NOTICES*/'] = html.escape('\n'.join(notices), quote=True)
    for key, value in replacements.items():
        web = web.replace(key, value)
    if re.search(r'/\*FM1_[A-Z_]+\*/', web):
        raise SystemExit('Unexpanded HTML placeholder.')
    write_text(output, web)
    if package_json:
        digest = sha256_file(output)
        write_text(output.parent / 'SHA256SUMS.txt', f'{digest}  {output.name}\n')
        print(f'Integrated FM1 workbench: {output}')
        return {'file': str(output), 'sha256': digest, 'simulator_sha256': sha256_file(compiled['file']),
                'simulator_source_sha256': compiled['source_sha256'], 'simulator_bytes': len(wasm_bytes)}
    print('Preview: build/preview/index.html')
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--wasi-sdk', default=os.environ.get('FM1_WASI_SDK') or None,
                        help='extracted WASI SDK 27 directory (default: $FM1_WASI_SDK, else downloaded to build/tools)')
    parser.add_argument('--source-url', default=os.environ.get('FM1_SOURCE_URL'),
                        help='HTTPS URL of this build\'s source archive (default: $FM1_SOURCE_URL)')
    parser.add_argument('--package-json-file', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    package_json = Path(args.package_json_file).read_text(encoding='utf-8') if args.package_json_file else None
    build(args.wasi_sdk, package_json, args.source_url)


if __name__ == '__main__':
    main()
