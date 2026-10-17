# Command inventory

## Supported batch launchers

These root filenames and their argument behavior are preserved:

- `poster.bat`
- `run_creator.bat`
- `run.creator.bat`
- `START CREATOR PANEL.bat`
- `run_qa_scroll.bat`

## Legacy Python commands

Every Python file directly under `misc/` remains executable with its original
name and arguments. Examples:

```powershell
python misc\reddit_open.py ...
python misc\creator_prompt_regress.py
python misc\comment_precache.py --help
```

The wrappers intentionally contain no business logic. Their canonical targets
are grouped under `src/friend_poster/{apps,core,device,reddit,services,workflows,cli}`.

## Canonical commands

After `python -m pip install -e .`:

```powershell
python -m friend_poster creator
python -m friend_poster poster
python -m friend_poster commenter
python -m friend_poster.reddit.reddit_open ...
python -m friend_poster.services.comment_precache --help
```

Use the legacy command when comparing behavior with an older runbook; use the
canonical command in new development documentation.
