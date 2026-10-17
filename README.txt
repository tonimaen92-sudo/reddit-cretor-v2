FRIEND POSTER PACK
==================
Start with: FRIEND_POSTER_GUIDE.pdf (in docs/)

CURRENT INSTALLATION NOTE
-------------------------
- The PDF and tutorial examples were recorded on an iPhone 7 at 750x1334
  and contain historical slot inventories. This installation uses an
  iPhone X; fixed coordinates and old slot lists are reference examples.
- logs/slots.json is the only authoritative slot state. Live screen dumps
  and the source's device-size scaling take precedence over old coordinates.

1. Copy this folder anywhere (e.g. Desktop).
2. Keep secrets.TEMPLATE.json as placeholders only. Copy it to secrets.json
   at the REPO ROOT next to poster.bat, then fill secrets.json locally.
3. Python 3.11 is required. The batch files create .venv and install
   requirements.txt only when .venv does not already exist.
4. Double-click START CREATOR PANEL.bat for the Creator GUI. You can also use
   poster.bat for the poster GUI or run_creator.bat for the Creator directly.
   Start only the GUI you intend to use; do not run both against one phone.
   The Creator Batch tab accepts aligned birth-date, swipe-count, and 2–7 second
   between-item delay lines. Each item verifies a fresh FalconProxy IP before
   running the full creation pipeline. Paused work remains in
   logs/bulk_creation.json and can be continued with Retry / Resume.
5. Your image pools go in full-send/subreddits/<sub>/.
   Optional title corpora go in full-send/subscraper/pics/<sub>/titles/.
6. logs/accounts.txt, logs/hidemyemail.json, and logs/creator_state.json are
   runtime outputs. Do not pre-create or fabricate them.
7. grok_api_key and xai_api_key use an xAI API key. openrouter_api_key uses
   a separate OpenRouter key. Leave placeholders until you add keys locally.

Nothing in here can post without YOUR iPhone + YOUR logins + YOUR key.
