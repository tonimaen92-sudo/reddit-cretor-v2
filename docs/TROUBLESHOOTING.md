# Troubleshooting

## A launcher says Python or a dependency is missing

Confirm `.venv\Scripts\python.exe` exists, then run:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
```

Do not substitute a quarantined environment from `archive/local-envs/`.

## A canonical module cannot be imported

Install the checkout editable, or temporarily set `PYTHONPATH` to the absolute
`src` directory. Legacy `python misc\...` commands bootstrap this path
themselves.

## A regression reports a missing dump

Check [BASELINE_TESTS.md](BASELINE_TESTS.md). Some historical regressions read
live `dumps/` files and were already incomplete at the baseline. Do not fetch,
generate, or overwrite live dumps merely to make an organization-only change
green.

## A GUI says another copy is running

Use the existing window. Creator and Poster intentionally prevent two
processes from controlling one phone.

## Runtime data appears missing

All live paths remain rooted at the checkout. Verify you launched with the
project as the working directory and that OneDrive did not relocate or
placeholder-only a private runtime file.
