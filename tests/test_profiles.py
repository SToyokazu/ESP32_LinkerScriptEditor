"""Chip templates must use distinct address layouts and explain shared SRAM."""

import unittest

from linker_editor.profiles import CHIP_NAMES, create_template, detect_chip, profile_notes


class ProfileTests(unittest.TestCase):
    def test_supported_templates_identify_their_chip(self):
        self.assertEqual(CHIP_NAMES, ("ESP32", "ESP32-S3"))
        for chip in CHIP_NAMES:
            with self.subTest(chip=chip):
                template = create_template(chip)
                self.assertEqual(detect_chip(template), chip)
                self.assertIn("MEMORY", template)
                self.assertIn("SECTIONS", template)
                self.assertIn("完全な起動用スクリプトではありません", template)
                self.assertTrue(profile_notes(chip))

    def test_s3_template_reserves_iram_in_shared_sram(self):
        template = create_template("ESP32-S3")
        self.assertIn("0x40378000, LENGTH = 64K", template)
        self.assertIn("0x3FC98000, LENGTH = 0x43700", template)
        self.assertEqual(0x3FC98000 - 0x3FC88000, 64 * 1024)
        self.assertEqual(0x3FC98000 + 0x43700 - 0x3FC88000,
                         0x403CB700 - 0x40378000)
        self.assertIn("同じ物理 SRAM", profile_notes("ESP32-S3"))

    def test_detect_address_and_unknown(self):
        self.assertEqual(detect_chip("ORIGIN = 0x400D0020"), "ESP32")
        self.assertEqual(detect_chip("ORIGIN = 0x42000020"), "ESP32-S3")
        self.assertIsNone(detect_chip("MEMORY { RAM : ORIGIN = 0, LENGTH = 4K }"))
        with self.assertRaises(ValueError):
            create_template("ESP32-C6")


if __name__ == "__main__":
    unittest.main()
