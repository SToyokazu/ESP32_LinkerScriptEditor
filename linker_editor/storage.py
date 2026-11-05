"""スクリプト、編集プロジェクト、出力ファイルの入出力。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

FORMAT = "esp32-linker-editor"


def read_text(path: Path) -> str:
    """改行および UTF-8 BOM を保持して読み込む。"""
    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def atomic_write(path: Path, text: str) -> None:
    """同じディレクトリの一時ファイルから置換する。"""
    fd, temporary = tempfile.mkstemp(prefix=".linker-editor-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_project(path: Path, state: dict) -> None:
    atomic_write(path, json.dumps({"format": FORMAT, "version": 1, **state},
                                 ensure_ascii=False, indent=2) + "\n")


def read_project(path: Path) -> dict:
    data = json.loads(read_text(path).lstrip("\ufeff"))
    if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("version") != 1:
        raise ValueError("対応していないプロジェクト形式です。")
    if data.get("chip") not in ("ESP32", "ESP32-S3"):
        raise ValueError("対応していないチップです。")
    scripts = data.get("scripts")
    if not isinstance(scripts, list) or not scripts:
        raise ValueError("スクリプトがありません。")
    for item in scripts:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("name", "source")):
            raise ValueError("スクリプトの形式が不正です。")
        safe_filename(item["name"])
    if len({s["name"].casefold() for s in scripts}) != len(scripts):
        raise ValueError("同名のスクリプトが含まれています。")
    if not isinstance(data.get("map_text", ""), str) or not isinstance(data.get("map_name", ""), str):
        raise ValueError("MAP データの形式が不正です。")
    if not isinstance(data.get("map_stale", False), bool):
        raise ValueError("MAP の状態が不正です。")
    paths = data.get("paths", {})
    if not isinstance(paths, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in paths.items()):
        raise ValueError("参照元パスの形式が不正です。")
    return data


def safe_filename(name: str) -> str:
    """プロジェクト経由のディレクトリ外への出力を防ぐ。"""
    if (not name or name in (".", "..") or any(c in name for c in '/\\:<>"|?*')
            or any(ord(c) < 32 for c in name) or name.endswith((".", " "))
            or name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}):
        raise ValueError(f"出力ファイル名が不正です: {name!r}")
    return name


def export_scripts(folder: Path, scripts: list) -> list[Path]:
    """ファイル名の衝突を先に検査して、各ソースをそのまま出力する。"""
    names = [safe_filename(script.name) for script in scripts]
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("同名のスクリプトは同じフォルダーに出力できません。")
    destinations = [folder / name for name in names]
    for path, script in zip(destinations, scripts):
        atomic_write(path, script.source)
    return destinations
