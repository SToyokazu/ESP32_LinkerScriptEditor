"""構文の境界、非破壊編集、GNU ld 数値式の回帰テスト。"""

from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from linker_editor.core import (
    add_region, add_section, evaluate_expression, move_section, parse_script,
    region_lookup, resolve_regions, update_region, update_section_body,
    validate_scripts,
)


class ExpressionTests(unittest.TestCase):
    def test_gnu_c_precedence_and_comparisons(self):
        examples = {
            "2 & 1 == 0": 0,
            "4 | 2 == 2": 5,
            "1 < 2 < 1": 0,
            "1 << 2 + 1": 8,
            "1 || 0 && 0": 1,
            "3 ^ 1 & 1": 2,
            "2 + 3 * 4": 14,
            "(2 + 3) * 4": 20,
            "~0 & 0xff": 255,
        }
        for expression, expected in examples.items():
            with self.subTest(expression=expression):
                self.assertEqual(evaluate_expression(expression), expected)

    def test_division_truncates_toward_zero_and_remainder_has_dividend_sign(self):
        for left, right, quotient, remainder in (
            (-7, 3, -2, -1), (7, -3, -2, 1), (-7, -3, 2, -1), (7, 3, 2, 1),
        ):
            with self.subTest(left=left, right=right):
                self.assertEqual(evaluate_expression(f"{left} / {right}"), quotient)
                self.assertEqual(evaluate_expression(f"{left} % {right}"), remainder)

    def test_short_circuit_ternary_units_and_functions(self):
        examples = {
            "1 || UNKNOWN": 1,
            "0 && (1 / 0)": 0,
            "1 ? 2K : UNKNOWN": 2048,
            "0 ? 1 : 0 ? 2 : 3": 3,
            "1 ? 0 ? 2 : 3 : 4": 3,
            "ALIGN(17, 16) + MAX(1, MIN(5, 3))": 35,
            "ABSOLUTE(1M - 4K)": 1044480,
            "010 + 1": 9,
            "BASE + 4": 4100,
        }
        for expression, expected in examples.items():
            with self.subTest(expression=expression):
                self.assertEqual(evaluate_expression(expression, {"BASE": 4096}), expected)

    def test_unknown_and_unsafe_expressions_are_not_executed(self):
        for expression in (
            "UNKNOWN + 1", "__import__('os').system('echo unsafe')",
            "getattr(1, 2)", "1 << 1000000000", "2 ** 1000000000",
            "1 / 0", "1 << -1", "1 +", "1, 2", "08", "0xffffffffffffffff + 1",
            "-1 < 1", "-1 >> 1", "0xffffffffffffffff / 3",
            "(" * 100 + "1" + ")" * 100,
        ):
            with self.subTest(expression=expression):
                self.assertIsNone(evaluate_expression(expression))


class ScriptParsingTests(unittest.TestCase):
    def test_cross_file_alias_and_region_expressions(self):
        constants = parse_script('BASE = 0x1000; REGION_ALIAS("code", ram);')
        memory = parse_script(
            "MEMORY { ram (rx): ORIGIN = BASE, LENGTH = 4K\n"
            " next (rw): ORIGIN = ORIGIN(code) + LENGTH(code), LENGTH = 2K }"
        )
        resolve_regions([constants, memory])
        self.assertEqual(memory.regions[0].origin, 0x1000)
        self.assertEqual(memory.regions[1].origin, 0x2000)
        self.assertIs(region_lookup([constants, memory])["code"], memory.regions[0])
        self.assertEqual(evaluate_expression("LENGTH(code)", regions=region_lookup([constants, memory])), 4096)

    def test_same_line_sections_and_quoted_braces_do_not_change_boundaries(self):
        source = ('SECTIONS{.text:{ASSERT(1, "} { /* > evil"); *(.text)} >RAM '
                  '.data (NOLOAD):{/* } */ *(.data)} AT >FLASH >RAM :data }')
        script = parse_script(source)
        self.assertTrue(script.editable)
        self.assertEqual([item.name for item in script.sections], [".text", ".data"])
        self.assertEqual(script.sections[1].address_expr, "")
        self.assertEqual(script.sections[1].load_region, "FLASH")
        self.assertEqual(move_section(script, 1, "OTHER"), source.replace("AT >FLASH >RAM", "AT >FLASH >OTHER"))

    def test_comments_noop_and_unknown_memory_declaration_remain_exact(self):
        source = ("/* prefix */\r\nMEMORY { ram (rx): org=0x1000 /* base */ + 4, len=1K /* tail */\r\n"
                  " other : LENGTH=2K, ORIGIN=0x4000\r\n}\r\nPHDRS { text PT_LOAD; }\r\n")
        script = parse_script(source)
        self.assertEqual(script.source, source)
        self.assertEqual(len(script.regions), 1)
        self.assertTrue(script.diagnostics)
        origin = script.regions[0].origin_expr
        self.assertEqual(update_region(script, 0, origin, "1K"), source)
        self.assertEqual(update_region(script, 0, origin, "2K"), source.replace("len=1K", "len=2K"))

    def test_malformed_or_unprocessed_scripts_block_structured_edits(self):
        valid = "MEMORY { RAM: ORIGIN=0, LENGTH=4K } SECTIONS { .text: {} >RAM }"
        for source in (
            "#if ENABLED\n" + valid + "\n#endif", valid + " /* unclosed",
            valid + ' "unclosed', valid + " }", valid[:-1],
        ):
            with self.subTest(source=source):
                script = parse_script(source)
                self.assertFalse(script.editable)
                self.assertTrue(script.diagnostics)
                with self.assertRaises(ValueError):
                    add_region(script, "MORE", "rw", "0x2000", "1K")

    def test_multiple_memory_blocks_and_unresolved_destination_are_reported(self):
        script = parse_script("MEMORY { A: org=0, len=1K } MEMORY { B: org=2K, len=1K }\n"
                              "SECTIONS { .text: {} >MISSING }")
        self.assertEqual(len(script.regions), 2)
        self.assertTrue(any("MISSING" in message for message in validate_scripts([script])))


class StructuredEditingTests(unittest.TestCase):
    def test_lma_remains_unchanged_when_vma_is_inserted_or_moved(self):
        for suffix in ("AT>FLASH", "> RAM AT > FLASH :code =0xff"):
            with self.subTest(suffix=suffix):
                source = f"SECTIONS {{ .text : {{ KEEP(*(.text)) }} {suffix} }}"
                script = parse_script(source)
                changed = move_section(script, 0, "IRAM")
                result = parse_script(changed)
                self.assertEqual(result.sections[0].region, "IRAM")
                self.assertEqual(result.sections[0].load_region, "FLASH")
                self.assertEqual(result.sections[0].body, script.sections[0].body)
                if ":code" in suffix:
                    self.assertIn(":code =0xff", changed)

    def test_fixed_address_and_address_align_are_not_silently_relocated(self):
        for address in ("0x40080000", "ORIGIN(RAM)", "ALIGN(16)"):
            with self.subTest(address=address):
                script = parse_script(f"SECTIONS {{ .text {address} (NOLOAD): {{}} >RAM }}")
                self.assertEqual(script.sections[0].address_expr, address)
                self.assertEqual(move_section(script, 0, "RAM"), script.source)
                with self.assertRaises(ValueError):
                    move_section(script, 0, "OTHER")

    def test_unresolved_valid_expression_is_retained_but_malformed_tokens_rejected(self):
        script = parse_script("MEMORY { RAM: org=0, len=4K }")
        for expression in ("CONFIG_SIZE - 1K", "ALIGN(UNKNOWN + 1, 16)", "CONFIG_A ? 4K : 2K"):
            with self.subTest(expression=expression):
                changed = update_region(script, 0, "0", expression)
                self.assertIn(expression, changed)
                self.assertIsNone(parse_script(changed).regions[0].length)
        for expression in ("0, LENGTH = 1", "1 +", "0) * (", "0 SECTIONS", "0;}", "1/*"):
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError):
                    update_region(script, 0, "0", expression)

    def test_additions_roundtrip_and_body_edit_does_not_touch_other_sections(self):
        source = add_region(parse_script("/* empty */\n"), "RAM", "rwx", "0x1000", "4K")
        source = add_section(parse_script(source), ".text", "RAM", "    KEEP(*(.text))")
        source = add_section(parse_script(source), ".bss", "RAM", "    *(.bss)", noload=True)
        script = parse_script(source)
        self.assertEqual(len(script.regions), 1)
        self.assertEqual([item.name for item in script.sections], [".text", ".bss"])
        changed = update_section_body(script, 0, "\n    *(.text .text.*)\n  ")
        result = parse_script(changed)
        self.assertEqual(result.sections[1].header, script.sections[1].header)
        self.assertEqual(result.sections[1].body, script.sections[1].body)
        self.assertEqual(result.sections[1].region, script.sections[1].region)
        self.assertEqual(result.regions[0].origin, 0x1000)

    def test_body_escape_unclosed_comments_and_invalid_new_region_are_rejected(self):
        script = parse_script("SECTIONS { .text : { *(.text) } >RAM }")
        for body in ('} MEMORY {', '/* not closed', 'ASSERT(1, "not closed)', '#include "x"'):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    update_section_body(script, 0, body)
                with self.assertRaises(ValueError):
                    add_section(script, ".data", "RAM", body)
        for origin, length in (("-1", "1K"), ("0", "-1"), ("0xfffffff0", "32")):
            with self.subTest(origin=origin, length=length):
                with self.assertRaises(ValueError):
                    add_region(script, "RAM", "rw", origin, length)


if __name__ == "__main__":
    unittest.main()
