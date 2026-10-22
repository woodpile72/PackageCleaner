# Changelog

## 0.1.1 — pre-release

- Correct disabled-backend readiness and show the backend's actual reason.
- Preserve unavailable reasons for combined provider previews.
- Add a synthetic GTK demonstration screenshot and contributor templates.
- Extract dialog construction into a presentation-only module.
- Extend Debian packaging with AppStream metadata, optional Flatpak dependencies,
  and source-archive build support. Preserve root-owned code and isolated helper execution.

## 0.1.0 — initial pre-release

- GTK and guided CLI for APT and Flatpak discovery, categories, search and review.
- Keep preferences, removal queues, dependency-aware previews and critical-package protection.
- Explicit approvals, transaction revalidation, final-state verification and audit logs.
- Original icon, desktop integration, and Debian installer.

## Known limitations

Developed for Linux Mint 22.3 Cinnamon. Inactivity clues are imperfect; missing
history is not evidence of non-use. Installed sizes differ from recoverable space.
Removal can run privileged scripts. Providers/installations are independent
transactions with no atomic rollback. Real authentication and execution are not
exercised by CI; automated removal backends are mocked.
