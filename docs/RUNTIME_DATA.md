# Runtime data

Runtime and private data deliberately remain at the project root so existing
launchers and operational state continue to work.

| Path | Purpose | Git policy |
| --- | --- | --- |
| `.venv/` | Active Python 3.11 launcher environment | ignored |
| `secrets.json` | API and device credentials | ignored |
| `accounts.txt` | Account records | ignored |
| `Birthdays.txt.txt` | Private birthday data | ignored |
| `ip_history.txt` | Network transition history | ignored |
| `logs/` | Checkpoints, queues, app logs, settings | ignored |
| `dumps/` | UI trees and screenshots | ignored |
| `full-send/` | Live media and scraper assets | selected private areas ignored |
| `posts/`, `used/` | Posting input/archive state | ignored |

Never print or copy credential/runtime contents into issues, commits, test
fixtures, or documentation. Tests that need stable data belong in
`tests/fixtures/` and must be reviewed for private information first.

Stale copied environments are quarantined under ignored
`archive/local-envs/`; the active `.venv` is not moved. Historical source
snapshots are retained under `archive/legacy-source/`.
