# Copyright (c) 2026 Shawn Stricker
"""Read SolixBLE's device classes: outers, telemetry, decode positions, commands.

For each device class: its outer (``SolixBLEDevice`` speaks the plain outer,
``PrimeDevice`` the encrypted one), the telemetry and snapshot messages it
listens for, its status request and reply, its subscribe, where each property
decodes from, and each command it sends with its parameter values. The classes
are read as source, never imported; parameter lambdas are evaluated only when
they are plain arithmetic or a conditional. Message types are 12-bit hex.

Several checkouts can be read: the first is the base, and each later one only
adds the classes the earlier ones lack (``--solixble path=label``).

Usage::

    python -m tools.import_solixble --solixble ../SolixBLE-t6-complete-merge
        --solixble export=label --output tools/solixble.json
"""

from __future__ import annotations

import argparse
import ast
import json
import operator
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tools.import_maps import revision


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

#: The base classes and the outer each speaks.
OUTERS = {"SolixBLEDevice": "plain", "PrimeDevice": "encrypted"}
TELEMETRY = "_TELEMETRY_COMMANDS"
SNAPSHOT = "_SNAPSHOT_COMMANDS"
STATUS_REQUEST = "CMD_GET_STATUS"
STATUS_REPLY = "CMD_RESPONSE_GET_STATUS"
SUBSCRIBE = "CMD_SUBSCRIBE"
#: Telemetry decoders: the stream's parse helpers and the snapshot's record.
PARSERS = {"_parse_int": "int", "_parse_string": "string"}
RECORD = "_record"
SEND = "_send_command"
#: How deep property helpers are followed.
HELPER_DEPTH = 4
#: The arithmetic a parameter lambda may use.
OPERATORS: dict[type, Callable[..., object]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
}

Function = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass
class DeviceClass:
    """One class as its source declares it."""

    name: str
    bases: list[str]
    functions: dict[str, Function]
    properties: set[str]
    #: Every module's constants, overridden by this module's own.
    constants: dict[str, object]
    #: This module's own constants.
    own: dict[str, object]
    attributes: dict[str, object] = field(default_factory=dict)


def msgtype(cmd: str) -> str:
    """Return a command's 12-bit message type (``c840`` -> ``840``)."""
    return f"{int(cmd, 16) & 0xFFF:03x}"


def _msgtypes(value: object) -> list[str]:
    """Return the message types of a command, or of a tuple of them."""
    commands = (value,) if isinstance(value, str) else value
    if not isinstance(commands, tuple):
        return []
    return [msgtype(cmd) for cmd in commands if isinstance(cmd, str)]


def _assigned(node: ast.stmt) -> tuple[str, ast.expr] | None:
    """Return ``(name, value)`` of a plain or annotated single-name assignment."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            return target.id, node.value
    if (
        isinstance(node, ast.AnnAssign)
        and node.value is not None
        and isinstance(node.target, ast.Name)
    ):
        return node.target.id, node.value
    return None


def _literal(node: ast.expr | None, names: Mapping[str, object]) -> object:
    """Return a constant, a tuple of them, a known name's value, or None."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.Tuple):
        items = tuple(_literal(item, names) for item in node.elts)
        return None if None in items else items
    return None


def evaluate(node: ast.expr, names: Mapping[str, object]) -> object:
    """Evaluate plain arithmetic and conditionals over ``names``; None otherwise."""
    if isinstance(node, ast.Constant | ast.Name):
        return _literal(node, names)
    if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
        left, right = evaluate(node.left, names), evaluate(node.right, names)
        if isinstance(left, int) and isinstance(right, int):
            return OPERATORS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPERATORS:
        operand = evaluate(node.operand, names)
        if operand is not None:
            return OPERATORS[type(node.op)](operand)
    if isinstance(node, ast.IfExp):
        test = evaluate(node.test, names)
        if test is not None:
            return evaluate(node.body if test else node.orelse, names)
    return None


def read_module(path: Path, shared: Mapping[str, object]) -> list[DeviceClass]:
    """Return the classes a module defines, with its module-level constants."""
    tree = ast.parse(path.read_text())
    constants: dict[str, object] = dict(shared)
    own: dict[str, object] = {}
    for node in tree.body:
        assigned = _assigned(node)
        if assigned is not None:
            name, value = assigned
            own[name] = (
                value if isinstance(value, ast.Dict) else _literal(value, constants)
            )
            constants[name] = own[name]
    classes = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        functions = {
            item.name: item for item in node.body if isinstance(item, Function)
        }
        properties = {
            name
            for name, function in functions.items()
            if any(
                isinstance(d, ast.Name) and d.id == "property"
                for d in function.decorator_list
            )
        }
        cls = DeviceClass(
            node.name,
            [base.id for base in node.bases if isinstance(base, ast.Name)],
            functions,
            properties,
            constants,
            own,
        )
        for item in node.body:
            assigned = _assigned(item)
            if assigned is not None:
                cls.attributes[assigned[0]] = _literal(assigned[1], constants)
        classes.append(cls)
    return classes


def module_constants(paths: list[Path]) -> dict[str, object]:
    """Return every module's plain constants, for names imported across modules."""
    constants: dict[str, object] = {}
    for path in paths:
        for node in ast.parse(path.read_text()).body:
            assigned = _assigned(node)
            if assigned is not None and isinstance(assigned[1], ast.Constant):
                constants[assigned[0]] = assigned[1].value
    return constants


def _self_call(node: ast.AST) -> str | None:
    """Return the method name of a ``self.<name>(...)`` call."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ):
        return node.func.attr
    return None


def _argument(call: ast.Call, name: str, index: int) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return call.args[index] if len(call.args) > index else None


def _read(
    source: str, parse: str, tag: object, begin: object, end: object
) -> dict[str, object]:
    return {"source": source, "parse": parse, "tag": tag, "begin": begin, "end": end}


def decode_reads(
    function: Function,
    names: Mapping[str, object],
    helpers: Mapping[str, Function],
    depth: int = 0,
) -> list[dict[str, object]]:
    """Return where a property decodes from, following ``self`` helpers.

    A stream read is a ``_parse_*`` call; a snapshot read slices a ``_record``
    or takes it whole. ``begin``/``end`` index the value with its type byte.
    """
    reads: list[dict[str, object]] = []
    records: dict[str, object] = {}
    assigned: set[int] = set()
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _self_call(node.value) == RECORD
            and isinstance(node.targets[0], ast.Name)
        ):
            records[node.targets[0].id] = _literal(node.value.args[0], names)
            assigned.add(id(node.value))
    sliced: set[str] = set()
    for node in ast.walk(function):
        name = _self_call(node)
        if isinstance(node, ast.Call) and name in PARSERS:
            reads.append(
                _read(
                    "stream",
                    PARSERS[name],
                    _literal(_argument(node, "key", 0), names),
                    _literal(_argument(node, "begin", 1), names),
                    _literal(_argument(node, "end", 2), names),
                )
            )
        elif isinstance(node, ast.Call) and name == RECORD and id(node) not in assigned:
            reads.append(
                _read("snapshot", "record", _literal(node.args[0], names), None, None)
            )
        elif (
            isinstance(node, ast.Call)
            and name is not None
            and name in helpers
            and depth < HELPER_DEPTH
        ):
            helper = helpers[name]
            reads.extend(
                decode_reads(helper, _bound(helper, node, names), helpers, depth + 1)
            )
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in records
            and isinstance(node.slice, ast.Slice)
        ):
            sliced.add(node.value.id)
            reads.append(
                _read(
                    "snapshot",
                    "int",
                    records[node.value.id],
                    _literal(node.slice.lower, names),
                    _literal(node.slice.upper, names),
                )
            )
    reads.extend(
        _read("snapshot", "record", key, None, None)
        for variable, key in records.items()
        if variable not in sliced
    )
    return reads


def _bound(
    helper: Function, call: ast.Call, names: Mapping[str, object]
) -> dict[str, object]:
    """Return a helper's parameters bound to a call's arguments and its defaults."""
    args = helper.args
    params = [a.arg for a in args.args][1:]
    defaults: dict[str, ast.expr | None] = dict(
        zip(params[len(params) - len(args.defaults) :], args.defaults, strict=True)
    )
    defaults |= {
        a.arg: default
        for a, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
    }
    bound = {}
    for index, param in enumerate([*params, *(a.arg for a in args.kwonlyargs)]):
        given = _argument(call, param, index if index < len(params) else len(call.args))
        bound[param] = _literal(
            given if given is not None else defaults.get(param), names
        )
    return bound


def sent_commands(
    function: Function,
    owner: Mapping[str, object],
    helpers: Mapping[str, tuple[Function, Mapping[str, object]]],
    bound: Mapping[str, object] | None = None,
    depth: int = 0,
) -> list[dict[str, object]]:
    """Return the commands a method sends, through ``self`` helpers.

    Args:
        function: The method.
        owner: The constants of the module that defines it.
        helpers: Every method of the class, with its module's constants.
        bound: The method's arguments, where a caller binds them.
        depth: How many helpers deep this is.

    """
    names = {**owner, **(bound or {})}
    commands: list[dict[str, object]] = []
    for node in ast.walk(function):
        name = _self_call(node)
        if name is None or not isinstance(node, ast.Call):
            continue
        if name == SEND:
            commands.append(_sent(node, names, owner))
        elif name in helpers and depth < HELPER_DEPTH:
            helper, constants = helpers[name]
            commands.extend(
                sent_commands(
                    helper,
                    constants,
                    helpers,
                    _bound(helper, node, names),
                    depth + 1,
                )
            )
    return commands


def _sent(
    node: ast.Call, names: Mapping[str, object], owner: Mapping[str, object]
) -> dict[str, object]:
    """Return one ``_send_command`` call's message type, fields and arguments."""
    cmd = _literal(_argument(node, "cmd", 0), names)
    arguments = {
        keyword.arg: _literal(keyword.value, names)
        for keyword in node.keywords
        if keyword.arg is not None and keyword.arg not in {"cmd", "parameters"}
    }
    parameters = _argument(node, "parameters", 1)
    if isinstance(parameters, ast.Name):
        found = owner.get(parameters.id)
        parameters = found if isinstance(found, ast.Dict) else None
    fields: dict[str, object] = {}
    if isinstance(parameters, ast.Dict):
        for key, spec in zip(parameters.keys, parameters.values, strict=True):
            tag = _literal(key, names)
            if isinstance(tag, str) and isinstance(spec, ast.Dict):
                fields[tag] = _field_value(spec, arguments, names)
    return {
        "cmd": msgtype(cmd) if isinstance(cmd, str) else None,
        "fields": fields,
        "arguments": arguments,
    }


def _field_value(
    spec: ast.Dict,
    arguments: Mapping[str, object],
    constants: Mapping[str, object],
) -> object:
    """Return a parameter field's value: a constant, or its lambda applied."""
    keys = [_literal(key, {}) for key in spec.keys]
    value = dict(zip(keys, spec.values, strict=True)).get("value")
    if isinstance(value, ast.Lambda):
        names = {a.arg: arguments.get(a.arg) for a in value.args.args}
        return evaluate(value.body, names)
    return _literal(value, constants)


def device_facts(
    classes: Mapping[str, DeviceClass], name: str
) -> dict[str, Any] | None:
    """Return one device class's facts; None for a base or unrelated class."""
    if name in OUTERS:
        return None
    chain, outer = [classes[name]], None
    while base := next((b for b in chain[-1].bases if b in classes), None):
        if outer is None and base in OUTERS:
            outer = OUTERS[base]
        chain.append(classes[base])
    if outer is None:
        return None
    functions: dict[str, tuple[Function, Mapping[str, object]]] = {}
    properties: set[str] = set()
    attributes: dict[str, object] = {}
    for cls in reversed(chain):
        functions |= {n: (f, cls.constants) for n, f in cls.functions.items()}
        properties |= cls.properties
        attributes |= {k: v for k, v in cls.attributes.items() if v is not None}
    methods = {n: pair for n, pair in functions.items() if n not in properties}
    helpers = {n: function for n, (function, _) in methods.items()}
    reads = {p: decode_reads(functions[p][0], {}, helpers) for p in sorted(properties)}
    # A private method another one calls is listed under its callers.
    called = {
        callee
        for function, _ in methods.values()
        for node in ast.walk(function)
        if (callee := _self_call(node)) is not None and callee.startswith("_")
    }
    commands = {
        method: list({json.dumps(s, sort_keys=True): s for s in sent}.values())
        for method, (function, constants) in methods.items()
        if method not in called and method != SEND
        if (sent := sent_commands(function, constants, methods))
    }
    own = chain[0].own
    request, reply, subscribe = (
        own.get(STATUS_REQUEST),
        own.get(STATUS_REPLY),
        own.get(SUBSCRIBE),
    )
    return {
        "outer": outer,
        "telemetry": _msgtypes(attributes.get(TELEMETRY)),
        "snapshot": _msgtypes(attributes.get(SNAPSHOT)),
        "status": [msgtype(request), msgtype(reply)]
        if isinstance(request, str) and isinstance(reply, str)
        else None,
        "subscribe": msgtype(subscribe) if isinstance(subscribe, str) else None,
        "commands": [
            {"method": method} | sent
            for method, sends in sorted(commands.items())
            for sent in sends
        ],
        "properties": {p: r for p, r in reads.items() if r},
    }


def read_solixble(root: Path) -> dict[str, dict[str, Any]]:
    """Return every device class's facts by class name."""
    package = root / "SolixBLE"
    paths = sorted(package.glob("*.py")) + sorted((package / "devices").glob("*.py"))
    shared = module_constants(paths)
    classes = {cls.name: cls for path in paths for cls in read_module(path, shared)}
    facts = {name: device_facts(classes, name) for name in classes}
    return {name: fact for name, fact in sorted(facts.items()) if fact}


def main(argv: list[str] | None = None) -> int:
    """Run the import; return the exit status."""
    parser = argparse.ArgumentParser(description="Read SolixBLE's device classes.")
    parser.add_argument("--solixble", required=True, action="append")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    sources: list[str] = []
    classes: dict[str, dict[str, Any]] = {}
    for given in args.solixble:
        path, _, label = given.partition("=")
        root = Path(path)
        sources.append(label or f"SolixBLE {revision(root)}".strip())
        added = read_solixble(root)
        classes |= {name: facts for name, facts in added.items() if name not in classes}
    data = {"sources": sources, "classes": dict(sorted(classes.items()))}
    args.output.write_text(json.dumps(data, indent=1) + "\n")
    sys.stderr.write(f"read {len(classes)} device classes\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
