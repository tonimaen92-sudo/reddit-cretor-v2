# UTF-8
"""Paths and constants for the iPhone USB control plane. No passwords here."""

from __future__ import annotations

import os

from friend_poster.core.paths import LEGACY_MISC_DIR, PROJECT_ROOT

MISC_DIR = str(LEGACY_MISC_DIR)
ROOT_DIR = str(PROJECT_ROOT)
DUMP_DIR = os.path.join(ROOT_DIR, "dumps")
LOG_DIR = os.path.join(ROOT_DIR, "logs")
POSTS_DIR = os.path.join(ROOT_DIR, "posts")
USED_DIR = os.path.join(ROOT_DIR, "used")
SECRETS_PATH = os.path.join(ROOT_DIR, "secrets.json")

# Full-send image pipeline lives in full-send/ (moved from repo root).
# Fall back to the old root-level layout so tools keep working mid-move.
FULLSEND_DIR = os.path.join(ROOT_DIR, "full-send")
if not os.path.isdir(FULLSEND_DIR):
    FULLSEND_DIR = ROOT_DIR
SUBREDDITS_DIR = os.path.join(FULLSEND_DIR, "subreddits")
SCRAPER_PICS_DIR = os.path.join(FULLSEND_DIR, "subscraper", "pics")
SCRAPER_PROFILE_DIR = os.path.join(FULLSEND_DIR, "subscraper", "chrome_bot_profile")

SSH_USER = "root"
SSH_USERS = ("mobile", "root")
SSH_LOCAL_HOST = "127.0.0.1"
SSH_LOCAL_PORT = 2222
SSH_REMOTE_PORT = 22
SSH_REMOTE_PORT_ALT = 44
WDA_LOCAL_PORT = 8100
WDA_REMOTE_PORT = 8100
XXT_LOCAL_PORT = 46952
XXT_REMOTE_PORT = 46952
XXT_SCRIPTS = "/var/mobile/Media/1ferver/lua/scripts"
XXT_LOG = "/var/mobile/Media/1ferver/log"
XXT_BUNDLES = (
    "ch.xxtou.XXTouchElite",
    "ch.xxtou.xxtouchelite",
    "ch.xxtou.XXTExplorer",
    "com.xxtouch.xxtouch",
)

USBMUX_PORT = 27015
REDDIT_BUNDLE = "com.reddit.Reddit"
SETTINGS_BUNDLE = "com.apple.Preferences"
SPRINGBOARD_BUNDLE = "com.apple.springboard"
CRANE_SETTINGS_URL = "prefs:root=Crane"
CRANE_APP_BUNDLE = "com.opa334.CraneApplication"
GHOST_BUNDLE = "com.sgwc.Ghost"
SHADOW_SETTINGS_URL = "prefs:root=Shadow"

# Opens the Hide My Email list. After this, taps come from a live dump.
HME_SETTINGS_URL = (
    "prefs:root=APPLE_ACCOUNT&path=ICLOUD_SERVICE/PRIVATE_EMAIL_MANAGE"
)

JB_PATH = (
    "/var/jb/usr/bin:/var/jb/bin:/var/jb/sbin:"
    "/var/jb/usr/local/xxtouch/bin:/usr/local/xxtouch/bin:"
    "/cores/binpack/usr/bin:/cores/binpack/bin:"
    "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
)

GHOST_APP = "/var/jb/Applications/Ghost.app"
GHOST_DYLIB = "/var/jb/Library/MobileSubstrate/DynamicLibraries/Ghost.dylib"
GHOST_DEB_ON_PC = r"YOUR_GHOST_DEB_PATH_HERE"

CREATE_LABELS = (
    "+ Create New Address",
    "+ Create new address",
    "Create New Address",
    "Create new address",
    "Create Address",
    "Create address",
    "Create New Email Address",
    "Add Email Address",
    "Create Email Address",
)
CONTINUE_LABELS = (
    "Continue",
    "Use This Email Address",
    "Use this email address",
    "Use This Address",
    "Use this address",
    "Next",
    "Save",
    "Done",
    "OK",
)
FORBIDDEN_HME_LABELS = (
    "sign in with apple",
    "delete address",
    "remove address",
    "turn off hide my email",
    "stop using hide my email",
    "manage settings",
    "home",
    "use different address",
    "use a different address",
)
