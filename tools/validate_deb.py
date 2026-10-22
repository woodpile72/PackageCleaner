#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Validate the exact release archive without installing anything on the host."""
import argparse
import gzip
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from package_cleaner import __version__


def validate_contents(deb):
    def field(name):
        return subprocess.check_output(['dpkg-deb', '-f', str(deb), name], text=True).strip()
    assert field('Package') == 'package-cleaner', 'Unexpected package name'
    assert field('Version') == __version__, 'Debian version differs from __version__'
    assert field('Architecture') == 'all', 'Unexpected architecture'
    archive = subprocess.check_output(['dpkg-deb', '--fsys-tarfile', str(deb)])
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        files = {}
        for member in tar:
            name = member.name.removeprefix('./').rstrip('/')
            assert member.uid == member.gid == 0, f'Non-root archive ownership: {name}'
            assert member.isdir() or member.isfile(), f'Unexpected archive entry: {name}'
            expected_mode = 0o755 if member.isdir() or name in ('usr/bin/package-cleaner', 'usr/bin/package-cleaner-gui') else 0o644
            assert member.mode == expected_mode, f'Incorrect mode: {name}: {member.mode:o}'
            if member.isfile():
                files[name] = tar.extractfile(member).read()
    expected = {str(Path('usr/share/package-cleaner') / p.relative_to(ROOT)) for p in (ROOT/'package_cleaner').rglob('*.py')}
    expected.update({
        'usr/bin/package-cleaner', 'usr/bin/package-cleaner-gui',
        'usr/share/package-cleaner/data/package-cleaner.svg',
        'usr/share/applications/org.packagecleaner.Review.desktop',
        'usr/share/icons/hicolor/scalable/apps/package-cleaner.svg',
        'usr/share/metainfo/org.packagecleaner.Review.metainfo.xml',
        'usr/share/doc/package-cleaner/copyright', 'usr/share/doc/package-cleaner/README.md',
        'usr/share/doc/package-cleaner/CC0-1.0.txt', 'usr/share/doc/package-cleaner/changelog.gz',
        'usr/share/man/man1/package-cleaner.1.gz', 'usr/share/man/man1/package-cleaner-gui.1.gz',
    })
    assert set(files) == expected, f'Unexpected/missing files: {set(files) ^ expected}'
    for p in (ROOT/'package_cleaner').rglob('*.py'):
        assert files[str(Path('usr/share/package-cleaner')/p.relative_to(ROOT))] == p.read_bytes(), f'Stale source: {p.name}'
    changelog = gzip.decompress(files['usr/share/doc/package-cleaner/changelog.gz']).decode()
    assert changelog.startswith(f'package-cleaner ({__version__})'), 'Changelog version mismatch'
    for name in ('package-cleaner', 'package-cleaner-gui'):
        manual = gzip.decompress(files[f'usr/share/man/man1/{name}.1.gz']).decode()
        assert __version__ in manual, f'Manual version mismatch: {name}'
    metadata = files['usr/share/metainfo/org.packagecleaner.Review.metainfo.xml']
    assert ET.fromstring(metadata).find('releases/release').get('version') == __version__, 'AppStream version mismatch'
    with tempfile.TemporaryDirectory() as tmp:
        control = Path(tmp)/'control'
        subprocess.run(['dpkg-deb','-e',str(deb),str(control)],check=True)
        assert {p.name for p in control.iterdir()} == {'control'}, 'Unexpected control files or maintainer scripts'
        metainfo = Path(tmp)/'org.packagecleaner.Review.metainfo.xml'
        metainfo.write_bytes(metadata)
        subprocess.run(['appstreamcli','validate','--no-net',str(metainfo)],check=True)
    print(f'Verified {len(files)} archive files, version, ownership, modes and AppStream metadata', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('deb', type=Path)
    parser.add_argument('--upgrade-from', type=Path, required=True)
    parser.add_argument('--privileged-isolation', action='store_true', help='Use sudo only for temporary namespace lifecycle checks on restricted CI runners')
    args = parser.parse_args()
    deb = args.deb.resolve()
    validate_contents(deb)
    subprocess.run(['lintian','--fail-on','error,warning',str(deb)],check=True)
    # Build a second archive using the same source and epoch; compare exact bytes.
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([sys.executable,str(ROOT/'packaging/build_deb.py'),'--output',tmp],check=True)
        assert deb.read_bytes() == (Path(tmp)/deb.name).read_bytes(), 'Build is not reproducible'
    checksum = deb.parent/'SHA256SUMS'
    checksum.write_text(hashlib.sha256(deb.read_bytes()).hexdigest()+'  '+deb.name+'\n')
    subprocess.run(['sha256sum','--check',checksum.name],cwd=deb.parent,check=True)
    isolated = (['sudo','--'] if args.privileged_isolation else []) + [sys.executable,str(ROOT/'tools/check_deb_isolated.py'),str(deb)]
    subprocess.run(isolated,check=True)
    subprocess.run(isolated+['--upgrade-from',str(args.upgrade_from.resolve())],check=True)
    print('Release artifact validation passed; checksum generated and verified.', flush=True)


if __name__ == '__main__':
    main()
