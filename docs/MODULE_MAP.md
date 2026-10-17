# Module ownership map

| Package | Owner responsibility | Representative modules |
| --- | --- | --- |
| `apps` | Tk entry points, controllers, UI persistence and validation | `creator`, `poster`, `commenter`, `*_controller`, `*_persistence` |
| `core` | Configuration, project paths, slots, durable state, shared validation | `config`, `paths`, `slots`, `bulk_creation`, `fieldvalue` |
| `device` | Phone transport and device applications | `ssh_device`, `ui_tree`, `crane_*`, `ghost_*`, `icloud_*` |
| `reddit` | Reddit navigation and account/post/comment actions | `reddit_open`, `reddit_post`, `reddit_create_day1`, `reddit_restore` |
| `services` | External/model/media/IP/data services | `comment_llm`, `comment_precache`, `ip_change`, `nsfw_*`, `source_bank` |
| `workflows` | Cross-domain orchestration and recovery | `creator_watchdog`, `mint_slot_email`, `replace_slot_email` |
| `cli` | Standalone diagnostics and maintenance | `detect7`, `eyes_bench`, `qa_scroll_test` |

Tests mirror risk rather than package names: isolated behaviors live in
`tests/unit`, end-to-end offline gates in `tests/regression`, and immutable
inputs in `tests/fixtures`.
