# Copyright (c) 2026 Shawn Stricker
"""Minimal SolixBLE source trees for the class-import tests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from tools.import_solixble import main


if TYPE_CHECKING:
    from pathlib import Path

#: The base checkout: one plain and one encrypted device, by path.
BASE = {
    "SolixBLE/const.py": 'CMD_SHARED = "4300"\n',
    "SolixBLE/device.py": (
        "class SolixBLEDevice:\n"
        '    _TELEMETRY_COMMANDS: tuple[str, ...] = ("c402", "4300", "c405")\n'
        "    def _record(self, key):\n"
        "        return b''\n"
    ),
    "SolixBLE/prime_device.py": (
        "from .device import SolixBLEDevice\n"
        "class PrimeDevice(SolixBLEDevice):\n"
        "    _EXPECTED_TELEMETRY_LENGTH: int\n"
    ),
    "SolixBLE/devices/station.py": (
        'CMD_GET_STATUS = "4040"\n'
        'CMD_RESPONSE_GET_STATUS = "c840"\n'
        'CMD_AC_OUTPUT = "404a"\n'
        "CMD_COMPUTED = int()\n"
        'FIRST = SECOND = "x"\n'
        "settings.value = 1\n"
        "settings.typed: int = 2\n"
        "PARAMETERS_ON = {\n"
        '    "a1": {"value": "21"},\n'
        '    "a2": {"type": 1, "value": lambda on: 1 if on else 0},\n'
        '    "a3": {"type": 1, "value": lambda port: port - 1},\n'
        '    "a4": {"type": 4, "value": lambda data: bytes(data)},\n'
        '    "a5": {"type": 1, "value": lambda flag: not flag},\n'
        '    "a6": {"type": 1, "value": lambda flag: -flag},\n'
        "    7: {},\n"
        '    "a7": 1,\n'
        "}\n"
        "class Station(SolixBLEDevice):\n"
        "    _TELEMETRY_COMMANDS = (LOOKED_UP,)\n"
        "    @property\n"
        "    def battery(self):\n"
        '        return self._parse_int("c1", begin=1)\n'
        "    @property\n"
        "    def serial(self):\n"
        '        return self._parse_string("a2", 3, 20)\n'
        "    @property\n"
        "    def temperature(self):\n"
        '        return self._parse_int("c2", 1, 3, True)\n'
        "    @property\n"
        "    def nothing(self):\n"
        "        return 0\n"
        "    async def turn_ac_on(self):\n"
        "        await self._send_command(\n"
        "            cmd=CMD_AC_OUTPUT, parameters=PARAMETERS_ON,\n"
        "            on=True, port=2, flag=1,\n"
        "        )\n"
        "    async def get_status_update(self):\n"
        "        await self._send_command(\n"
        '            cmd=CMD_GET_STATUS, parameters={"a1": {"value": "21"}}\n'
        "        )\n"
        "    async def raw(self):\n"
        "        await self._send_command(cmd=UNKNOWN, parameters=UNDEFINED)\n"
        "class StationPlus(Station):\n"
        "    async def turn_ac_on(self):\n"
        "        pass\n"
        "class StationGen2(Station):\n"
        "    _DEFAULT_ENCRYPTED_NEGOTIATION: bool = True\n"
    ),
    "SolixBLE/devices/charger.py": (
        'CMD_SUBSCRIBE = "4200"\n'
        'CMD_STREAM = "4303"\n'
        "class Charger(PrimeDevice):\n"
        "    _TELEMETRY_COMMANDS = (CMD_STREAM)\n"
        '    _SNAPSHOT_COMMANDS = ("4a00",)\n'
        "    def _port(self, key, begin, end=4):\n"
        "        return self._parse_int(key, begin=begin, end=end)\n"
        "    def _record_int(self, key, begin, end):\n"
        "        record = self._record(key)\n"
        "        return int.from_bytes(record[begin:end], 'little')\n"
        "    def _whole(self, key):\n"
        "        record = self._record(key)\n"
        "        return record\n"
        "    @property\n"
        "    def port_power(self):\n"
        '        return self._port("a2", 2)\n'
        "    @property\n"
        "    def version(self):\n"
        '        return self._record_int("a2", begin=1, end=3)\n'
        "    @property\n"
        "    def schedule(self):\n"
        '        return parse(self._record("aa"))\n'
        "    @property\n"
        "    def timer(self):\n"
        '        return self._whole("ab")\n'
        "class Helper:\n"
        "    pass\n"
        "class Mode(Enum):\n"
        "    ON = 1\n"
    ),
}

#: A later checkout: a new class, and a copy of one the base already has.
EXTRA = BASE | {
    "SolixBLE/devices/legacy.py": (
        "class Legacy(SolixBLEDevice):\n"
        "    @property\n"
        "    def level(self):\n"
        '        return self._parse_int("a1")\n'
    ),
    "SolixBLE/devices/station.py": ("class Station(PrimeDevice):\n    pass\n"),
}


def write_solixble(root: Path, sources: dict[str, str] = BASE) -> Path:
    """Write a source tree under ``root``; return the checkout root."""
    for name, source in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return root


def stream_read(
    parse: str,
    begin: int | None,
    end: int | None,
    tag: str = "a2",
    *,
    signed: bool = False,
) -> dict[str, object]:
    """Return a property's stream read as the class import records it."""
    return {
        "source": "stream",
        "parse": parse,
        "tag": tag,
        "begin": begin,
        "end": end,
        "signed": signed,
    }


def snapshot_read(tag: str, begin: int | None, end: int | None) -> dict[str, object]:
    """Return a property's snapshot read: a slice, or the whole record."""
    parse = "record" if begin is None and end is None else "int"
    return {
        "source": "snapshot",
        "parse": parse,
        "tag": tag,
        "begin": begin,
        "end": end,
        "signed": False,
    }


def run_import(output: Path, *roots: str) -> dict[str, Any]:
    """Import ``roots`` (each ``path`` or ``path=label``) to ``output``; return it."""
    if main([*(f"--solixble={root}" for root in roots), "--output", str(output)]):
        msg = "import_solixble failed"
        raise RuntimeError(msg)
    data: dict[str, Any] = json.loads(output.read_text())
    return data
