"""ESP32 / ESP32-S3 の新規編集用テンプレート。

アドレスは ESP-IDF v6.1 の memory.ld.in を参照する。テンプレートは
独立したリンカ配置の編集開始点であり、ESP-IDF の起動契約を実装しない。
"""

from __future__ import annotations

import re


CHIP_NAMES = ("ESP32", "ESP32-S3")

_NOTES = {
    "ESP32": (
        "ESP32: IRAM と DRAM の値は ESP-IDF v6.1 の基本配置を参照しています。"
        "Bluetooth・トレース・ROM・ブートローダー・ULP の予約領域は設定で変化します。"
        "RTC FAST の命令側 0x400C0000 とデータ側 0x3FF80000 は同じ物理メモリです。"
        "フラッシュ領域の LENGTH は仮想アドレス窓であり、搭載フラッシュ容量やアプリパーティション容量ではありません。"
        "既存 ESP-IDF プロジェクトでは生成済み memory.ld と sections.ld を読み込んでください。"
    ),
    "ESP32-S3": (
        "ESP32-S3: IRAM と DRAM は同じ物理 SRAM を別アドレスから参照します。"
        "新規テンプレートでは I-cache 32 KiB を前提に、先頭 64 KiB を IRAM、残りを DRAM に分割しています。"
        "IRAM を拡張する場合は DRAM 側も調整が必要です。両方の全容量を独立して使用することはできません。"
        "RTC FAST は命令とデータで同一アドレスです。"
        "フラッシュ領域の LENGTH は仮想アドレス窓です。PSRAM・キャッシュ・ブートローダー・ULP の予約条件は設定依存です。"
        "既存 ESP-IDF プロジェクトでは生成済み memory.ld と sections.ld を読み込んでください。"
    ),
}


def profile_notes(chip: str) -> str:
    """選択チップのメモリ共有・予約に関する説明を返す。"""
    if chip not in CHIP_NAMES:
        raise ValueError(f"未対応のチップです: {chip}")
    return _NOTES[chip]


def detect_chip(text: str) -> str | None:
    """チップ名、ターゲット識別子、代表アドレスから候補を推定する。"""
    lowered = text.lower()
    if re.search(r"esp32[-_]?s3\b|0x403[78][0-9a-f]{4}\b|0x42000020\b|0x600fe000\b", lowered):
        return "ESP32-S3"
    if re.search(r"\besp32\b|idf_target_esp32\b|0x40080000\b|0x400d0020\b", lowered):
        return "ESP32"
    return None


def create_template(chip: str) -> str:
    """起動処理を含まない GNU ld の新規配置テンプレートを返す。"""
    profile_notes(chip)  # Validate before selecting an address layout.
    if chip == "ESP32":
        memory = """    iram0_0_seg (RX) : ORIGIN = 0x40080000, LENGTH = 128K
    iram0_2_seg (RX) : ORIGIN = 0x400D0020, LENGTH = 0x330000 - 0x20
    dram0_0_seg (RW) : ORIGIN = 0x3FFB0000, LENGTH = 0x2C200
    drom0_0_seg (R)  : ORIGIN = 0x3F400020, LENGTH = 4M - 0x20
    rtc_iram_seg (RX) : ORIGIN = 0x400C0000, LENGTH = 8K
    rtc_slow_seg (RW) : ORIGIN = 0x50000000, LENGTH = 8K - 24"""
        detail = """ * Bluetooth / trace / ULP / bootloader RTC reservations are not enabled here.
 * The final 24 bytes of RTC SLOW are excluded for the RTC timer.
 * RTC FAST data alias 0x3FF80000 shares the block at 0x400C0000.
 * This template uses RTC FAST for code only to avoid alias double allocation."""
    else:
        memory = """    /* Shared SRAM split: 64 KiB instruction view, then data view. */
    iram0_0_seg (RX) : ORIGIN = 0x40378000, LENGTH = 64K
    iram0_2_seg (RX) : ORIGIN = 0x42000020, LENGTH = 8M - 0x20
    dram0_0_seg (RW) : ORIGIN = 0x3FC98000, LENGTH = 0x43700
    drom0_0_seg (R)  : ORIGIN = 0x3C000020, LENGTH = 32M - 0x20
    rtc_iram_seg (RX) : ORIGIN = 0x600FE000, LENGTH = 8K - 24
    rtc_slow_seg (RW) : ORIGIN = 0x50000000, LENGTH = 8K"""
        detail = """ * I-cache: 32 KiB. IRAM/DRAM are views of shared physical SRAM.
 * 0x40378000 maps to 0x3FC88000. This example assigns its first 64 KiB
 * to IRAM and starts DRAM at 0x3FC98000 to prevent physical overlap.
 * Static SRAM ends at instruction view 0x403CB700 / data view 0x3FCDB700.
 * Changing the IRAM length requires changing the DRAM origin/length too.
 * RTC FAST excludes 24 bytes for the timer. ULP / extra bootloader RTC
 * reservation and PSRAM are not configured in this example."""
    return f"""/* {chip} - new linker layout
 *
 * 新規配置の編集用テンプレートです。ESP-IDF の完全な起動用スクリプトではありません。
 * 既存アプリにはビルド済み memory.ld と sections.ld を読み込んでください。
 * 起動ベクタ、初期化シンボル、ROM 定義、アプリヘッダ、フラッシュの
 * セグメント整列などは利用する起動コードとビルドシステムに合わせて追加してください。
 *
 * Flash LENGTH values describe virtual windows, not installed flash size
 * or the app partition. Verify partition size and image layout separately.
{detail}
 * Reference: ESP-IDF v6.1 components/esp_system/ld/
 * https://github.com/espressif/esp-idf/tree/v6.1/components/esp_system/ld
 */

MEMORY
{{
{memory}
}}

SECTIONS
{{
    .iram0.text :
    {{
        . = ALIGN(4);
        *(.iram1 .iram1.*)
        . = ALIGN(4);
    }} > iram0_0_seg

    .flash.text :
    {{
        . = ALIGN(4);
        *(.literal .literal.* .text .text.*)
        . = ALIGN(4);
    }} > iram0_2_seg

    .flash.rodata :
    {{
        . = ALIGN(4);
        *(.rodata .rodata.*)
        . = ALIGN(4);
    }} > drom0_0_seg

    .dram0.data :
    {{
        . = ALIGN(4);
        *(.data .data.*)
        . = ALIGN(4);
    }} > dram0_0_seg

    .dram0.bss (NOLOAD) :
    {{
        . = ALIGN(4);
        *(.bss .bss.*)
        *(COMMON)
        . = ALIGN(4);
    }} > dram0_0_seg

    .rtc.text :
    {{
        . = ALIGN(4);
        *(.rtc.literal .rtc.text .rtc.text.*)
        . = ALIGN(4);
    }} > rtc_iram_seg

    .rtc.data :
    {{
        . = ALIGN(4);
        *(.rtc.data .rtc.data.* .rtc.rodata .rtc.rodata.*)
        . = ALIGN(4);
    }} > rtc_slow_seg

    .rtc.bss (NOLOAD) :
    {{
        . = ALIGN(4);
        *(.rtc.bss .rtc.bss.*)
        . = ALIGN(4);
    }} > rtc_slow_seg
}}
"""
