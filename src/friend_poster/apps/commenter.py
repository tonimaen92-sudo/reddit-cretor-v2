"""Stable entry point for the Commenter application."""

from friend_poster.apps._entrypoint import expose

expose("friend_poster.apps.commenter_controller", __name__)
