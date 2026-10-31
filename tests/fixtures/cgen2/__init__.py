# Copyright (c) 2026 Shawn Stricker
"""C Gen 2 rigs for the display board's timed behavior."""

from .cgen2_fixtures import (
    authorized_client,
    backup_plan,
    backup_tail,
    command,
    next_0421,
    next_0490,
    pushed,
    socket_and_ports,
    tou_on_the_cloud,
)
from .cgen2_live import (
    A1783_TABLE,
    INPUT_POWERS,
    OUTPUT_POWERS,
    a1783_live,
    a1783_real,
    counters,
    named,
    poll,
    real_table,
    run,
)


__all__ = [
    "A1783_TABLE",
    "INPUT_POWERS",
    "OUTPUT_POWERS",
    "a1783_live",
    "a1783_real",
    "authorized_client",
    "backup_plan",
    "backup_tail",
    "command",
    "counters",
    "named",
    "next_0421",
    "next_0490",
    "poll",
    "pushed",
    "real_table",
    "run",
    "socket_and_ports",
    "tou_on_the_cloud",
]
