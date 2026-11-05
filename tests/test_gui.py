"""Tk の実ウィジェットを使用した編集・履歴・MAP 同期の統合テスト。"""

import json
from pathlib import Path
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from linker_editor.core import parse_script
from linker_editor.gui import Editor
from linker_editor.storage import FORMAT


SIMPLE_SOURCE = """/* preserve me */
MEMORY {
 ram (rwx) : ORIGIN = 0x40080000, LENGTH = 64K
 flash (rx) : ORIGIN = 0x400D0020, LENGTH = 1M
}
SECTIONS {
 .text : { *(.text .text.*) } > flash
 .data : { *(.data .data.*) } > ram
}
"""
SIMPLE_MAP = """Linker script and memory map
.text 0x400d0020 0x10
 .text.app 0x400d0020 0x10 main.o
 0x400d0020 app
.data 0x40080000 0x4
 .data.value 0x40080000 0x4 main.o
"""


class GuiIntegrationTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk の画面を利用できません: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = Editor(self.root)
        self.app.scripts = [parse_script(SIMPLE_SOURCE, "layout.ld")]
        self.app.saved_state = self.app.capture()
        self.app.refresh()
        self.root.update_idletasks()
        self.temporary = tempfile.TemporaryDirectory(prefix="linker-gui-test-")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.errors = self._patch("linker_editor.gui.messagebox.showerror")
        self._patch("linker_editor.gui.messagebox.showinfo")
        self._patch("linker_editor.gui.messagebox.askyesno", return_value=True)
        self.ask = self._patch("linker_editor.gui.messagebox.askyesnocancel", return_value=None)

    def _patch(self, name, **kwargs):
        mocker = patch(name, **kwargs)
        result = mocker.start()
        self.addCleanup(mocker.stop)
        return result

    def _set_source_input(self, text):
        self.app.source_text.delete("1.0", "end")
        self.app.source_text.insert("1.0", text)
        self.app._source_modified()
        self.assertTrue(self.app.source_dirty)

    def _load_map(self):
        path = self.folder / "snapshot.map"
        path.write_text(SIMPLE_MAP, encoding="utf-8")
        self.app.load_map(path)
        self.assertFalse(self.app.map_stale)

    def test_source_apply_undo_redo_keeps_exact_original(self):
        edited = SIMPLE_SOURCE.replace("LENGTH = 64K", "LENGTH = 48K")
        self._set_source_input(edited)
        self.assertTrue(self.app.apply_source())
        self.assertEqual(self.app.scripts[0].source, edited)
        self.assertEqual(self.app.region_refs[0][2].length, 48 * 1024)
        self.assertFalse(self.app.source_dirty)
        self.app.undo()
        self.assertEqual(self.app.scripts[0].source, SIMPLE_SOURCE)
        self.app.redo()
        self.assertEqual(self.app.scripts[0].source, edited)
        self.errors.assert_not_called()

    def test_source_apply_preserves_crlf_convention(self):
        source = SIMPLE_SOURCE.replace("\n", "\r\n")
        self.app.scripts = [parse_script(source, "layout.ld")]
        self.app.refresh()
        self._set_source_input(SIMPLE_SOURCE.replace("64K", "32K"))
        self.assertTrue(self.app.apply_source())
        self.assertEqual(self.app.scripts[0].source, source.replace("64K", "32K"))

    def test_invalid_source_is_not_committed(self):
        self._set_source_input("MEMORY {")
        self.assertFalse(self.app.apply_source())
        self.assertEqual(self.app.scripts[0].source, SIMPLE_SOURCE)
        self.assertTrue(self.app.source_dirty)
        self.errors.assert_called_once()

    def test_reassign_invalidates_map_and_undo_restores_snapshot(self):
        self._load_map()
        self.app.move_to_region(0, "ram")
        self.assertEqual(self.app.scripts[0].sections[0].region, "ram")
        self.assertEqual(self.app.scripts[0].source, SIMPLE_SOURCE.replace("> flash", "> ram"))
        self.assertTrue(self.app.map_stale)
        self.assertIn("編集前", self.app.map_label.cget("text"))
        self.app.undo()
        self.assertFalse(self.app.map_stale)
        self.assertEqual(self.app.scripts[0].source, SIMPLE_SOURCE)
        self.app.redo()
        self.assertTrue(self.app.map_stale)
        self.errors.assert_not_called()

    def test_drag_to_region_and_resize_use_editor_history(self):
        # Exercise the same release handler used by an actual canvas drag.
        x0, y0, x1, y1 = self.app.region_boxes[0]
        self.app.drag = ("s", 0, 1, 1)
        self.app.canvas_release(SimpleNamespace(x=int((x0 + x1) / 2), y=int(y0 + 30)))
        self.assertEqual(self.app.scripts[0].sections[0].region, "ram")
        self.app.drag = ("resize", 0, 100, 100)
        self.app.canvas_release(SimpleNamespace(x=220, y=100))
        self.assertEqual(self.app.region_refs[0][2].length, 96 * 1024)
        self.app.undo()
        self.assertEqual(self.app.region_refs[0][2].length, 64 * 1024)
        self.assertEqual(self.app.scripts[0].sections[0].region, "ram")
        self.errors.assert_not_called()

    def test_source_guard_cancel_and_discard_protect_pending_text(self):
        self._set_source_input(SIMPLE_SOURCE.replace("64K", "32K"))
        self.app.move_to_region(0, "ram")
        self.assertEqual(self.app.scripts[0].source, SIMPLE_SOURCE)
        self.assertTrue(self.app.source_dirty)
        self.ask.return_value = False
        self.app.move_to_region(0, "ram")
        self.assertFalse(self.app.source_dirty)
        self.assertEqual(self.app.region_refs[0][2].length, 64 * 1024)
        self.assertEqual(self.app.scripts[0].sections[0].region, "ram")

    def test_source_guard_apply_requires_retry_for_index_safety(self):
        self._set_source_input(SIMPLE_SOURCE.replace("64K", "32K"))
        self.ask.return_value = True
        self.app.move_to_region(0, "ram")
        self.assertFalse(self.app.source_dirty)
        self.assertEqual(self.app.region_refs[0][2].length, 32 * 1024)
        self.assertEqual(self.app.scripts[0].sections[0].region, "flash")
        self.app.move_to_region(0, "ram")
        self.assertEqual(self.app.scripts[0].sections[0].region, "ram")

    def test_loading_invalid_map_keeps_existing_state(self):
        self._load_map()
        before = self.app.capture()
        path = self.folder / "invalid.map"
        path.write_text("not a map", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.app.load_map(path)
        self.assertEqual(self.app.capture(), before)

    def test_close_discards_unapplied_invalid_source_without_saving(self):
        self.app.move_to_region(0, "ram")
        self._set_source_input("MEMORY {")
        self.ask.return_value = False
        save = self._patch("linker_editor.gui.save_project")
        with patch.object(self.root, "destroy") as destroy:
            self.app.close()
            destroy.assert_called_once()
        save.assert_not_called()
        self.errors.assert_not_called()
        self.ask.assert_called_once()

    def test_close_cancel_preserves_pending_source(self):
        self._set_source_input("MEMORY {")
        self.ask.return_value = None
        before = self.app.capture()
        with patch.object(self.root, "destroy") as destroy:
            self.app.close()
            destroy.assert_not_called()
        self.assertEqual(self.app.capture(), before)
        self.assertTrue(self.app.source_dirty)
        self.assertEqual(self.app.source_text.get("1.0", "end-1c"), "MEMORY {")

    def test_close_save_writes_pending_source_then_exits(self):
        edited = SIMPLE_SOURCE.replace("64K", "32K")
        self._set_source_input(edited)
        self.ask.return_value = True
        destination = self.folder / "saved.json"
        self._patch("linker_editor.gui.filedialog.asksaveasfilename", return_value=str(destination))
        with patch.object(self.root, "destroy") as destroy:
            self.app.close()
            destroy.assert_called_once()
        data = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(data["scripts"][0]["source"], edited)
        self.assertEqual(self.app.capture(), self.app.saved_state)

    def test_close_save_cancellation_and_failure_keep_window_open(self):
        self.app.move_to_region(0, "ram")
        self.ask.return_value = True
        filename = self._patch("linker_editor.gui.filedialog.asksaveasfilename", return_value="")
        with patch.object(self.root, "destroy") as destroy:
            self.app.close()
            destroy.assert_not_called()
            filename.return_value = str(self.folder / "saved.json")
            with patch("linker_editor.gui.save_project", side_effect=OSError("書き込み失敗")):
                self.app.close()
            destroy.assert_not_called()
        self.errors.assert_called_once()
        self.assertNotEqual(self.app.capture(), self.app.saved_state)

    def test_close_save_invalid_source_keeps_window_open(self):
        self._set_source_input("MEMORY {")
        self.ask.return_value = True
        save = self._patch("linker_editor.gui.save_project")
        with patch.object(self.root, "destroy") as destroy:
            self.app.close()
            destroy.assert_not_called()
        save.assert_not_called()
        self.assertTrue(self.app.source_dirty)

    def test_close_clean_workspace_does_not_prompt(self):
        with patch.object(self.root, "destroy") as destroy:
            self.app.close()
            destroy.assert_called_once()
        self.ask.assert_not_called()

    def test_no_continues_to_new_template_without_applying_invalid_source(self):
        self._set_source_input("MEMORY {")
        self.ask.return_value = False
        save = self._patch("linker_editor.gui.save_project")
        self.app.new_dialog()
        dialogs = [child for child in self.root.winfo_children() if isinstance(child, tk.Toplevel)]
        self.assertEqual(len(dialogs), 1)
        button = next(child for child in dialogs[0].winfo_children()
                      if child.winfo_class() == "TButton" and child.cget("text") == "作成")
        button.invoke()
        self.assertFalse(self.app.source_dirty)
        self.assertTrue(self.app.scripts[0].editable)
        save.assert_not_called()
        self.errors.assert_not_called()
        self.ask.assert_called_once()

    def test_cancel_stops_new_template_and_retains_input(self):
        self._set_source_input("MEMORY {")
        self.ask.return_value = None
        self.app.new_dialog()
        self.assertFalse(any(isinstance(child, tk.Toplevel) for child in self.root.winfo_children()))
        self.assertTrue(self.app.source_dirty)
        self.assertEqual(self.app.source_text.get("1.0", "end-1c"), "MEMORY {")

    def test_no_then_cancel_file_selection_retains_current_edits(self):
        self.app.move_to_region(0, "ram")
        self._set_source_input("MEMORY {")
        before = self.app.capture()
        self.ask.return_value = False
        picker = self._patch("linker_editor.gui.filedialog.askopenfilename", return_value="")
        self.app.open_project_dialog()
        picker.assert_called_once()
        self.assertEqual(self.app.capture(), before)
        self.assertTrue(self.app.source_dirty)
        self.assertEqual(self.app.source_text.get("1.0", "end-1c"), "MEMORY {")

    def test_map_search_finds_symbol_across_all_sections(self):
        self._load_map()
        self.app.selected = ("section", 1)
        self.app.map_all.set(True)
        self.app.map_filter.set("app")
        self.app.fill_entries()
        rows = [self.app.entry_tree.item(item, "values") for item in self.app.entry_tree.get_children()]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row[4] == ".text" for row in rows))
        self.assertTrue(any(row[0] == "app" and row[2] == "—" for row in rows))
        self.app.map_all.set(False)
        self.app.fill_entries()
        self.assertFalse(self.app.entry_tree.get_children())

    def test_undo_and_redo_restore_include_search_paths(self):
        first, second = self.folder / "first", self.folder / "second"
        first.mkdir()
        second.mkdir()
        for folder in (first, second):
            (folder / "layout.ld").write_text(SIMPLE_SOURCE, encoding="utf-8")
        self.app.load_paths([first / "layout.ld"], replace=True)
        self.app.load_paths([second / "layout.ld"], replace=True)
        self.app.undo()
        self.assertEqual(self.app.paths["layout.ld"], (first / "layout.ld").resolve())
        self.app.redo()
        self.assertEqual(self.app.paths["layout.ld"], (second / "layout.ld").resolve())

    def test_open_project_without_optional_map_fields_starts_clean(self):
        self._load_map()
        self._set_source_input("MEMORY {")
        self.ask.return_value = False
        path = self.folder / "project.json"
        path.write_text(json.dumps({"format": FORMAT, "version": 1, "chip": "ESP32",
                                    "scripts": [{"name": "layout.ld", "source": SIMPLE_SOURCE}]}), encoding="utf-8")
        self._patch("linker_editor.gui.filedialog.askopenfilename", return_value=str(path))
        self.app.open_project_dialog()
        self.assertEqual(self.app.map_text, "")
        self.assertEqual(self.app.capture(), self.app.saved_state)
        self.assertFalse(self.app.source_dirty)
        self.ask.assert_called_once()
        self.errors.assert_not_called()

    def test_actual_build_folder_loads_sources_and_code_objects(self):
        build = Path(__file__).resolve().parents[3] / "build"
        if not (build / "first_sample.map").is_file():
            self.skipTest("Workspace build map is optional")
        self.app.move_to_region(0, "ram")
        self._set_source_input("MEMORY {")
        self.ask.return_value = False
        self._patch("linker_editor.gui.filedialog.askdirectory", return_value=str(build))
        self.app.open_build()
        self.assertFalse(self.app.source_dirty)
        self.ask.assert_called_once()
        self.assertEqual({script.name for script in self.app.scripts}, {"memory.ld", "sections.ld"})
        self.assertEqual(self.app.map_name, "first_sample.map")
        self.assertFalse(self.app.map_stale)
        self.assertGreaterEqual(len(self.app.region_refs), 6)
        index = next(index for index, (_, _, section) in enumerate(self.app.section_refs)
                     if section.name == ".flash.text")
        self.app.selected = ("section", index)
        self.app.show_inspector()
        rows = [self.app.entry_tree.item(item, "values") for item in self.app.entry_tree.get_children()]
        self.assertTrue(any(row[0] == "led_blink_task" and "led_blink" in row[3] for row in rows))
        self.errors.assert_not_called()


if __name__ == "__main__":
    unittest.main()
