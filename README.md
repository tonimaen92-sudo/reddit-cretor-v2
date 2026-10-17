# Friend Poster

Friend Poster is a Windows/Python 3.11 desktop automation project with three
Tk applications: Creator, Poster, and Commenter.  The implementation now uses
a standard `src/` package layout while every historical batch file and
`python misc\<tool>.py` command remains available through a thin wrapper.

## Start here

Use the existing launchers exactly as before:

```bat
run_creator.bat
poster.bat
START CREATOR PANEL.bat
```

For development, activate `.venv`, install the checkout, and use the package
entry points:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m friend_poster creator
python -m friend_poster poster
python -m friend_poster commenter
```

Do not run phone, account, posting, proxy, or external-model workflows as a
test.  Use the offline suite described in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Repository map

- `src/friend_poster/` — canonical implementation.
- `misc/` — compatibility wrappers for old commands and flat imports.
- `tests/unit/` — isolated unit tests.
- `tests/regression/` — offline behavioral regression scripts.
- `tests/fixtures/` — immutable test data.
- `docs/` — architecture, commands, runtime data, and troubleshooting.
- `archive/legacy-source/` — retained historical source snapshots.
- `archive/local-envs/` — ignored, quarantined stale virtual environments.

Runtime locations such as `logs/`, `dumps/`, `secrets.json`, `accounts.txt`,
`full-send/`, and `.venv` have not moved.

See [Architecture](docs/ARCHITECTURE.md), [Commands](docs/COMMANDS.md), and
[Runtime data](docs/RUNTIME_DATA.md) before changing behavior.
