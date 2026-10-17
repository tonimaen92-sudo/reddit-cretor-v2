# Architecture

## Dependency direction

The intended dependency direction is:

```text
apps / cli
    -> workflows
        -> reddit / device / services
            -> core
```

`core` owns stable paths, configuration, state models, validation, and
persistence primitives. `device` owns iPhone, SSH, UI-tree, Crane, Ghost, and
iCloud integration. `reddit` owns app-specific navigation and actions.
`services` owns IP, email, media, title, scraping, and model clients.
`workflows` coordinates multi-stage operations. `apps` owns GUI entry points,
controllers, validation, and UI-specific persistence.

## GUI boundaries

The public modules `friend_poster.apps.creator`, `.poster`, and `.commenter`
are stable entry points. They expose their corresponding controller modules so
existing public symbols and monkeypatch-based regressions continue to work.
The large controller classes retain their exact widget construction and event
ordering. Low-risk settings persistence and Creator input validation have been
extracted into dedicated modules; operational workflow logic lives in the
domain packages.

This conservative split is deliberate: changing Tk construction order or
moving methods between classes would risk visible behavior and callback
binding changes.

## Compatibility layer

Every old `misc/*.py` path is a wrapper. On execution it runs the canonical
module as `__main__`; on legacy flat import it returns the canonical module
object. Consequently arguments, exit codes, and public symbols come from one
implementation rather than a copied shim.

All filesystem roots come from `friend_poster.core.paths`. Package depth must
never determine a runtime path.
