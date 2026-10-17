"""Stable entry point for the Creator application."""

from friend_poster.apps._entrypoint import expose

expose("friend_poster.apps.creator_controller", __name__)
