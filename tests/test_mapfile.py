"""Map parsing tests: distinguish measured output sizes from nested contributions."""

from pathlib import Path
import unittest

from linker_editor.mapfile import parse_map


class MapParserTests(unittest.TestCase):
    def test_discarded_wrapped_input_lma_symbols_and_fill(self):
        result = parse_map("""Discarded input sections
 .text.dead     0x00000000       0x50 dead.o
Linker script and memory map
.flash.text
                0x400d0020       0x20 load address 0x00010020
 .text.function_with_long_name
                0x400d0020       0x10 libapp.a(main.c.obj)
                                 0x14 (size before relaxing)
                0x400d0020                function_with_long_name
 *fill*         0x400d0030        0x4
 .text.other    0x400d0034        0xc other.o
                0x400d0034                other
                0x400d0040                _end = ABSOLUTE (.)
Cross Reference Table
.text.fake      0x400d0040       0x80
""")
        self.assertEqual(len(result.sections), 1)
        section = result.sections[0]
        self.assertEqual((section.name, section.address, section.size, section.load_address),
                         (".flash.text", 0x400D0020, 0x20, 0x10020))
        self.assertEqual([entry.kind for entry in section.entries],
                         ["input", "symbol", "fill", "input", "symbol"])
        self.assertEqual(section.entries[1].object_file, "libapp.a(main.c.obj)")
        self.assertEqual(section.entries[1].size, 0)
        self.assertEqual(sum(entry.size for entry in section.entries), section.size)
        self.assertEqual(result.diagnostics, [])

    def test_no_header_does_not_treat_discarded_entries_as_output(self):
        result = parse_map("Discarded input sections\n.text.dead 0x0 0x100\n")
        self.assertEqual(result.sections, [])
        self.assertTrue(result.diagnostics)

    def test_headerless_excerpt_zero_size_and_non_dot_section(self):
        result = parse_map("ROM 0x1000 0x10\n .text 0x1000 0x10 main.o\n.empty 0x1010 0x0\n")
        self.assertEqual([section.name for section in result.sections], ["ROM", ".empty"])
        self.assertEqual(result.sections[1].size, 0)
        self.assertTrue(result.diagnostics)

    def test_symbols_outside_output_range_do_not_belong_to_last_section(self):
        result = parse_map(".text 0x1000 0x10\n 0x40000000 rom_function\n")
        self.assertEqual(result.sections[0].entries, [])

    def test_existing_workspace_build_if_present(self):
        path = Path(__file__).resolve().parents[3] / "build" / "first_sample.map"
        if not path.exists():
            self.skipTest("Workspace build map is optional")
        result = parse_map(path.read_text(encoding="utf-8"))
        self.assertFalse(result.diagnostics)
        sections = {section.name: section for section in result.sections}
        self.assertIn(".flash.text", sections)
        functions = [entry for entry in sections[".flash.text"].entries
                     if entry.kind == "symbol" and entry.name == "app_main"]
        self.assertEqual(len(functions), 1)
        self.assertIn("main.c.obj", functions[0].object_file)
        self.assertGreater(sections[".flash.text"].size, 0)


if __name__ == "__main__":
    unittest.main()
