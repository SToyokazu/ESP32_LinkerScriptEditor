"""GNU ld スクリプトの配置情報を読み取り、指定した範囲だけを書き換える。

完全なリンカではない。元の文字列と位置を保持し、認識しない文を再生成しない。
実際のアドレス、サイズ、入力セクションの採用結果はリンク時に確定する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import operator
from pathlib import PurePath
import re
from typing import Iterable


Span = tuple[int, int]
_NAME = r"[A-Za-z_.$][A-Za-z0-9_.$-]*"


@dataclass
class Region:
    name: str
    attributes: str
    origin_expr: str
    length_expr: str
    origin: int | None = None
    length: int | None = None
    origin_span: Span = (0, 0)
    length_span: Span = (0, 0)


@dataclass
class Section:
    name: str
    header: str
    body: str
    region: str | None
    load_region: str | None
    address_expr: str = ""
    body_span: Span = (0, 0)
    region_span: Span | None = None
    close_pos: int = 0


@dataclass
class Script:
    source: str
    name: str = ""
    regions: list[Region] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    includes: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    editable: bool = True
    memory_blocks: list[Span] = field(default_factory=list)
    section_blocks: list[Span] = field(default_factory=list)
    assignments: dict[str, str] = field(default_factory=dict)


def _masked(text: str, strings: bool = False) -> str:
    """コメントを同じ長さの空白にし、元のオフセットを保持する。"""
    pattern = re.compile(r'/\*.*?\*/|//[^\r\n]*|"(?:\\.|[^"\\])*"', re.S)

    def replace(match: re.Match) -> str:
        value = match.group()
        if value.startswith('"') and not strings:
            return value
        return re.sub(r"[^\r\n]", " ", value)

    return pattern.sub(replace, text)


def _closing(text: str, start: int, opening: str = "{", closing: str = "}") -> int:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if not depth:
                return index
    raise ValueError("対応する閉じ括弧がありません。")


def _trim_span(masked: str, start: int, end: int) -> Span:
    while start < end and masked[start].isspace():
        start += 1
    while end > start and (masked[end - 1].isspace() or masked[end - 1] == ";"):
        end -= 1
    return start, end


def _top_comma(text: str, start: int, end: int) -> int:
    depth = 0
    for index in range(start, end):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
        elif text[index] == "," and depth == 0:
            return index
    return -1


def parse_script(text: str, name: str = "") -> Script:
    """スクリプトの原文、領域、出力セクションを位置情報付きで取得する。"""
    script = Script(text, name)
    masked = _masked(text)
    structural = _masked(text, strings=True)
    if "/*" in structural or '"' in structural:
        script.editable = False
        script.diagnostics.append("閉じられていないコメントまたは文字列があります。")
    if re.search(r"^\s*#\s*(?:if|ifdef|ifndef|elif|else|endif|include|define|undef)\b", masked, re.M):
        script.editable = False
        script.diagnostics.append("プリプロセッサ処理前の入力です。ESP-IDF のビルド生成済み .ld を読み込んでください。")
    try:
        depth = 0
        for char in structural:
            depth += (char == "{") - (char == "}")
            if depth < 0:
                raise ValueError("閉じ括弧が多すぎます。")
        if depth:
            raise ValueError("対応する閉じ括弧がありません。")

        for match in re.finditer(r"\b(MEMORY|SECTIONS)\s*\{", structural):
            start = match.end() - 1
            end = _closing(structural, start)
            if match.group(1) == "MEMORY":
                script.memory_blocks.append((start, end))
                _parse_memory(script, masked, start + 1, end)
            else:
                script.section_blocks.append((start, end))
                _parse_sections(script, masked, structural, start + 1, end)
    except ValueError as error:
        script.editable = False
        script.diagnostics.append(str(error))

    for match in re.finditer(r'\bREGION_ALIAS\s*\(\s*"([^"\r\n]+)"\s*,\s*(' + _NAME + r")\s*\)", masked):
        alias, destination = match.groups()
        if alias in script.aliases:
            script.diagnostics.append(f"領域エイリアス {alias} が重複しています。")
        script.aliases[alias] = destination
    script.includes = [match.group(1) or match.group(2) for match in re.finditer(r'\bINCLUDE\s+(?:"([^"\r\n]+)"|([^\s;]+))', masked)]
    # SECTIONS 内の代入は位置カウンタに依存し得るため定数扱いしない。
    global_text = list(masked)
    for begin, end in script.memory_blocks + script.section_blocks:
        global_text[begin:end + 1] = " " * (end + 1 - begin)
    script.assignments = dict(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^;{}]+);", "".join(global_text)))
    resolve_regions([script])
    if not script.regions and not script.sections:
        script.diagnostics.append("MEMORY / SECTIONS を検出できませんでした。原文は保持しています。")
    return script


def _parse_memory(script: Script, masked: str, start: int, end: int) -> None:
    pattern = re.compile(r"(" + _NAME + r")\s*(?:\(\s*([rRwWxXaAiIlL!]*)\s*\))?\s*:\s*(?:ORIGIN|org|o)\s*=\s*")
    entries = list(pattern.finditer(masked, start, end))
    # 未対応形式の次の宣言を前の LENGTH の一部として取り込まない。
    declarations = list(re.finditer(r"(" + _NAME + r")\s*(?:\([^()]*\))?\s*:\s*(?:ORIGIN|org|o|LENGTH|len|l)\s*=", masked[start:end]))
    boundaries = [start + item.start() for item in declarations] + [end]
    consumed: list[Span] = []
    for index, match in enumerate(entries):
        boundary = next((position for position in boundaries if position > match.start()), end)
        comma = _top_comma(masked, match.end(), boundary)
        length_match = re.match(r",\s*(?:LENGTH|len|l)\s*=\s*", masked[comma:boundary]) if comma >= 0 else None
        if not length_match:
            script.diagnostics.append(f"MEMORY {match.group(1)} の ORIGIN / LENGTH を解析できません。")
            continue
        origin_span = _trim_span(masked, match.end(), comma)
        length_span = _trim_span(masked, comma + length_match.end(), boundary)
        script.regions.append(Region(match.group(1), match.group(2) or "", script.source[slice(*origin_span)], script.source[slice(*length_span)], origin_span=origin_span, length_span=length_span))
        consumed.append((match.start(), boundary))
    residual = list(masked[start:end])
    for begin, boundary in consumed:
        residual[begin - start:boundary - start] = " " * (boundary - begin)
    if "".join(residual).strip(" \t\r\n;"):
        script.diagnostics.append("MEMORY に未対応の宣言があります。その部分は原文のまま保持します。")


def _parse_sections(script: Script, masked: str, structural: str, start: int, end: int) -> None:
    pattern = re.compile(r"(?P<name>/DISCARD/|" + _NAME + r")(?P<prefix>[^;{}]*?):(?P<suffix>[^;{}]*?)$", re.S)
    cursor = start
    while cursor < end:
        opening = structural.find("{", cursor, end)
        if opening < 0:
            break
        closing = _closing(structural, opening)
        if closing > end:
            raise ValueError("SECTIONS の範囲が不正です。")
        # ASSERT / シンボル代入をヘッダーに巻き込まないよう、候補の行頭を後ろから調べる。
        candidate_starts = [cursor] + [cursor + m.end() for m in re.finditer(r"[\n;]", structural[cursor:opening])]
        match = None
        header_start = cursor
        for candidate in reversed(candidate_starts):
            segment = structural[candidate:opening]
            offset = len(segment) - len(segment.lstrip())
            found = pattern.fullmatch(segment.strip())
            if found and "=" not in found.group("prefix") and found.group("name") != ".":
                match, header_start = found, candidate + offset
                break
        cursor = closing + 1
        if match is None or match.group("name") in {"OVERLAY", "ASSERT", "PROVIDE", "PROVIDE_HIDDEN"}:
            script.diagnostics.append("未対応の出力セクション宣言を原文のまま保持しています。")
            continue
        prefix = match.group("prefix").strip()
        # このセクションの直後にある属性だけを走査する。次の文まで進まない。
        tail_cursor = closing + 1
        region = load_region = None
        region_span = None
        while tail_cursor < end:
            whitespace = re.match(r"\s*", masked[tail_cursor:end]).end()
            tail_cursor += whitespace
            tail = masked[tail_cursor:end]
            load_match = re.match(r"AT\s*>\s*(" + _NAME + r")", tail)
            region_match = re.match(r">\s*(" + _NAME + r")", tail)
            phdr_match = re.match(r":\s*" + _NAME, tail)
            if load_match:
                load_region = load_match.group(1)
                tail_cursor += load_match.end()
            elif region_match:
                region = region_match.group(1)
                region_span = (tail_cursor + region_match.start(1), tail_cursor + region_match.end(1))
                tail_cursor += region_match.end()
            elif phdr_match:
                tail_cursor += phdr_match.end()
            else:
                break
        cursor = tail_cursor
        address = re.sub(r"\(\s*(?:NOLOAD|DSECT|COPY|INFO|OVERLAY|READONLY)\s*\)", "", prefix).strip()
        script.sections.append(Section(match.group("name"), script.source[header_start:opening].strip(), script.source[opening + 1:closing], region, load_region, address, (opening + 1, closing), region_span, closing))


_PRECEDENCE = {"||": 1, "&&": 2, "|": 3, "^": 4, "&": 5,
               "==": 6, "!=": 6, "<": 7, "<=": 7, ">": 7, ">=": 7,
               "<<": 8, ">>": 8, "+": 9, "-": 9, "*": 10, "/": 10, "%": 10}
_BINARY = {"+": operator.add, "-": operator.sub, "*": operator.mul,
           "|": operator.or_, "&": operator.and_, "^": operator.xor,
           "<<": operator.lshift, ">>": operator.rshift}
_COMPARE = {"==": operator.eq, "!=": operator.ne, "<": operator.lt,
            "<=": operator.le, ">": operator.gt, ">=": operator.ge}
_TOKEN = re.compile(r"\s*(0[xX][0-9a-fA-F]+[kKmM]?|[0-9]+[kKmM]?|[A-Za-z_.$][A-Za-z0-9_.$]*|<<|>>|<=|>=|==|!=|&&|\|\||[()+*/%~!&|^<>?:,\-])")


def _expression_tree(value: str) -> tuple:
    """GNU ld / C の演算子優先順位で、実行可能コードを作らず式を解析する。"""
    if not value or len(value) > 4096:
        raise ValueError("数値式が空か、長すぎます。")
    tokens: list[str] = []
    offset = 0
    while offset < len(value):
        match = _TOKEN.match(value, offset)
        if not match:
            raise ValueError("数値式に使用できない記号があります。")
        tokens.append(match.group(1))
        offset = match.end()
    if len(tokens) > 256:
        raise ValueError("数値式が長すぎます。")
    position = 0

    def take(expected: str) -> None:
        nonlocal position
        if position >= len(tokens) or tokens[position] != expected:
            raise ValueError("数値式の演算子または括弧の対応が不正です。")
        position += 1

    def expression(minimum: int = 0, depth: int = 0) -> tuple:
        nonlocal position
        if depth > 64 or position >= len(tokens):
            raise ValueError("数値式の項が不足しているか、入れ子が深すぎます。")
        token = tokens[position]
        position += 1
        if token in {"+", "-", "~", "!"}:
            node = ("unary", token, expression(11, depth + 1))
        elif token == "(":
            node = expression(0, depth + 1)
            take(")")
        elif re.fullmatch(r"0[xX][0-9a-fA-F]+[kKmM]?|[0-9]+[kKmM]?", token):
            number = token[:-1] if token[-1] in "kKmM" else token
            base = 16 if number.lower().startswith("0x") else 8 if number.startswith("0") and len(number) > 1 else 10
            scale = 1024 if token[-1] in "kK" else 1024 * 1024 if token[-1] in "mM" else 1
            node = ("number", int(number, base) * scale)
        elif re.fullmatch(r"[A-Za-z_.$][A-Za-z0-9_.$]*", token):
            node = ("name", token)
            if position < len(tokens) and tokens[position] == "(":
                position += 1
                arguments, raw_arguments = [], []
                if position < len(tokens) and tokens[position] != ")":
                    while True:
                        first = position
                        arguments.append(expression(0, depth + 1))
                        raw_arguments.append("".join(tokens[first:position]))
                        if position >= len(tokens) or tokens[position] != ",":
                            break
                        position += 1
                take(")")
                node = ("call", token, tuple(arguments), tuple(raw_arguments))
        else:
            raise ValueError("数値式の項が不正です。")
        while position < len(tokens) and _PRECEDENCE.get(tokens[position], -1) >= minimum:
            operation = tokens[position]
            position += 1
            node = ("binary", operation, node, expression(_PRECEDENCE[operation] + 1, depth + 1))
        if minimum == 0 and position < len(tokens) and tokens[position] == "?":
            position += 1
            when_true = expression(0, depth + 1)
            take(":")
            node = ("conditional", node, when_true, expression(0, depth + 1))
        return node

    tree = expression()
    if position != len(tokens):
        raise ValueError("数値式の後ろに余分な記述があります。")
    return tree


def evaluate_expression(expr: str, symbols: dict[str, int] | None = None,
                        regions: dict[str, Region] | None = None) -> int | None:
    """定数式の対応サブセットを安全に計算する。未解決・未対応なら None。"""
    symbols, regions = symbols or {}, regions or {}
    expression = _masked(expr).strip()
    try:
        tree = _expression_tree(expression)

        def visit(node: tuple) -> int:
            kind = node[0]
            if kind == "number":
                result = node[1]
            elif kind == "name":
                result = symbols[node[1]]
            elif kind == "binary":
                operation, left = node[1], visit(node[2])
                if operation == "&&":
                    return int(bool(left) and bool(visit(node[3])))
                if operation == "||":
                    return int(bool(left) or bool(visit(node[3])))
                right = visit(node[3])
                if operation in {"<<", ">>"} and not 0 <= right <= 63:
                    raise ValueError("shift limit")
                # ld の bfd_vma 幅・符号拡張に依存するケースは推測しない。
                # ESP32 の通常の 32 ビットアドレス計算はこの制限にかからない。
                if operation in {"<<", ">>"} and left < 0:
                    raise ValueError("target-dependent signed shift")
                if operation in {"<", "<=", ">", ">="} and (left < 0 or right < 0):
                    raise ValueError("target-dependent signed comparison")
                if operation in _BINARY:
                    result = _BINARY[operation](left, right)
                elif operation in _COMPARE:
                    result = int(_COMPARE[operation](left, right))
                else:
                    if not (-2**63 <= left < 2**63 and -2**63 <= right < 2**63):
                        raise ValueError("target-dependent signed division")
                    quotient = (abs(left) // abs(right)) * (-1 if (left < 0) != (right < 0) else 1)
                    result = quotient if operation == "/" else left - quotient * right
            elif kind == "unary":
                operation, value = node[1], visit(node[2])
                result = value if operation == "+" else -value if operation == "-" else ~value if operation == "~" else int(not value)
            elif kind == "conditional":
                result = visit(node[2] if visit(node[1]) else node[3])
            elif kind == "call":
                function = node[1]
                if function in {"ORIGIN", "LENGTH"} and len(node[3]) == 1:
                    region = regions[node[3][0]]
                    result = region.origin if function == "ORIGIN" else region.length
                    if result is None:
                        raise ValueError("unresolved region")
                    return result
                arguments = [visit(item) for item in node[2]]
                if function == "ABSOLUTE" and len(arguments) == 1:
                    result = arguments[0]
                elif function in {"MAX", "MIN"} and len(arguments) == 2:
                    result = (max if function == "MAX" else min)(arguments)
                elif function in {"ALIGN", "ALIGN_UP"} and len(arguments) == 2 and arguments[1] > 0:
                    result = ((arguments[0] + arguments[1] - 1) // arguments[1]) * arguments[1]
                else:
                    raise ValueError("unsupported call")
            else:
                raise ValueError("unsupported expression")
            if type(result) is not int or abs(result) >= 2**64:
                raise ValueError("integer limit")
            return result

        return visit(tree)
    except (ValueError, TypeError, KeyError, ZeroDivisionError, OverflowError, RecursionError):
        return None


def region_lookup(scripts: Iterable[Script]) -> dict[str, Region]:
    """領域と解決可能なエイリアスを名前で引ける辞書にする。"""
    scripts = list(scripts)
    regions = {region.name: region for script in scripts for region in script.regions}
    aliases = {key: value for script in scripts for key, value in script.aliases.items()}
    for _ in range(len(aliases) + 1):
        changed = False
        for name, target in aliases.items():
            if name not in regions and target in regions:
                regions[name] = regions[target]
                changed = True
        if not changed:
            break
    return regions


def resolve_regions(scripts: Iterable[Script]) -> None:
    """複数ファイル間の定数・領域参照を反復解決する。"""
    scripts = list(scripts)
    regions = region_lookup(scripts)
    symbols: dict[str, int] = {}
    assignments = {key: value for script in scripts for key, value in script.assignments.items()}
    for region in regions.values():
        region.origin = region.length = None
    for _ in range(min(128, len(regions) * 2 + len(assignments) + 1)):
        changed = False
        for name, expression in assignments.items():
            value = evaluate_expression(expression, symbols, regions)
            if value is not None and symbols.get(name) != value:
                symbols[name] = value
                changed = True
        for region in regions.values():
            for attribute, expression in (("origin", region.origin_expr), ("length", region.length_expr)):
                value = evaluate_expression(expression, symbols, regions)
                if value is not None and getattr(region, attribute) != value:
                    setattr(region, attribute, value)
                    changed = True
        if not changed:
            break


def validate_scripts(scripts: Iterable[Script]) -> list[str]:
    """構造上の注意点を返す。GNU ld によるリンク検証の代わりにはならない。"""
    scripts = list(scripts)
    resolve_regions(scripts)
    messages = [f"{script.name}: {message}" for script in scripts for message in script.diagnostics]
    regions = region_lookup(scripts)
    seen: set[str] = set()
    physical = []
    for script in scripts:
        for region in script.regions:
            if region.name in seen:
                messages.append(f"領域名 {region.name} が重複しています。")
            seen.add(region.name)
            if region.origin is None or region.length is None:
                messages.append(f"{region.name}: アドレスまたは長さの式を解決できません。")
            elif region.origin < 0 or region.length < 0 or region.origin + region.length > 2**32:
                messages.append(f"{region.name}: 32 ビットアドレス範囲または長さが不正です。")
            elif region.length:
                physical.append(region)
        for section in script.sections:
            for destination in (section.region, section.load_region):
                if destination and destination not in regions:
                    messages.append(f"{section.name}: 配置先 {destination} が未定義です。関連する .ld も読み込んでください。")
        for alias, destination in script.aliases.items():
            if alias not in regions:
                messages.append(f"エイリアス {alias} → {destination} を解決できません。")
        loaded = {PurePath(item.name.replace('\\', '/')).name for item in scripts}
        for include in script.includes:
            if PurePath(include.replace('\\', '/')).name not in loaded:
                messages.append(f"INCLUDE {include}: 未読込です。必要なファイルを追加してください。")
    for index, left in enumerate(physical):
        for right in physical[index + 1:]:
            if max(left.origin, right.origin) < min(left.origin + left.length, right.origin + right.length):
                messages.append(f"{left.name} と {right.name} の仮想アドレスが重なっています。共有窓・ダミーセクションを確認してください。")
    return list(dict.fromkeys(messages))


def _ensure_editable(script: Script) -> None:
    if not script.editable:
        raise ValueError("このスクリプトは構造編集できません。前処理済みで括弧の対応が正しい .ld を使用してください。")


def _patch(source: str, changes: list[tuple[Span, str]]) -> str:
    for (start, end), replacement in sorted(changes, reverse=True):
        source = source[:start] + replacement + source[end:]
    return source


def _valid_name(value: str) -> bool:
    return re.fullmatch(_NAME, value) is not None


def _check_expression(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 4096 or re.search(r'[;{}#"\r\n]', value) or re.search(r"/\*|//", value):
        raise ValueError("アドレス・長さには単一の数値式を指定してください。")
    # 値の解決とは分離して構文を検証する。未定義シンボルは許可するが、
    # `0, LENGTH = ...` のように別の宣言を混入する入力は受け付けない。
    _expression_tree(value)
    return value


def update_region(script: Script, index: int, origin_expr: str, length_expr: str) -> str:
    """指定領域のアドレス式と長さ式だけを置き換える。"""
    _ensure_editable(script)
    region = script.regions[index]
    # 変更されていない式はコメントや未対応構文も含めてそのまま保持する。
    origin_expr = region.origin_expr if origin_expr.strip() == region.origin_expr else _check_expression(origin_expr)
    length_expr = region.length_expr if length_expr.strip() == region.length_expr else _check_expression(length_expr)
    origin, length = evaluate_expression(origin_expr), evaluate_expression(length_expr)
    if (origin is not None and not 0 <= origin < 2**32) or (length is not None and not 0 <= length <= 2**32):
        raise ValueError("アドレスまたは長さが 32 ビットの範囲外です。")
    if origin is not None and length is not None and origin + length > 2**32:
        raise ValueError("領域の末尾が 32 ビットのアドレス範囲を超えています。")
    changes = []
    if region.origin_expr != origin_expr:
        changes.append((region.origin_span, origin_expr))
    if region.length_expr != length_expr:
        changes.append((region.length_span, length_expr))
    return _patch(script.source, changes)


def move_section(script: Script, index: int, region: str) -> str:
    """VMA の配置先のみ変更する。AT> と固定アドレスの暗黙変更はしない。"""
    _ensure_editable(script)
    if not _valid_name(region):
        raise ValueError("配置先の領域名が不正です。")
    section = script.sections[index]
    if section.name == "/DISCARD/":
        raise ValueError("/DISCARD/ はメモリ領域へ移動できません。")
    if section.region == region:
        return script.source
    # コロン前の ALIGN(...) も現在の位置カウンタを使う明示アドレスである。
    # `> region` だけ変えると別領域のアドレスが残り得るため同様に扱う。
    if section.address_expr:
        raise ValueError("固定アドレス・複雑なアドレス式を持つセクションは、原文のアドレス指定も確認して編集してください。")
    span = section.region_span or (section.close_pos + 1, section.close_pos + 1)
    value = region if section.region_span else f" > {region}"
    return _patch(script.source, [(span, value)])


def update_section_body(script: Script, index: int, body: str) -> str:
    """入力セクション指定・シンボル定義など、セクション本文を変更する。"""
    _ensure_editable(script)
    masked = _masked(body, strings=True)
    if "/*" in masked or '"' in masked:
        raise ValueError("セクション本文のコメントまたは文字列が閉じられていません。")
    if "{" in masked or "}" in masked or re.search(r"^\s*#", masked, re.M):
        raise ValueError("セクション本文に外側の波括弧やプリプロセッサ命令は記述できません。")
    return _patch(script.source, [(script.sections[index].body_span, body)])


def add_region(script: Script, name: str, attributes: str, origin_expr: str, length_expr: str) -> str:
    """MEMORY ブロックに新しい領域を追加する。ブロックがなければ作成する。"""
    _ensure_editable(script)
    if not _valid_name(name) or any(region.name == name for region in script.regions) or name in script.aliases:
        raise ValueError("領域名が不正、または既に使用されています。")
    if not re.fullmatch(r"[rRwWxXaAiIlL!]*", attributes):
        raise ValueError("領域属性には R W X A I L ! を指定してください。")
    origin_expr, length_expr = _check_expression(origin_expr), _check_expression(length_expr)
    origin, length = evaluate_expression(origin_expr), evaluate_expression(length_expr)
    if (origin is not None and not 0 <= origin < 2**32) or (length is not None and not 0 <= length <= 2**32):
        raise ValueError("アドレスまたは長さが 32 ビットの範囲外です。")
    if origin is not None and length is not None and origin + length > 2**32:
        raise ValueError("領域の末尾が 32 ビットのアドレス範囲を超えています。")
    newline = "\r\n" if "\r\n" in script.source else "\n"
    attrs = f" ({attributes})" if attributes else ""
    declaration = f"  {name}{attrs} : ORIGIN = {origin_expr}, LENGTH = {length_expr}{newline}"
    if script.memory_blocks:
        position = script.memory_blocks[-1][1]
        return _patch(script.source, [((position, position), newline + declaration)])
    return f"MEMORY{newline}{{{newline}{declaration}}}{newline}{newline}" + script.source


def add_section(script: Script, name: str, region: str, body: str, noload: bool = False) -> str:
    """SECTIONS ブロックに出力セクションを追加する。"""
    _ensure_editable(script)
    if not _valid_name(name) or any(section.name == name for section in script.sections):
        raise ValueError("セクション名が不正、または既に使用されています。")
    if not _valid_name(region):
        raise ValueError("配置先の領域名が不正です。")
    masked = _masked(body, strings=True)
    if "/*" in masked or '"' in masked or re.search(r"^\s*#", masked, re.M):
        raise ValueError("セクション本文に閉じていないコメント・文字列、またはプリプロセッサ命令があります。")
    if "{" in masked or "}" in masked:
        raise ValueError("セクション本文には外側の波括弧を含めないでください。")
    newline = "\r\n" if "\r\n" in script.source else "\n"
    declaration = f"  {name}{' (NOLOAD)' if noload else ''} :{newline}  {{{newline}{body.rstrip()}{newline}  }} > {region}{newline}"
    if script.section_blocks:
        position = script.section_blocks[-1][1]
        return _patch(script.source, [((position, position), newline + declaration)])
    return script.source + f"{newline}SECTIONS{newline}{{{newline}{declaration}}}{newline}"
