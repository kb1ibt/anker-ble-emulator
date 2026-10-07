# Copyright (c) 2026 Shawn Stricker
"""A stand-in client library for the ``EmulatedConnection`` tests."""

from .consumer import CONSUMER_TARGET, connect, ignore_disconnect


__all__ = ["CONSUMER_TARGET", "connect", "ignore_disconnect"]
