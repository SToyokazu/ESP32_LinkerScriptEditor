"""ローカル GUI の起動とコマンドライン引数。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="ESP32 / ESP32-S3 Linker Script Editor")
    parser.add_argument("scripts", nargs="*", type=Path, help="読み込む .ld（複数指定可）")
    parser.add_argument("--chip", choices=("ESP32", "ESP32-S3"), default="ESP32")
    parser.add_argument("--map", dest="map_path", type=Path, help="リンク済み GNU .map")
    parser.add_argument("--smoke-test", action="store_true", help="GUI を構築・描画して終了")
    args = parser.parse_args()

    try:
        import tkinter as tk
        from linker_editor.gui import Editor

        root = tk.Tk()
        app = Editor(root, chip=args.chip)
        if args.scripts:
            app.load_paths(args.scripts, replace=True)
        if args.map_path:
            app.load_map(args.map_path)
        if args.smoke_test:
            root.update()
            print(f"GUI OK: {len(app.scripts)} scripts, {len(app.region_refs)} regions, "
                  f"{len(app.section_refs)} sections")
            root.destroy()
        else:
            root.mainloop()
        return 0
    except (ImportError, OSError, ValueError) as exc:
        print(f"起動エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
