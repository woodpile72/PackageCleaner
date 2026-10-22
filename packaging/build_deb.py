#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Build an unprivileged, root-owned .deb using only Python and dpkg-deb."""
import argparse
import gzip
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from package_cleaner import __version__


def build(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    epoch = os.environ.get('SOURCE_DATE_EPOCH')
    if epoch is None:
        epoch = subprocess.check_output(['git', 'log', '-1', '--format=%ct'], cwd=ROOT, text=True).strip()
    epoch = int(epoch)
    with tempfile.TemporaryDirectory(prefix='package-cleaner-deb-') as temporary:
        stage = Path(temporary)
        app = stage / 'usr/share/package-cleaner'
        for source in sorted((ROOT / 'package_cleaner').rglob('*.py')):
            target = app / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        (app / 'data').mkdir(parents=True)
        shutil.copyfile(ROOT / 'data/package-cleaner.svg', app / 'data/package-cleaner.svg')
        bindir = stage / 'usr/bin'
        bindir.mkdir(parents=True)
        for name, module, function in [('package-cleaner', '__main__', 'main'), ('package-cleaner-gui', 'gui', 'run')]:
            script = bindir / name
            script.write_text('#!/usr/bin/python3\n# SPDX-License-Identifier: GPL-3.0-only\n'
                              "import sys\nsys.path.insert(0, '/usr/share/package-cleaner')\n"
                              f'from package_cleaner.{module} import {function}\nraise SystemExit({function}())\n')
            script.chmod(0o755)
        for source, destination in [
            ('data/org.packagecleaner.Review.desktop', 'usr/share/applications/org.packagecleaner.Review.desktop'),
            ('data/package-cleaner.svg', 'usr/share/icons/hicolor/scalable/apps/package-cleaner.svg'),
            ('packaging/copyright', 'usr/share/doc/package-cleaner/copyright'),
            ('README.md', 'usr/share/doc/package-cleaner/README.md'),
        ]:
            target = stage / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / source, target)
        doc = stage / 'usr/share/doc/package-cleaner'
        (doc / 'changelog.gz').write_bytes(gzip.compress((ROOT / 'packaging/changelog').read_bytes(), mtime=0))
        man = stage / 'usr/share/man/man1'
        man.mkdir(parents=True)
        for name in ('package-cleaner', 'package-cleaner-gui'):
            (man / (name + '.1.gz')).write_bytes(gzip.compress((ROOT / 'packaging' / (name + '.1')).read_bytes(), mtime=0))
        size = (sum(p.stat().st_size for p in stage.rglob('*') if p.is_file()) + 1023) // 1024
        control = stage / 'DEBIAN'
        control.mkdir()
        (control / 'control').write_text(f'''Package: package-cleaner
Version: {__version__}
Section: admin
Priority: optional
Architecture: all
Maintainer: Erick Woods <17180367+woodpile72@users.noreply.github.com>
Depends: python3 (>= 3.10), python3-apt, python3-gi, gir1.2-gtk-3.0, librsvg2-common, apt, sudo, flatpak, gir1.2-flatpak-1.0
Recommends: ssh-askpass | ksshaskpass | kdesvn
Installed-Size: {size}
Homepage: https://github.com/woodpile72/PackageCleaner
Description: GTK and CLI software cleanup utility for Linux Mint
 Investigate APT and Flatpak applications, review dependency-aware removal
 proposals, and explicitly approve validated cleanup transactions.
 Inactivity clues are imperfect and package removal has inherent risks.
''')
        # No maintainer scripts, services, sudoers changes or setuid binaries.
        for path in stage.rglob('*'):
            if path.is_dir():
                path.chmod(0o755)
            elif path.parent != bindir:
                path.chmod(0o644)
            os.utime(path, (epoch, epoch))
        os.utime(stage, (epoch, epoch))
        target = output / f'package-cleaner_{__version__}_all.deb'
        subprocess.run(['dpkg-deb', '--root-owner-group', '-Zxz', '--build', str(stage), str(target)],
                       check=True, env={**os.environ, 'SOURCE_DATE_EPOCH': str(epoch)})
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=str(ROOT / 'dist'))
    print(build(parser.parse_args().output))
