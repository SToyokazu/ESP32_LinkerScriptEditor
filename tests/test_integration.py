"""複数スクリプトと実際の ESP-IDF 生成物による統合テスト。"""

from pathlib import Path
import sys
import unittest


TOOL_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = TOOL_DIR.parents[1]
sys.path.insert(0, str(TOOL_DIR))

from linker_editor.core import (  # noqa: E402
    move_section,
    parse_script,
    resolve_regions,
    update_region,
)
from linker_editor.mapfile import parse_map  # noqa: E402
from linker_editor.profiles import CHIP_NAMES, create_template  # noqa: E402


def read_exact(path):
    """改行を変換せずに UTF-8 のテキストを読み込む。"""
    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


class WorkspaceIntegrationTests(unittest.TestCase):
    def test_separate_memory_and_sections_keep_original_source(self):
        memory_text = (
            "/* separate memory */\r\nMEMORY {\r\n"
            "  ram (rwx) : ORIGIN = 0x40080000, LENGTH = 128K\r\n"
            "  flash (rx) : ORIGIN = 0x400D0020, LENGTH = 0x330000-0x20\r\n"
            "}\r\nREGION_ALIAS(\"code\", flash);\r\n"
        )
        section_text = (
            'ENTRY(start)\r\nSECTIONS {\r\n'
            ' .text : { KEEP(*(.entry)) /* } literal brace */ *(.text .text.*) }'
            ' > code AT> flash\r\n'
            ' ASSERT(SIZEOF(.text) < 128K, "text > limit");\r\n}\r\n'
        )
        memory = parse_script(memory_text, "memory.ld")
        sections = parse_script(section_text, "sections.ld")
        resolve_regions([memory, sections])
        self.assertEqual(memory.source, memory_text)
        self.assertEqual(sections.source, section_text)
        self.assertEqual(memory.regions[0].origin, 0x40080000)
        self.assertEqual(memory.regions[0].length, 128 * 1024)
        self.assertEqual(sections.sections[0].region, "code")
        self.assertEqual(sections.sections[0].load_region, "flash")

        moved = move_section(sections, 0, "ram")
        self.assertEqual(moved, section_text.replace("> code", "> ram"))
        reparsed = parse_script(moved, "sections.ld")
        self.assertEqual(reparsed.sections[0].region, "ram")
        self.assertEqual(reparsed.sections[0].load_region, "flash")
        self.assertEqual(reparsed.sections[0].body, sections.sections[0].body)
        self.assertEqual(memory.source, memory_text)

    def test_region_resize_does_not_rewrite_other_document(self):
        source = (
            "/* preserve */\nMEMORY {\n"
            " ram (rw) : ORIGIN = 0x3ffb0000, LENGTH = 128K /* tail */\n"
            "}\nSECTIONS { .data : { *(.data) } > ram }\n"
        )
        script = parse_script(source)
        edited = update_region(script, 0, "0x3ffb0000", "96K")
        self.assertEqual(edited, source.replace("128K", "96K"))
        parsed = parse_script(edited)
        resolve_regions([parsed])
        self.assertEqual(parsed.regions[0].length, 96 * 1024)

    def test_both_chip_templates_roundtrip_and_relocate(self):
        self.assertIn("ESP32", CHIP_NAMES)
        self.assertIn("ESP32-S3", CHIP_NAMES)
        for chip in CHIP_NAMES:
            with self.subTest(chip=chip):
                source = create_template(chip)
                script = parse_script(source, f"{chip}.ld")
                resolve_regions([script])
                self.assertEqual(script.source, source)
                self.assertGreaterEqual(len(script.regions), 4)
                self.assertGreaterEqual(len(script.sections), 3)
                for region in script.regions:
                    self.assertIsNotNone(region.origin, region.name)
                    self.assertIsNotNone(region.length, region.name)
                    self.assertGreater(region.length, 0, region.name)
                section_index = next(
                    index for index, section in enumerate(script.sections)
                    if section.region and not section.address_expr
                )
                destination = script.regions[0].name
                changed = move_section(script, section_index, destination)
                again = parse_script(changed)
                self.assertEqual(again.sections[section_index].region, destination)
                self.assertEqual(
                    again.sections[section_index].body,
                    script.sections[section_index].body,
                )


class ExistingBuildIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.memory_path = REPO_DIR / "build/esp-idf/esp_system/ld/memory.ld"
        self.sections_path = REPO_DIR / "build/esp-idf/esp_system/ld/sections.ld"
        self.map_path = REPO_DIR / "build/first_sample.map"
        if not all(path.is_file() for path in (
            self.memory_path, self.sections_path, self.map_path
        )):
            self.skipTest("ローカル ESP-IDF ビルド生成物がないためスキップ")

    def test_real_build_preserves_source_and_resolves_memory(self):
        memory_text = read_exact(self.memory_path)
        section_text = read_exact(self.sections_path)
        memory = parse_script(memory_text, str(self.memory_path))
        sections = parse_script(section_text, str(self.sections_path))
        resolve_regions([memory, sections])
        self.assertEqual(memory.source, memory_text)
        self.assertEqual(sections.source, section_text)
        self.assertGreaterEqual(len(memory.regions), 6)
        self.assertGreaterEqual(len(sections.sections), 20)
        iram = next(region for region in memory.regions if region.name == "iram0_0_seg")
        self.assertIsNotNone(iram.origin)
        self.assertIsNotNone(iram.length)
        text_index = next(index for index, section in enumerate(sections.sections)
                          if section.name == ".flash.text")
        original_section = sections.sections[text_index]
        changed = move_section(sections, text_index, "iram0_0_seg")
        reparsed = parse_script(changed)
        self.assertEqual(reparsed.sections[text_index].body, original_section.body)
        self.assertEqual(reparsed.sections[text_index].region, "iram0_0_seg")
        self.assertEqual(len(reparsed.sections), len(sections.sections))

    def test_real_map_matches_output_sections_and_retains_user_object(self):
        scripts = [parse_script(read_exact(self.memory_path)),
                   parse_script(read_exact(self.sections_path))]
        resolve_regions(scripts)
        mapped = parse_map(read_exact(self.map_path))
        output = {section.name: section for section in mapped.sections}
        self.assertIn(".flash.text", output)
        self.assertIn(".iram0.text", output)
        self.assertGreater(output[".flash.text"].size, 0)
        names = {section.name for section in scripts[1].sections}
        self.assertIn(".flash.text", names)
        entries = [entry for section in mapped.sections for entry in section.entries]
        self.assertTrue(any("led_blink" in entry.object_file for entry in entries))
        self.assertTrue(any(entry.name == "led_blink_task" for entry in entries))


if __name__ == "__main__":
    unittest.main()
