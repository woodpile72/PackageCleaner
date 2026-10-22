# Contributing to Package Cleaner

## Development setup

Clone the repository and use the system `/usr/bin/python3` on Linux Mint 22.3 Cinnamon. Requirements are listed in the README. There is no build or pip-install step. If you prefer a virtual environment, create it with `--system-site-packages` so distro bindings remain available; keep it outside source control. Do not replace the system python-apt or PyGObject packages with unrelated pip versions.

Run `./package-cleaner --help` for supported CLI options and `./package-cleaner gui` for the GUI. Keep real cleanup experiments separate from development tests.

## Tests

```sh
/usr/bin/python3 -m unittest discover -s tests -q
PACKAGE_CLEANER_GTK_TESTS=1 /usr/bin/python3 -m unittest discover -s tests -q
```

The second command needs a desktop display. Native tests use mocks for execution. Optional Flatpak-binding tests skip when the system API is unavailable. CI uses Ubuntu 24.04, system Python and an isolated virtual display; it never authorizes real cleanup.

Use temporary preferences, queues and evidence directories. Mock privileged launchers and libflatpak transaction execution. Never add tests that remove installed software, modify APT flags, reset personal preferences, or depend on a particular user's package inventory. Synthetic log fixtures may include deliberately fake secrets to test redaction; do not replace them with private logs.

## Architecture

- `package_cleaner/core/` owns inventory, classification, search, evidence, Keep preferences, selections, provider resolution, transaction safety, approval, execution verification and logs.
- `core/review_service.py` exposes structured discovery, selection and preview operations.
- `core/cleanup.py` coordinates single-use GTK proposals, fresh validation, independent provider approvals and queue reconciliation.
- `core/inventory.py`, `removal.py`, `execution.py`, `privileged.py` and `result_protocol.py` implement the APT path. APT is the dependency resolver; the locked helper checks the exact approved operation set again.
- `core/flatpak_provider.py` and `flatpak_execution.py` own installation/ref identity, aborting previews, exact-operation checks, authorization boundaries and final-state reconciliation.
- `gui.py`, `gui_dialogs.py`, `gui_formatting.py` and `gui_icons.py` present GTK information and collect input. `display.py` contains plain shared formatting.
- `__main__.py`, `presentation.py`, `terminal.py` and `progress.py` implement the terminal frontend. Top-level compatibility modules forward older import paths to the core.

The core must not import GTK or a frontend, print terminal UI, read input, or produce ANSI sequences. It remains usable without GTK. Both frontends consume structured results, domain exceptions and progress/cancellation APIs; do not parse one frontend's rendered output or duplicate business rules. The GUI must remain usable when CLI-specific files are removed.

## Pull requests

Keep changes focused. Describe the problem, resulting behavior, verification and limitations. Add meaningful regression tests; retain existing tests. UI changes should include light/dark and compact-window checks, keyboard/accessibility review and screenshots without personal data. Use themed icons and retain important action labels.

Changes affecting removal require particularly careful review: protected and unselected manual packages, baseline orphans, shared dependencies, stale inventory, changed plans, duplicate approvals, denied authorization, cancellation, partial/unknown outcomes, exact operation sets and cross-provider effects. A queued intention is not approval. A successful process exit is not independently verified completion. Do not weaken checks to make a preview pass.

Do not run real removals while testing a pull request. Any real-system trial requires separate informed authorization, a disposable or backed-up system, and preserved evidence. Report what was actually verified rather than claiming absolute safety.

Do not commit `work/`, generated inventories, runtime logs, private configuration, credentials or unredacted screenshots. Review the complete staged diff, including fixtures. Contributions are made under GPL-3.0-only; retain third-party notices and document any new asset's provenance and license.

## Building the Debian installer

`/usr/bin/python3 packaging/build_deb.py` builds `dist/package-cleaner_0.1.1_all.deb` using the system `dpkg-deb`; no root privileges, network access or installation is involved. The builder copies an explicit set of source/data files, writes root ownership to the archive, and includes no maintainer scripts. `SOURCE_DATE_EPOCH` can override the timestamp; by default it uses the latest Git commit time. With unchanged input and epoch, repeated builds are identical.

Run `lintian dist/package-cleaner_0.1.1_all.deb` if lintian is available, and `dpkg-deb --contents` / `--info` to inspect it. Tests build and extract the archive in temporary directories, never install it. Real installation is a separate explicitly authorized action. Release checksums cover the uploaded `.deb`; GitHub supplies source archives for the tag.

Generate the public screenshot with `/usr/bin/python3 tools/demo_screenshot.py` in a GTK session. It uses fabricated inventory and blocks execution, never host discovery. When building a source archive without Git history, timestamps fall back to the newest packaging changelog date. Flatpak bindings are recommended, not mandatory; missing Flatpak support leaves APT discovery available.

`tools/check_deb_isolated.py dist/package-cleaner_0.1.1_all.deb` is an opt-in local packaging check requiring user namespaces and bubblewrap. It never modifies the host package database. It uses `--force-depends` in an otherwise empty temporary dpkg root, then read-only host runtime mounts; dependency metadata must also be checked with APT simulation. Its live preview targets an already installed Package Cleaner. It is not part of CI and is not a substitute for a fresh Mint VM trial.

To check an upgrade in the same disposable environment, add
`--upgrade-from dist/package-cleaner_0.1.0_all.deb`. This verifies the old version
was installed, upgrades to the new archive, checks every installed file and
preserved synthetic preferences, then removes only the temporary installation.
