"""利用可能な ESP-IDF ツールチェーンで、出力を実際にリンクして検証する。"""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from linker_editor.core import move_section, parse_script, update_region
from linker_editor.mapfile import parse_map
from linker_editor.profiles import create_template
from linker_editor.storage import export_scripts


class ActualLinkerTests(unittest.TestCase):
    def _verify_chip(self, chip, target):
        compiler = shutil.which(f"xtensa-{target}-elf-gcc")
        description = Path(__file__).resolve().parents[3] / "build" / "project_description.json"
        if not compiler and description.is_file():
            configured = Path(json.loads(description.read_text(encoding="utf-8"))["c_compiler"])
            candidate = configured.with_name(f"xtensa-{target}-elf-gcc{configured.suffix}")
            if candidate.is_file():
                compiler = str(candidate)
        if not compiler:
            self.skipTest(f"{target} ツールチェーンがありません")
        compiler = Path(compiler)
        assembler = compiler.with_name(f"xtensa-{target}-elf-as{compiler.suffix}")
        linker = compiler.with_name(f"xtensa-{target}-elf-ld{compiler.suffix}")
        if not assembler.is_file() or not linker.is_file():
            self.skipTest(f"{target} assembler / linker がありません")

        def run(*command):
            result = subprocess.run([str(arg) for arg in command], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

        with tempfile.TemporaryDirectory(prefix="esp32-linker-output-") as temporary:
            folder = Path(temporary)
            asm = folder / "fixture.s"
            asm.write_text('.section .text.app,"ax"\n.global app\napp:\n.byte 1,2,3,4\n'
                           '.section .iram1,"ax"\n.byte 5,6,7,8\n'
                           '.section .data,"aw"\n.byte 1,2,3,4\n', encoding="utf-8")
            obj, elf, map_path = folder / "fixture.o", folder / "fixture.elf", folder / "fixture.map"
            run(assembler, "-o", obj, asm)
            script = parse_script(create_template(chip), "layout.ld")
            paths = export_scripts(folder, [script])
            run(linker, "-T", paths[0], "-Map", map_path, "-o", elf, obj)
            baseline = parse_map(map_path.read_text(encoding="utf-8"))
            baseline_text = next(s for s in baseline.sections if s.name == ".flash.text")
            flash_region = next(r for r in script.regions if r.name == "iram0_2_seg")
            self.assertEqual(baseline_text.address, flash_region.origin)

            index = next(i for i, section in enumerate(script.sections) if section.name == ".flash.text")
            script = parse_script(move_section(script, index, "iram0_0_seg"), script.name)
            ri = next(i for i, region in enumerate(script.regions) if region.name == "iram0_0_seg")
            script = parse_script(update_region(script, ri, script.regions[ri].origin_expr, "48K"), script.name)
            export_scripts(folder, [script])
            run(linker, "-T", paths[0], "-Map", map_path, "-o", elf, obj)
            moved = next(s for s in parse_map(map_path.read_text(encoding="utf-8")).sections if s.name == ".flash.text")
            ram = script.regions[ri]
            self.assertGreaterEqual(moved.address, ram.origin)
            self.assertLess(moved.address + moved.size, ram.origin + ram.length)
            self.assertNotEqual(moved.address, baseline_text.address)

    def test_esp32_export_and_relocation_link(self):
        self._verify_chip("ESP32", "esp32")

    def test_esp32s3_export_and_relocation_link(self):
        self._verify_chip("ESP32-S3", "esp32s3")


if __name__ == "__main__":
    unittest.main()
