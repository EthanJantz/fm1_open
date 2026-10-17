# SPDX-License-Identifier: MIT
# Copyright (c) 2023 Andrey Grigoryev (kagaimiq), 2026 M-VAVE.
# JieLi formats and ciphers: jl-misctools 0a5b12d. FM1 layout and writer: M-VAVE.
# Python port of sdk/Package.cs for hosts without .NET. Full MIT terms and
# source links: THIRD_PARTY.md.
"""Write an FM1 `.fwsc` update package from the linked application image."""
import re
from pathlib import Path

AREA = 0x4000
ENTRY_POINT = 0x02000120
APP_DATA = 0x120
FLASH_LIMIT = 0xff000
USER_SIZE = 536576
BT_SIZE = 4096
MINIMUM_VM = 32768
PUBLIC_CODE = 0x980f


def _make_crc_table():
    table = []
    for i in range(256):
        value = i << 8
        for _ in range(8):
            value = ((value << 1) ^ (0x1021 if value & 0x8000 else 0)) & 0xffff
        table.append(value)
    return table


CRC_TABLE = _make_crc_table()


def crc(data, begin, length):
    value = 0
    for i in range(begin, begin + length):
        value = ((value << 8) ^ CRC_TABLE[(value >> 8) ^ data[i]]) & 0xffff
    return value


def cipher(data, begin, length, state):
    for i in range(begin, begin + length):
        data[i] ^= state & 0xff
        state = ((state << 1) ^ (0x1021 if state & 0x8000 else 0)) & 0xffff


def round_up(size, alignment):
    return (size + alignment - 1) & -alignment


def u16(data, offset):
    return int.from_bytes(data[offset:offset + 2], 'little')


def i32(data, offset):
    return int.from_bytes(data[offset:offset + 4], 'little', signed=True)


class Image:
    def __init__(self, size, fill):
        self.bytes = bytearray([fill]) * size

    def number(self, offset, value, width):
        self.bytes[offset:offset + width] = (value & ((1 << (8 * width)) - 1)).to_bytes(width, 'little')

    def copy(self, offset, data):
        self.bytes[offset:offset + len(data)] = data

    def text(self, offset, value):
        self.copy(offset, value.encode('ascii'))

    def checksum(self, field, offset, size):
        self.number(field, crc(self.bytes, offset, size), 2)

    def record(self, offset, name, crc_value, address, size, attributes, last, extra):
        self.bytes[offset:offset + 32] = bytes(32)
        self.number(offset + 2, crc_value, 2)
        self.number(offset + 4, address, 4)
        self.number(offset + 8, size, 4)
        self.number(offset + 12, attributes, 2)
        self.number(offset + 14, last, 2)
        self.bytes[offset + 16:offset + 32] = b'\xff' * 16
        self.text(offset + 16, name)
        self.bytes[offset + 16 + len(name)] = 0
        if extra is not None:
            self.number(offset + 28, extra, 4)
        self.checksum(offset, offset + 2, 30)


def read_resource(directory, name, size):
    data = (Path(directory) / name).read_bytes()
    if len(data) != size:
        raise ValueError('Unexpected FM1 resource: ' + name)
    return data


def encode_boot(plain):
    result = bytearray(plain)
    banks = u16(plain, 0)
    if banks < 1 or banks > len(plain) // 16:
        raise ValueError('Invalid boot directory.')
    previous_end = banks * 16
    for i in range(banks):
        header = i * 16
        length = u16(plain, header + 2)
        start = i32(plain, header + 8)
        if length == 0 or start < previous_end or start > len(plain) - length or \
                crc(plain, header, 14) != u16(plain, header + 14) or \
                crc(plain, start, length) != u16(plain, header + 12):
            raise ValueError('Invalid boot bank.')
        cipher(result, header, 16, 65535)
        cipher(result, start, length, 65535)
        previous_end = start + length
    return bytes(result)


def build_flash(application, directory, end, vm_end):
    boot = read_resource(directory, 'uboot.boot', 14488)
    config = read_resource(directory, 'boot-config.bin', 721)
    tool = read_resource(directory, 'cfg_tool.bin', 383)
    image = Image(end, 255)
    settings = Image(34 + len(config), 0)
    # Deterministic jl-misctools chipkeybin descriptor for the public image.
    for bit in range(16):
        settings.bytes[bit] = 16
        settings.bytes[16 + bit] = 16 ^ (255 if ((PUBLIC_CODE >> bit) & 1) == 0 else 0)
    settings.checksum(32, 0, 32)
    settings.copy(34, config)
    # AC791N boot header fields from the verified FM1 boot resources.
    image.copy(0, bytes([0, 0, 0x20, 2, 0x2f, 0x10, 0x4c, 0xc9, 0, 0xf0, 0x0f, 0, 0x10, 0x10, 0xa4, 0xff,
                         0x4e, 0x5d, 0x0b, 0x41, 0xe0, 0xcd, 0x78, 0x1d, 0xc8, 0x56, 0x41, 0x7f, 0xb3, 0x67, 0xcf, 0x9f]))
    image.checksum(0, 2, 30)
    config_at = 160 + len(boot)
    image.record(32, 'uboot.boot', crc(boot, 0, len(boot)), 160, len(boot), 0, 0, None)
    image.record(64, 'isd_config.ini', crc(settings.bytes, 0, len(settings.bytes)), config_at, len(settings.bytes), 0x8002, 0, None)
    image.record(96, 'app_dir_head', 65535, AREA, 0xffffffff, 0xff81, 0, None)
    image.record(128, 'key_mac', 65535, FLASH_LIMIT, 4096, 0x0112, 65535, None)
    for p in range(0, 160, 32):
        cipher(image.bytes, p, 32, 65535)
    image.copy(160, encode_boot(boot))
    image.copy(config_at, settings.bytes)
    payload_size = APP_DATA + len(application) + len(tool)
    app = AREA + APP_DATA
    image.copy(app, application)
    image.copy(app + len(application), tool)
    image.record(AREA + 32, 'app.bin', crc(application, 0, len(application)), APP_DATA, len(application), 0xff82, 0, None)
    image.record(AREA + 64, 'cfg_tool.bin', crc(tool, 0, len(tool)), APP_DATA + len(application), len(tool), 0xff82, 0, None)
    image.record(AREA + 96, 'VM', 65535, end, vm_end - end, 0x8112, 0, MINIMUM_VM)
    image.record(AREA + 128, 'PRCT', 65535, 0, end, 0x8292, 0, 0x45444f43)
    image.record(AREA + 160, 'BTIF', 65535, vm_end, BT_SIZE, 0x8192, 1, 0x4f545541)
    image.record(AREA, 'app_area_head', crc(image.bytes, AREA + 32, payload_size - 32), ENTRY_POINT, payload_size, 0xff83, 0, None)
    reserved = AREA + payload_size
    image.record(reserved + 32, 'USER', 65535, FLASH_LIMIT - USER_SIZE, USER_SIZE, 0x8192, 1, 0x4f545541)
    image.record(reserved, 'EXT_RESERVED', crc(image.bytes, reserved + 32, 32), 32, 64, 0xff93, 1, None)
    for p in range(0, payload_size + 64, 32):
        cipher(image.bytes, AREA + p, min(32, payload_size + 64 - p), PUBLIC_CODE ^ (p >> 2))
    return image


def wrap(flash, loader, product):
    if len(loader) < 1 or len(loader) >= 32768:
        raise ValueError('Invalid FM1 update loader.')
    loader_at = 1024 + round_up(len(flash.bytes), 32)
    total = loader_at + round_up(len(loader), 32)
    ufw = Image(total, 255)
    ufw.bytes[0:224] = bytes(224)
    files = [bytes(flash.bytes), loader]
    names = ['flash.bin', 'ota.bin']
    for i, data in enumerate(files):
        record = 64 + i * 80
        ufw.number(record, 0 if i == 0 else 100, 2)
        ufw.number(record + 2, i, 2)
        ufw.number(record + 4, crc(data, 0, len(data)), 2)
        ufw.number(record + 8, 1024 if i == 0 else loader_at, 4)
        ufw.number(record + 12, len(data), 4)
        ufw.number(record + 16, round_up(len(data), 32), 4)
        ufw.text(record + 64, names[i])
        cipher(ufw.bytes, record, 80, 65535)
    ufw.number(4, total, 4)
    ufw.number(8, 2, 2)
    ufw.number(10, 4, 2)
    ufw.number(12, 512, 4)
    ufw.text(16, 'AC791N')
    ufw.checksum(2, 64, 160)
    ufw.checksum(0, 2, 62)
    cipher(ufw.bytes, 0, 64, 65535)
    ufw.copy(1024, flash.bytes)
    ufw.copy(loader_at, loader)
    output = bytearray(total + 20)
    destination = 0
    for source in range(total):
        output[destination] = ufw.bytes[source]
        if source < 940 and (source + 1) % 47 == 0:
            index = source // 47
            destination += 1
            output[destination] = (ord(product[index]) + index + 1) & 0xff if index < len(product) else 125
        destination += 1
    return bytes(output)


def create(app, resources, output, user_bytes, product):
    """Package `app` (concatenated text/data/ram0_data/cache_ram_data) into `output`."""
    if product is None or not re.fullmatch(r'FM-1_8[0-9]{6}', product):
        raise ValueError('Invalid FM1 update identity.')
    if not app or len(app) > FLASH_LIMIT - USER_SIZE - BT_SIZE - MINIMUM_VM - AREA - APP_DATA - 383 - 64 or user_bytes != USER_SIZE:
        raise ValueError('Application does not fit the FM1 layout.')
    image_size = round_up(AREA + APP_DATA + len(app) + 383 + 64, 4096)
    user_at = FLASH_LIMIT - USER_SIZE
    vm_end = user_at - BT_SIZE
    if image_size > vm_end - MINIMUM_VM:
        raise ValueError('Insufficient FM1 settings space.')
    image = build_flash(app, resources, image_size, vm_end)
    package = wrap(image, (Path(resources) / 'ota.bin').read_bytes(), product)
    Path(output).write_bytes(package)
    return {
        'flash_bytes': 1048576, 'board_access': False, 'fits': True, 'packager': 'source',
        'FLASH_BIN_SIZE': image_size, 'FLASH_NEED_SIZE': FLASH_LIMIT, 'VM_START_ADDR': image_size, 'VM_END_ADDR': vm_end,
        'VM_REAL_SIZE': vm_end - image_size, 'BTIF_RESERVED_START': vm_end, 'BTIF_RESERVED_END': user_at,
        'USER_RESERVED_START': user_at, 'USER_RESERVED_END': FLASH_LIMIT, 'vm_minimum_bytes': MINIMUM_VM,
        'vm_extra_gap_bytes': vm_end - image_size - MINIMUM_VM, 'btif_bytes': BT_SIZE, 'user_bytes': USER_SIZE,
    }
