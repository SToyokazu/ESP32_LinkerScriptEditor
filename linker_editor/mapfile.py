"""GNU ld の map から、リンク済みの出力セクションとその内容を読み取る。"""

from __future__ import annotations

from dataclasses import dataclass, field
import re


@dataclass
class MapEntry:
    """入力セクション・シンボル・パディング。シンボルの size=0 はサイズ不明。"""

    name: str
    address: int
    size: int
    object_file: str = ""
    kind: str = "input"


@dataclass
class MapSection:
    """出力セクションの VMA とサイズ。入力項目のサイズは加算しない。"""

    name: str
    address: int
    size: int
    load_address: int | None = None
    entries: list[MapEntry] = field(default_factory=list)


@dataclass
class MapData:
    sections: list[MapSection] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)


_HEX = r"0[xX][0-9a-fA-F]+"
_NAME = r"[.$A-Za-z_][^\s]*"
_OUTPUT = re.compile(rf"^(?P<name>{_NAME})\s+(?P<address>{_HEX})\s+(?P<size>{_HEX})(?P<tail>.*)$")
_INPUT = re.compile(rf"^\s+(?P<name>{_NAME}|\*fill\*)\s+(?P<address>{_HEX})\s+(?P<size>{_HEX})(?P<tail>.*)$")
_CONTINUATION = re.compile(rf"^\s+(?P<address>{_HEX})\s+(?P<size>{_HEX})(?P<tail>.*)$")
_SYMBOL = re.compile(rf"^\s+({_HEX})\s+(.+?)\s*$")
_LMA = re.compile(rf"\bload address\s+({_HEX})", re.IGNORECASE)
_NAME_ONLY = re.compile(rf"^(\s*)({_NAME})\s*$")
_DIRECTIVES = {"LOAD", "START", "END", "GROUP", "STARTGROUP", "ENDGROUP", "OUTPUT", "INPUT"}


def parse_map(text: str) -> MapData:
    """GNU ld map を解析する。破棄セクションとクロスリファレンスは対象外。"""
    data = MapData()
    marker = "Linker script and memory map"
    if marker in text:
        text = text.split(marker, 1)[1]
    elif "Discarded input sections" in text:
        data.diagnostics.append("map に『Linker script and memory map』がありません。破棄セクションの誤集計を防ぐため読み込みを中止しました。")
        return data
    else:
        data.diagnostics.append("GNU ld の map ヘッダーがありません。セクション一覧として読み込みました。")

    current: MapSection | None = None
    pending: tuple[str, bool] | None = None
    last_input: MapEntry | None = None

    def accept(name: str, is_output: bool, match: re.Match[str]) -> None:
        nonlocal current, last_input
        address = int(match["address"], 16)
        size = int(match["size"], 16)
        tail = match["tail"].strip()
        if is_output:
            load = _LMA.search(tail)
            current = MapSection(name, address, size, int(load[1], 16) if load else None)
            data.sections.append(current)
            last_input = None
        elif current is not None:
            kind = "fill" if name == "*fill*" else "input"
            entry = MapEntry(name, address, size, tail if kind == "input" else "", kind)
            current.entries.append(entry)
            last_input = entry if kind == "input" else None

    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "Cross Reference Table" or stripped.startswith("OUTPUT("):
            break
        if not stripped:
            pending = None
            continue

        if pending is not None:
            continuation = _CONTINUATION.match(line)
            name, is_output = pending
            pending = None
            if continuation is not None:
                accept(name, is_output, continuation)
                continue

        match = _OUTPUT.match(line)
        if match is not None and match["name"] not in _DIRECTIVES:
            accept(match["name"], True, match)
            continue

        match = _INPUT.match(line)
        if match is not None:
            accept(match["name"], False, match)
            continue

        match = _NAME_ONLY.match(line)
        if match is not None and match[2] not in _DIRECTIVES:
            pending = (match[2], not bool(match[1]))
            continue

        match = _SYMBOL.match(line)
        if match is None or current is None:
            continue
        address, name = int(match[1], 16), match[2]
        # Location-counter assignments and linker expressions are not code symbols.
        if ("=" in name or name.startswith((".", "PROVIDE", "ASSERT", "LOADADDR", "0x", "0X"))
                or "(size before relaxing)" in name):
            continue
        if not (current.address <= address <= current.address + current.size):
            continue
        object_file = ""
        if last_input is not None and last_input.address <= address < last_input.address + last_input.size:
            object_file = last_input.object_file
        current.entries.append(MapEntry(name, address, 0, object_file, "symbol"))

    if not data.sections:
        data.diagnostics.append("出力セクションを検出できませんでした。GNU ld が生成した .map を指定してください。")
    return data
