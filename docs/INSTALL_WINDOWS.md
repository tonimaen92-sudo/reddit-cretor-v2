# Windows installation

## Fast setup

1. Download or clone the repository.
2. Extract it if it was downloaded as a ZIP.
3. Connect the PC to the internet.
4. Double-click `INSTALL.bat`.
5. When setup succeeds, fill in the newly created local files:
   - `secrets.json`
   - `accounts.txt`
   - `Birthdays.txt.txt`
6. Double-click `START CREATOR PANEL.bat`.

The installer:

- detects Python 3.11;
- installs Python 3.11 with `winget` for the current user when it is missing;
- creates a local `.venv`;
- upgrades `pip`, `setuptools`, and `wheel`;
- installs every package from `requirements.txt` through the editable project install;
- creates safe local runtime files without overwriting existing private data;
- verifies the required Python imports;
- checks whether Apple Mobile Device support is present.

## What cannot be copied from another PC

The repository intentionally does not contain credentials, accounts, API keys,
logs, dumps, or an existing virtual environment. Each user must provide their
own values. The installer creates `secrets.json` from
`secrets.TEMPLATE.json`, but placeholder values must be replaced before any
live workflow is started.

The phone must also be prepared separately. PC setup cannot install or copy an
iPhone jailbreak, Reddit login, Ghost/Crane containers, XXTouch, or
WebDriverAgent. Connect and unlock the iPhone, tap **Trust**, and install Apple
Devices from Microsoft Store (or desktop iTunes from Apple) if Windows does not
detect Apple Mobile Device support.

## Re-running setup

`INSTALL.bat` is safe to run again. It keeps existing private files and reuses a
working Python 3.11 `.venv`. If `.venv` uses the wrong Python version, setup
moves it to a timestamped backup before creating a replacement.
