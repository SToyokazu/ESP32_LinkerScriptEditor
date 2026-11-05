"""ファイル出力の原文保持と編集プロジェクト入力の検証。"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from linker_editor.core import parse_script
from linker_editor.storage import (FORMAT, atomic_write, export_scripts,
                                   read_project, read_text, safe_filename, save_project)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="linker-storage-test-")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.source = '\ufeff/* 日本語 / KEEP */\r\nMEMORY { ram : ORIGIN = 0x1000, LENGTH = 4K }\r\n'
        self.state = {"chip": "ESP32", "scripts": [{"name": "memory.ld", "source": self.source}],
                      "map_text": "", "map_name": "", "map_stale": False}

    def test_source_bytes_bom_comments_and_crlf_survive_export(self):
        script = parse_script(self.source, "memory.ld")
        result = export_scripts(self.folder, [script])
        self.assertEqual(result, [self.folder / "memory.ld"])
        self.assertEqual(result[0].read_bytes(), self.source.encode("utf-8"))
        self.assertEqual(read_text(result[0]), self.source)

    def test_atomic_replacement_does_not_leave_temporary_files(self):
        path = self.folder / "memory.ld"
        path.write_bytes(b"old data")
        atomic_write(path, self.source)
        self.assertEqual(path.read_bytes(), self.source.encode("utf-8"))
        self.assertEqual([item.name for item in self.folder.iterdir()], ["memory.ld"])

    def test_project_roundtrip_preserves_source_and_map_state(self):
        path = self.folder / "project.json"
        self.state.update(map_text=".text 0x1000 0x20\r\n", map_name="test.map", map_stale=True)
        save_project(path, self.state)
        loaded = read_project(path)
        self.assertEqual(loaded, {"format": FORMAT, "version": 1, **self.state})

    def test_duplicate_export_names_fail_before_writing(self):
        scripts = [parse_script("first", "a.ld"), parse_script("second", "A.LD")]
        with self.assertRaises(ValueError):
            export_scripts(self.folder, scripts)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_invalid_filenames_are_rejected(self):
        for name in ("../outside.ld", "..\\outside.ld", "C:outside.ld", "sub/one.ld",
                     "NUL.ld", "COM1.ld", "x.ld ", "x.ld.", "", "a\x00.ld"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_filename(name)
        self.assertEqual(safe_filename("日本語.memory.ld"), "日本語.memory.ld")

    def test_invalid_project_fields_and_collisions_are_rejected(self):
        cases = [
            {"chip": "ESP32-C6"}, {"scripts": []}, {"scripts": "memory.ld"},
            {"scripts": [{"name": "../bad.ld", "source": ""}]},
            {"scripts": [{"name": "memory.ld", "source": 5}]},
            {"scripts": [{"name": "a.ld", "source": ""}, {"name": "A.LD", "source": ""}]},
            {"map_text": []}, {"map_name": None}, {"map_stale": "false"},
        ]
        path = self.folder / "project.json"
        for replacement in cases:
            with self.subTest(replacement=replacement):
                state = deepcopy(self.state)
                state.update(replacement)
                path.write_text(json.dumps({"format": FORMAT, "version": 1, **state}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_project(path)


if __name__ == "__main__":
    unittest.main()
