# Copyright (c) 2026 Shawn Stricker
"""Reusable construct validators for a device's command fields.

A validator's *shape* (accept one of a set) is generic; a
device supplies its own accepted values when it builds one, directly in its
own module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from construct import Validator


if TYPE_CHECKING:
    from construct import Construct


class OptionsValidator(Validator):  # type: ignore[type-arg]  # construct-typing's Validator is generic only in its stub; the runtime class isn't subscriptable
    """Accept only one of the given option values."""

    def __init__(self, subcon: Construct[int, int], *options: int) -> None:
        """Wrap ``subcon``, accepting only a value in ``options``."""
        super().__init__(subcon)
        self.options = options

    def _validate(self, obj: int, context: object, path: object) -> bool:  # noqa: ARG002  # construct's own Validator signature
        return obj in self.options
