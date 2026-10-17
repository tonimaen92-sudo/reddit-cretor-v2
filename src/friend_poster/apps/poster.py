"""Stable entry point for the Poster application."""

from friend_poster.apps._entrypoint import expose

expose("friend_poster.apps.poster_controller", __name__)
