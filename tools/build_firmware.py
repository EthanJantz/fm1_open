#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only WITH GPL-3.0-linking-exception
"""Build the FM1 firmware (and, with --package, the complete workbench).

Portable port of sdk/Build.ps1: runs on Linux, macOS and Windows with Python 3
and the JieLi PI32v2 clang 4.0.1 toolchain. On Windows the vendor's hash
contract for the toolchain binaries is enforced exactly as in Build.ps1. On
other hosts the pinned hashes describe Windows executables, so the toolchain is
checked by version and header content instead and the build identity records
`toolchain_verified: false`.

Differences from Build.ps1: file lists are sorted by ordinal path (PowerShell
sorts culture-aware), so the reported build identity may differ from a Windows
build of the same tree. The firmware image itself is unaffected.
"""
import argparse
import json
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_web  # noqa: E402
import fm1_package  # noqa: E402
from build_web import sha256_file, sha256_text, write_text  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SDK = ROOT / 'sdk'
PROFILE = 'dx7'
BUILD = ROOT / 'build' / PROFILE
OFFICIAL = ROOT / 'build' / 'official-web'
WINDOWS = os.name == 'nt'
# Vendor (Windows) names first, then the names JieLi's Linux package uses.
TOOL_NAMES = {
    'clang': ['clang.exe', 'clang'],
    'ld': ['pi32v2-ld.exe', 'pi32v2-ld', 'ld'],
    'gold': ['LLVMgold.dll', 'LLVMgold.so'],
    'nm': ['llvm-nm.exe', 'llvm-nm', 'nm'],
    'objcopy': ['llvm-objcopy.exe', 'llvm-objcopy', 'objcopy'],
}
REQUIRED_CONTRACT = ['bin/clang.exe', 'bin/pi32v2-ld.exe', 'bin/LLVMgold.dll', 'bin/llvm-nm.exe', 'bin/llvm-objcopy.exe']


class BuildError(SystemExit):
    pass


def run(program, arguments, cwd):
    result = subprocess.run([str(program)] + [str(a) for a in arguments], cwd=str(cwd))
    if result.returncode != 0:
        raise BuildError(f'{Path(program).name} failed ({result.returncode})')


def relative(path):
    return path.relative_to(ROOT).as_posix()


def ordinal_files(folder):
    return sorted((p for p in folder.rglob('*') if p.is_file()), key=lambda p: p.as_posix())


def locate_toolchain(explicit):
    toolchain = Path(explicit) if explicit else SDK / 'tools'
    toolchain = toolchain.resolve()
    if any((toolchain / n).exists() for n in TOOL_NAMES['clang']):
        toolchain = toolchain.parent
    if not any((toolchain / 'bin' / n).exists() for n in TOOL_NAMES['clang']) and \
            any((toolchain / 'pi32v2' / 'bin' / n).exists() for n in TOOL_NAMES['clang']):
        toolchain = toolchain / 'pi32v2'  # JieLi Linux package root
    tools = {}
    for key, names in TOOL_NAMES.items():
        for name in names:
            if (toolchain / 'bin' / name).exists():
                tools[key] = toolchain / 'bin' / name
                break
    if 'clang' not in tools:
        raise BuildError('Install the matching JieLi PI32v2 r3 toolchain and set FM1_PI32_TOOLCHAIN to its directory '
                         'containing bin/clang (bin/clang.exe on Windows). See README.md.')
    missing = [k for k in TOOL_NAMES if k not in tools]
    if missing:
        raise BuildError('Toolchain is missing: ' + ', '.join(TOOL_NAMES[k][0] for k in missing))
    return toolchain, tools


def verify_toolchain(manifest, toolchain, tools):
    """Return True when the vendor hash contract was verified byte for byte."""
    contract = manifest.get('external_toolchain')
    if not contract:
        return False
    if contract.get('schema') != 1 or not contract.get('files'):
        raise BuildError('Invalid external toolchain manifest.')
    for required in REQUIRED_CONTRACT:
        if required not in contract['files']:
            raise BuildError(f'Missing toolchain contract: {required}')
    for name, digest in contract['files'].items():
        if not re.fullmatch(r'(bin|lib)/[A-Za-z0-9_./-]+', name) or '..' in name.split('/') or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise BuildError('Invalid external toolchain entry.')
    exact = all((toolchain / n).exists() and sha256_file(toolchain / n) == d for n, d in contract['files'].items())
    if exact:
        return True
    if WINDOWS:
        for name, digest in contract['files'].items():
            file = toolchain / name
            if not file.exists() or sha256_file(file) != digest:
                raise BuildError(f'External toolchain does not match the SDK: {name}. Install the matching version listed in sdk/manifest.json.')
    # Non-Windows host: the pinned executables are Windows builds. Check the
    # compiler version and the toolchain headers (modulo line endings).
    version = subprocess.run([str(tools['clang']), '--version'], capture_output=True, text=True).stdout
    if 'clang version 4.0.1' not in version:
        first = version.strip().splitlines()[0] if version.strip() else 'unknown'
        raise BuildError('External toolchain is not JieLi clang 4.0.1: ' + first)
    for name, digest in contract['files'].items():
        if not name.startswith('lib/'):
            continue
        file = toolchain / name
        if not file.exists():
            raise BuildError(f'External toolchain does not match the SDK: {name}.')
        data = file.read_bytes()
        if not any(sha256_text(v.decode('latin-1')) == digest for v in (data, data.replace(b'\r\n', b'\n'), data.replace(b'\n', b'\r\n'))):
            raise BuildError(f'External toolchain does not match the SDK: {name}.')
    print(f'Warning: toolchain binaries at {toolchain} are not the hash-pinned Windows build; '
          'version and headers match JieLi PI32v2 clang 4.0.1.')
    return False


def verify_sdk(manifest, synth):
    if manifest.get('abi') != 131072:
        raise BuildError('This application requires the matching 128-frame SDK.')
    for name, digest in manifest['files'].items():
        full = (ROOT / name).resolve()
        if SDK not in full.parents or sha256_file(full) != digest:
            raise BuildError(f'SDK payload changed: {name}')
    for file in SDK.rglob('*'):
        if file.is_file():
            rel = relative(file)
            if rel != 'sdk/manifest.json' and rel not in manifest['files']:
                raise BuildError(f'Unexpected SDK file: {rel}')
    if synth.get('schema') != 4 or synth.get('api') != 7 or synth.get('kernel_abi') != 3 or synth.get('block_frames') != 128 or \
            synth.get('sample_rate') != 44100 or synth.get('sdk_block_sha256') != sha256_file(SDK / 'include' / 'fm1_block.h') or \
            synth.get('native_sha256') != sha256_file(SDK / 'lib' / 'libfm1_engine.a'):
        raise BuildError('Native DSP kernels and SDK audio contracts do not match. Install a matching SDK package.')
    if not re.search(r'(?m)^#define FM1_DSP_ABI 3U\s*$', (ROOT / 'app' / 'synth' / 'engine.h').read_text(encoding='utf-8')):
        raise BuildError('Unsupported native DSP ABI.')
    for name, digest in (synth.get('portable_contracts') or {}).items():
        if not re.fullmatch(r'[A-Za-z0-9_]+\.inc', name) or digest != sha256_file(ROOT / 'app' / 'synth' / name):
            raise BuildError('Fixed DSP coefficients differ from the native kernels. Matching kernels are required for this coefficient change.')
    if not synth.get('sram_required') or not synth.get('sram_optional'):
        raise BuildError('Native DSP SRAM contract is missing.')
    for name in list(synth['sram_required']) + list(synth['sram_optional']):
        if not re.fullmatch(r'[A-Za-z_][\w.$]*', name):
            raise BuildError('Invalid DSP SRAM symbol.')


def read_version():
    text = (ROOT / 'app' / 'version.h').read_text(encoding='utf-8')
    version = re.search(r'(?m)^#define FM1_RELEASE_VERSION "(V(?:0[1-9]|[1-9][0-9]{1,2}))"\s*$', text)
    if not version:
        raise BuildError('Set FM1_RELEASE_VERSION in app/version.h.')
    product = re.search(r'(?m)^#define FM1_UPDATE_PRODUCT "(FM-1_8[0-9]{6})"\s*$', text)
    if not product:
        raise BuildError('Set a compatible FM1_UPDATE_PRODUCT in app/version.h.')
    return version.group(1), product.group(1)


# ---------------------------------------------------------------------------
# ELF inspection (port of Test-Firmware)

NM_LINE = re.compile(r'^([0-9a-fA-F]+)[ \t]+(([0-9a-fA-F]+)[ \t]+)?([A-Za-z])[ \t]+([^\r]+)')
SRAM_BEGIN, SRAM_END = 0x01c00000, 0x01c80000


def test_firmware(tools, synth, variant, extra):
    elf = BUILD / 'sdk.elf'
    report = BUILD / 'firmware-report.json'
    write_text(report, '{"passed":false}')
    nm = subprocess.run([str(tools['nm']), '--print-size', str(elf)], capture_output=True, text=True)
    if nm.returncode != 0:
        raise BuildError('Cannot inspect ELF symbols.')
    info = {}
    heap = {}
    usb_midi = False
    for line in nm.stdout.splitlines():
        match = NM_LINE.match(line)
        if not match:
            continue
        name = match.group(5)
        address = int(match.group(1), 16)
        size = int(match.group(3), 16) if match.group(3) else None
        kind = match.group(4)
        info[name] = {'address': address, 'size': size, 'kind': kind}
        if re.match(r'^(__fat_|fat_sdfile_fat_ops$|jl_fat_vfs_ops$|nor_sdfile_vfs_ops$|sdfile_ext_vfs_ops$)', name):
            raise BuildError(f'Unused filesystem entered the fixed Flash runtime: {name}')
        if 'HEAP' in name or ('heap' in name and kind == 'A'):
            heap[name] = address
        if name.startswith('lv_'):
            raise BuildError(f'Unexpected LVGL runtime: {name}')
        if re.search(r'usb.*midi|midi.*usb', name):
            usb_midi = True

    def size_of(name):
        return info[name]['size'] or 0

    # Table entries are discovered by ranges, so native archives must retain them.
    registrations = [('initcall', 4, 4), ('early_initcall', 16, 4), ('late_initcall', 4, 4),
                     ('platform_initcall', 4, 4), ('module_initcall', 4, 4), ('sys_event_handler', 12, 12),
                     ('syscfg_ops', 84, 28), ('lp_target', 72, 8), ('sys_cpu_timer', 16, 16),
                     ('clock_critical_handler', 16, 8), ('vfs_ops', 120, 120),
                     ('audio_decoder', 56, 56), ('audio_hwaccel', 32, 32)]
    for name, minimum, stride in registrations:
        begin, end = f'_{name}_begin', f'_{name}_end'
        if begin not in info or end not in info:
            begin, end = f'{name}_begin', f'{name}_end'
        if begin not in info or end not in info:
            raise BuildError(f'Runtime registration range missing: {name}')
        size = info[end]['address'] - info[begin]['address']
        if size < minimum or size % stride:
            raise BuildError(f'Runtime registration table invalid: {name}')
        if name == 'vfs_ops' and size != 120:
            raise BuildError('Expected only the internal Flash filesystem registration.')
    required = ['fm1_r_7919135c9963', 'fm1_r_1f449c2c7b8f', 'vSoftwareInterruptISR', 'fm1_r_e7d3da61dac2', 'fm1_r_1993f9b103e3', 'fm1_r_05431712fb7b',
                'fm1_update_init', 'fm1_update_message', 'fm1_update_busy', 'fm1_build_id', 'fm1_flash_init',
                'sdfile_vfs_ops', 'sdfile_init', 'syscfg_vm_init', 'syscfg_btif_init',
                'wifi_get_xosc', 'wifi_get_mcs_dgain', 'wifi_get_pa_trim_data',
                'connectivity_wl82_bluetooth_start', 'ble_profile_init', 'att_server_init',
                'fm1_ble_set_role', 'fm1_ble_scan', 'fm1_ble_connect', 'fm1_ble_disconnect', 'fm1_ble_get_status', 'fm1_ble_get_device',
                'audio_app_usb_microphone_enable', 'audio_app_usb_speaker_enable',
                'connectivity_audio_usb_microphone_enable', 'connectivity_audio_usb_speaker_enable',
                'audio_app_bluetooth_open', 'sbc_decoder', 'sbc_hwaccel', 'avdtp_packet_handler', 'bt_get_local_name', 'bt_get_mac_addr',
                'fm1_bluetooth_configure', 'fm1_bluetooth_get_status',
                'groove_tuning_capture', 'groove_tuning_apply', 'groove_project_apply_edit']
    for name in required:
        if name not in info:
            raise BuildError(f'Required runtime symbol absent: {name}')
    # Incoming connections also work with inert client stubs. Require the
    # fixed SDK's real discovery paths for A2DP reconnect and BLE HOST.
    for name in ('sdp_send_cmd_iotl', 'sdp_master_packet_handler', 'gatt_client_discover_primary_services_by_uuid128'):
        if name not in info or info[name]['kind'] not in ('T', 't', 'N', 'n') or size_of(name) <= 16:
            raise BuildError(f'Bluetooth discovery implementation missing or stubbed: {name}')
    # The instrument and UI must retain their real callbacks.
    for name in ('fm1_r_dee1ea411566', 'fm1_r_d16405f329ed', 'fm1_r_f03cc16d330d', 'fm1_r_1993f9b103e3',
                 'fm1_io_trs_midi_enabled', 'fm1_r_05431712fb7b', 'fm1_r_0aa010fe1de1', 'fm1_r_33d5e5b63b6b', 'fm1_app_sysex'):
        # This vendor nm prints SRAM-section functions as N, Flash text as T.
        if name not in info or info[name]['kind'] not in ('T', 'N', 't', 'n') or not size_of(name):
            raise BuildError(f'Application callback was lost or replaced by a weak default: {name}')
    if size_of('fm1_build_id') != 32:
        raise BuildError('Browser update build identity must be exactly 32 bytes.')
    if 'fm1_update_identity' not in info or size_of('fm1_update_identity') != 27:
        raise BuildError('FM1 update identity must be exactly 27 bytes.')
    if not usb_midi:
        raise BuildError('USB MIDI runtime is missing.')
    # Check the fixed platform ABI.
    for name in ('CurrentTCB', 'highCurrentTCB', 'xIdleTaskHandle'):
        if name not in info or size_of(name) != 4:
            raise BuildError(f'Platform RTOS ABI is invalid: {name}')
    for name in ('__rt_local_irq_disable', '__rt_local_irq_enable', 'bt_lock_cnt',
                 'fm1_r_83b2de623a96', 'fm1_r_c327ea18da09', 'cpuSuspend', 'audio_app_worker', 'edr_name', 'ble_name',
                 'a2dp_source_event_handler', 'a2dp_source_sbc_codec', 'hfp_profile_init', 'hfp_ag_profile_init',
                 'spp_user_send_data', 'avrcp_player_event', 'hid_diy_regiest_callback', 'obex_connection_req',
                 'pan_cmd_io_ctrl', 'gatt_client_discover_primary_services', 'ancs_client_init', 'ams_client_init',
                 'sdio_host_init', 'wl30_sdio_claim_irq', 'wl30_sdio_release_irq', 'wifi_on', 'wifi_off',
                 'wifi_rx_task', 'wifi_tx_task', 'ui_yuv420_to_rgb565_pairs', 'ui_tiny_ttf_pack_scanline'):
        if name in info:
            raise BuildError(f'Unsupported platform runtime entered FM1: {name}')
    link_map = (BUILD / 'sdk.map').read_text(encoding='utf-8', errors='replace')
    if variant['frozen'] + '(' not in link_map:
        raise BuildError('Fixed native SDK archive missing.')
    if 'libfm1_runtime.a(' in link_map:
        raise BuildError('Only the fixed music/BLE SDK may be linked.')
    for item in ('/system.a(', 'audio_server.a(', 'media_app.a(', 'btstack.a(', 'lib_ui.a(', 'libjpeg_decode_only.a('):
        if item in link_map:
            raise BuildError(f'Unsupported FM1 link input: {item}')
    for member in ('a2dp_source', 'ams_client', 'ancs_client', 'avctp', 'avrcp', 'bip', 'bnep', 'bt_audio',
                   'client_user', 'hfp_ag_profile', 'hfp_profile', 'hid', 'iap_profile', 'obex', 'opp', 'pan',
                   'pbg_profile', 'rfcomm', 'spp_profile', 'spp_update_profile', 'spp_user', 'vCard'):
        if f'.a({member}.c.o)' in link_map:
            raise BuildError(f'Unsupported FM1 link member: {member}')
    acl = {}
    for direction in ('tx', 'rx'):
        begin = f'acl_{direction}_pool'
        end = begin + '_end'
        if begin not in info or end not in info:
            raise BuildError(f'Required controller pool missing: {begin}')
        acl[direction] = info[end]['address'] - info[begin]['address']
    if acl['tx'] < 1024 or acl['rx'] < 4096:
        raise BuildError('Classic ACL pools too small; need 1024/4096 bytes.')
    heap_match = re.search(r'0x([0-9a-fA-F]+)[^\r\n]*_MALLOC_SIZE[ ]*=', link_map)
    if not heap_match:
        raise BuildError('Cannot find runtime heap in link map.')
    heap_bytes = int(heap_match.group(1), 16)
    if heap_bytes < 49152:
        raise BuildError(f'FM1 runtime heap is {heap_bytes} bytes; need at least 49152.')
    callbacks = ['fm1_r_e7d3da61dac2', 'fm1_r_1993f9b103e3', 'fm1_r_05431712fb7b', 'fm1_r_ffe3f82683f6', 'fm1_audio_irq', 'fm1_flash_worker_prepare',
                 'fm1_r_7e12ed19990d', 'fm1_r_74310ca1fed9',
                 'fm1_engine_render', 'fm1_engine_output_level', 'fm1_audio_copy', 'fm1_output_mix',
                 'fm1_usb_audio_read', 'fm1_bluetooth_audio_read', 'fm1_usb_audio_write', 'connectivity_audio_get_speaker_volume',
                 'connectivity_audio_get_mic_volume'] + list(extra)
    for name in list(synth['sram_optional']) + list(synth.get('native_sram_optional') or []):
        if name in info:
            callbacks.append(name)
    callbacks += ['cpu_enter_sfc_critical', 'cpu_exit_sfc_critical', 'enter_spi_code', 'exit_spi_code', 'fm1_r_da3931a5b7a8', 'sfc_drop_cache']
    # emu2149 helpers can inline into the mandatory psg_* SRAM callbacks.
    for name in ('audio_app_usb_mix', 'audio_app_usb_record', 'audio_app_usb_replace_input',
                 'render_stereo', 'effects_render', 'effects_sample', 'sequence_event', 'clock_publish',
                 'groove_arp_event', 'groove_arp_running', 'engine_note'):
        if name in info:
            callbacks.append(name)
    engine_ready = 'fm1_r_0aa010fe1de1' in info and info['fm1_r_0aa010fe1de1']['address'] > 0
    if engine_ready:
        callbacks.append('fm1_r_0aa010fe1de1')
        if 'fm1_r_33d5e5b63b6b' in info and info['fm1_r_33d5e5b63b6b']['address'] > 0:
            callbacks.append('fm1_r_33d5e5b63b6b')
    for name in callbacks:
        if name not in info:
            raise BuildError(f'Audio callback absent: {name}')
        if not SRAM_BEGIN <= info[name]['address'] < SRAM_END:
            raise BuildError(f'Audio callback is outside internal SRAM: {name}')
    for name in ('fm1_input_tick', 'fm1_panel_irq', 'fm1_scan_decode', 'fm1_key_pin', 'fm1_encoder_pin', 'fm1_quadrature'):
        if name not in info or not SRAM_BEGIN <= info[name]['address'] < SRAM_END:
            raise BuildError(f'Panel scan code or wiring table is outside internal SRAM: {name}')

    data = elf.read_bytes()

    def check_range(offset, count):
        if offset < 0 or count < 0 or offset > len(data) or count > len(data) - offset:
            raise BuildError('Truncated ELF.')

    def u16(offset):
        check_range(offset, 2)
        return struct.unpack_from('<H', data, offset)[0]

    def u32(offset):
        check_range(offset, 4)
        return struct.unpack_from('<I', data, offset)[0]

    check_range(0, 52)
    if data[:6] != b'\x7fELF\x01\x01':
        raise BuildError('Expected ELF32 little endian.')
    entry = u32(24)
    if entry != 0x02000120 or '_start' not in info or info['_start']['address'] != entry or \
            '_text_rodata_begin' not in info or info['_text_rodata_begin']['address'] != entry:
        raise BuildError('Firmware startup must match the fixed ROM entry and the first packaged instruction.')
    table, stride, count, strings_index = u32(32), u16(46), u16(48), u16(50)
    if stride < 40 or not count or strings_index >= count:
        raise BuildError('Invalid ELF section table.')
    check_range(table, stride * count)
    strings_header = table + strings_index * stride
    strings_offset, strings_bytes = u32(strings_header + 16), u32(strings_header + 20)
    check_range(strings_offset, strings_bytes)
    sections = []
    payload = app_image = 0
    for i in range(count):
        header = table + i * stride
        name_offset, kind, flags = u32(header), u32(header + 4), u32(header + 8)
        address, offset, size = u32(header + 12), u32(header + 16), u32(header + 20)
        if kind != 8:
            check_range(offset, size)
        if not (flags & 2) or not size:
            continue
        if name_offset >= strings_bytes:
            raise BuildError('ELF section name is outside string table.')
        start = end = strings_offset + name_offset
        limit = strings_offset + strings_bytes
        while end < limit and data[end]:
            end += 1
        if end >= limit:
            raise BuildError('Unterminated ELF section name.')
        name = data[start:end].decode('ascii')
        if not re.fullmatch(r'\.(text|data|bss|ram0_data|ram0_bss|boot_info|cache_ram_data|cache_ram_bss)', name):
            raise BuildError(f'Unsupported allocated section: {name}')
        nobits = kind == 8
        if not nobits:
            payload += size
            if re.fullmatch(r'\.(text|data|ram0_data|cache_ram_data)', name):
                app_image += size
            elif name != '.boot_info':
                raise BuildError(f'Allocated section is not covered by FM1 packaging: {name}')
        sections.append({'name': name, 'address': address, 'bytes': size, 'nobits': nobits})
    result = {'passed': True, 'elf_sha256': sha256_file(elf), 'allocated_image_bytes': payload, 'app_image_bytes': app_image,
              'startup_entry_verified': True, 'startup_entry': entry,
              'instrument_ready': engine_ready,
              'image_definition': 'Sum of ELF allocated non-NOBITS sections; excludes bootloader/filesystem packaging',
              'sections': sections, 'heap_symbols': heap, 'runtime_heap_bytes': heap_bytes, 'minimum_runtime_heap_bytes': 49152,
              'classic_acl_tx_bytes': acl['tx'], 'classic_acl_rx_bytes': acl['rx'], 'registration_tables_verified': True,
              'rtos_abi_verified': True, 'audio_callbacks_in_sram': True, 'extra_audio_symbols_in_sram': list(extra),
              'bluetooth_and_usb_midi_linked': True, 'usb_audio_playback_and_capture_linked': True, 'bluetooth_audio_absent': False,
              'ble_roles': 'peripheral/central', 'lvgl_absent': True, 'active_bluetooth_discovery_linked': True,
              'application_callbacks_verified': True, 'board_validation': 'not performed'}
    write_text(report, json.dumps(result, indent=2))
    print(f'FM1: {app_image} bytes app image; SRAM/BLE/USB checks passed')


def package_firmware(tools, variant, product):
    generated = BUILD / 'generated'
    elf = BUILD / 'sdk.elf'
    before = sha256_file(elf)
    output = generated / 'FM1_OFFICIAL_OPEN.fwsc'
    complete = False
    try:
        if struct.unpack_from('<I', elf.read_bytes(), 24)[0] != 0x02000120:
            raise BuildError('Package entry must match the verified FM1 firmware entry.')
        image = bytearray()
        for section in ('text', 'data', 'ram0_data', 'cache_ram_data'):
            run(tools['objcopy'], ['-O', 'binary', '-j', '.' + section, str(elf), f'{section}.bin'], cwd=generated)
            image += (generated / f'{section}.bin').read_bytes()
        try:
            layout = fm1_package.create(bytes(image), SDK / 'tools' / 'pack', output, variant['user_bytes'], product)
        except ValueError as error:
            raise BuildError(str(error))
        if not output.exists() or sha256_file(elf) != before:
            raise BuildError('Firmware packaging did not produce a matching image.')
        write_text(BUILD / 'package-report.json', json.dumps(layout, indent=2))
        print(f"Offline package fits 1 MiB: image {layout['FLASH_BIN_SIZE']}, VM {layout['VM_REAL_SIZE']}, user {layout['user_bytes']} bytes")
        complete = True
    finally:
        for name in ('text.bin', 'data.bin', 'ram0_data.bin', 'cache_ram_data.bin'):
            if (generated / name).exists():
                (generated / name).unlink()
        if not complete and output.exists():
            output.unlink()


# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--package', action='store_true', help='also build the firmware package and complete workbench')
    parser.add_argument('--toolchain', default=os.environ.get('FM1_PI32_TOOLCHAIN') or None,
                        help='JieLi PI32v2 toolchain directory (default: $FM1_PI32_TOOLCHAIN)')
    parser.add_argument('--source-url', default=os.environ.get('FM1_SOURCE_URL'),
                        help='HTTPS URL of this build\'s source archive, required with --package (default: $FM1_SOURCE_URL)')
    parser.add_argument('--wasi-sdk', default=os.environ.get('FM1_WASI_SDK') or None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    # Invalidate stale successful output before validating build inputs.
    BUILD.mkdir(parents=True, exist_ok=True)
    write_text(BUILD / 'firmware-report.json', '{"passed":false}')
    write_text(BUILD / 'package-report.json', '{"board_access":false,"fits":false}')
    write_text(BUILD / 'web-package-report.json', '{"integrated":false,"board_access":false}')
    for name in ('build/dx7/update.html', 'build/site/index.html', 'build/site/.nojekyll',
                 'build/dx7/sdk.elf', 'build/dx7/sdk.map', 'build/dx7/build-identity.json',
                 'build/dx7/compile_commands.json', 'build/dx7/link-arguments.json',
                 'build/official-web/SHA256SUMS.txt'):
        if (ROOT / name).exists():
            (ROOT / name).unlink()
    old_web = OFFICIAL / 'FM1-Official-Open-Source.html'
    if old_web.exists():
        write_text(old_web, '<!doctype html><meta charset="utf-8"><title>FM1</title><p>Build incomplete. '
                            'Run the package build successfully before distributing this page.</p>')
    for name in ('FM1_OFFICIAL_OPEN.fw', 'FM1_OFFICIAL_OPEN.fwsc'):
        if (BUILD / 'generated' / name).exists():
            (BUILD / 'generated' / name).unlink()
    if args.package:
        build_web.validate_source_url(args.source_url)
    release_version, product = read_version()
    print(f'Build FM1 Official Open {release_version}')
    toolchain, tools = locate_toolchain(args.toolchain)
    bin_dir = toolchain / 'bin'
    manifest = json.loads((SDK / 'manifest.json').read_text(encoding='utf-8'))
    toolchain_verified = verify_toolchain(manifest, toolchain, tools)
    synth = json.loads((SDK / 'synth.json').read_text(encoding='utf-8'))
    verify_sdk(manifest, synth)

    variant_name = 'release'
    variant = {'user_bytes': 536576, 'frozen': 'sdk/lib/libfm1_engine.a', 'update_family': 7}
    include = ['sdk/include', 'sdk/include/libc', 'app/synth', 'sdk/config', 'app', 'app/board']
    app_synth = (ROOT / 'app' / 'synth').resolve()
    sources = [relative(p) for p in ordinal_files(ROOT / 'app')
               if p.suffix in ('.c', '.S') and app_synth not in p.resolve().parents]
    extra = ['groove_sequence_render', 'groove_sequence_clock', 'groove_sequence_position', 'groove_sequence_transport',
             'fm1_sea_decode_groups', 'fm1_codec_sea_mono_s16_run'] + list(synth['sram_required']) + [
        'synth_dx7_render', 'synth_dx7_midi', 'synth_dx7_key', 'fm1_dx7_panic', 'fm_exp2_fixed', 'bass303_bank_render', 'bass303_bank_key',
        'bass303_bank_set', 'drum808_kit_render', 'drum808_kit_trigger', 'psg_render', 'psg_key', 'psg_midi', 'psg_panic', 'psg_set',
        'psg_get', 'pluck_render', 'pluck_key', 'pluck_midi', 'pluck_panic', 'pluck_set', 'pluck_get', 'fm1_mix128',
        'fm1_mix_mono_stereo128', 'fm1_bus_output128', 'fm1_master_mix128', 'fm1_master_gain', 'fm1_declick128', 'fm1_render_begin',
        'fm1_render_end', 'synth_dx7_active', 'bass303_bank_active', 'drum808_kit_active', 'psg_active', 'pluck_active',
        'sampledrum_active', 'synth_dx7_limit', 'psg_limit', 'pluck_limit', 'sampledrum_limit', 'bass303_advance_idle', 'fm1_send128',
        'fm1_fx_render', 'fm1_fx_clear', 'fm1_fx_active', 'sampledrum_render', 'sampledrum_trigger', 'sampledrum_panic', 'psg_active_chips']
    (BUILD / 'obj').mkdir(parents=True, exist_ok=True)
    (BUILD / 'generated').mkdir(parents=True, exist_ok=True)
    # All application units are rebuilt. Remove objects for deleted/renamed sources.
    object_directory = (BUILD / 'obj').resolve()
    if (ROOT / 'build').resolve() not in object_directory.parents:
        raise BuildError('Invalid object directory.')
    for file in object_directory.iterdir():
        if file.is_file():
            file.unlink()
    flags = ['-target', 'pi32v2', '-integrated-as', '-mcpu=r3', '-mfprev1', '-Wuninitialized',
             '-Wno-invalid-noreturn', '-fno-common', '-flto', '-fallow-pointer-null', '-fprefer-gnu-section',
             '-femulated-tls', '-Wno-shift-negative-value', '-Wframe-larger-than=2560', '-fms-extensions',
             '-fno-unwind-tables', '-ffunction-sections', '-fdata-sections', '-fmessage-length=0',
             '-cl-single-precision-constant', '-mllvm', '-pi32v2-large-program=true', '-D__GCC_PI32V2__', '-O2']
    flags += ['-I' + str(ROOT / d) for d in include]
    objects = []
    commands = []
    for index, source in enumerate(sources):
        input_file = ROOT / source
        # The index keeps nested paths and non-ASCII names from colliding after sanitising.
        output = f"obj/{index}_{re.sub(r'[^a-zA-Z0-9._-]', '_', source)}.obj"
        options = list(flags)
        # This PI32 LTO plugin can discard application definitions when a
        # native SDK object has weak defaults. Keep user entry units native.
        if source in ('app/screen.c', 'app/transfer.c'):
            options.append('-fno-lto')
        if source == 'app/board/panel.c':
            options += ['-fno-lto', '-fno-pic']
        options += ['-fwrapv', '-fno-fast-math', '-ffp-contract=off']
        # Storage and screen run only from control; keep audio DSP on O2.
        if source in ('app/project.c', 'app/transfer.c', 'app/samples.c', 'app/screen.c', 'app/presets.c'):
            options.append('-Oz')
        arguments = options + ['-MMD', '-MF', output + '.d', '-c', str(input_file), '-o', output]
        print(f'Compile {source}')
        run(tools['clang'], arguments, cwd=BUILD)
        objects.append(output)
        commands.append({'directory': str(BUILD), 'file': str(input_file), 'output': str(BUILD / output),
                         'arguments': [str(tools['clang'])] + arguments})
    # The device reports this source/SDK build identity after a browser update.
    # It is distinct from the final package SHA-256 (which covers every byte).
    identity_inputs = [PROFILE, sha256_file(SDK / 'manifest.json')]
    identity_inputs += [f'{relative(f)}:{sha256_file(f)}' for f in ordinal_files(ROOT / 'app')]
    build_identity = sha256_text('\n'.join(identity_inputs))
    id_bytes = ['0x' + build_identity[i:i + 2] for i in range(0, 64, 2)]
    if variant['update_family'] != 7 or variant['user_bytes'] < 4096 or variant['user_bytes'] % 4096:
        raise BuildError('Invalid fixed firmware layout.')
    identity_source = ('#include "layout.h"\n'
                       f"const struct fm1_firmware_layout fm1_layout = {{{variant['user_bytes']}U,{variant['update_family']}U}};\n"
                       f"const unsigned char fm1_build_id[32] = {{{','.join(id_bytes)}}};\n"
                       f'const unsigned char fm1_update_identity[27] = "{product}";\n')
    write_text(BUILD / 'generated' / 'build_identity.c', identity_source)
    run(tools['clang'], flags + ['-c', 'generated/build_identity.c', '-o', 'obj/build_identity.obj'], cwd=BUILD)
    objects.append('obj/build_identity.obj')
    write_text(BUILD / 'build-identity.json', json.dumps({
        'profile': PROFILE, 'family': variant_name, 'release_version': release_version, 'build_id': build_identity,
        'toolchain': str(toolchain), 'toolchain_verified': toolchain_verified, 'host': sys.platform}, indent=2))
    linker = (SDK / 'config' / 'linker.ld.in').read_text(encoding='utf-8') \
        .replace('@SBC_DECODER@', 'sbc_decoder\n').replace('@ACL_TX_EXTRA@', ' + 1024').replace('@ACL_RX_EXTRA@', ' + 4096')
    write_text(BUILD / 'generated' / 'linker', linker)
    link = ['--plugin', str(tools['gold']), '-o', 'sdk.elf'] + objects + [
        '-flto', '--plugin-opt=-pi32v2-always-use-itblock=false', '--plugin-opt=-enable-ipra=true',
        '--plugin-opt=-pi32v2-merge-max-offset=4096', '--plugin-opt=-pi32v2-enable-simd=true', '--plugin-opt=mcpu=r3',
        '--plugin-opt=-global-merge-on-const', '--plugin-opt=-inline-threshold=5', '--plugin-opt=-inline-max-allocated-size=32',
        '--plugin-opt=-inline-normal-into-special-section=true',
        '--plugin-opt=-dont-used-symbol-list=malloc,free,sprintf,printf,puts,putchar',
        '--plugin-opt=-pi32v2-enable-rep-memop', '--plugin-opt=-emulated-tls', '--sort-common',
        '--plugin-opt=-pi32v2-large-program=true', '--plugin-opt=-pi32v2-csync-before-rti=true', '--start-group',
        '--whole-archive', '../../' + variant['frozen'], '--no-whole-archive',
        '../../sdk/lib/libc.a', '../../sdk/lib/libcompiler_rt.a',
        '--end-group', '--gc-sections', '-Tgenerated/linker', '-M=sdk.map', '--undefined=connectivity_wl82_bluetooth_start',
        '--undefined=audio_app_bluetooth_open', '--undefined=bt_get_local_name', '--undefined=bt_get_mac_addr']
    write_text(BUILD / 'compile_commands.json', json.dumps(commands, indent=2))
    write_text(BUILD / 'link-arguments.json', json.dumps(link, indent=2))
    print(f'Link {PROFILE} using prebuilt FM1 SDK')
    run(tools['ld'], link, cwd=BUILD)
    test_firmware(tools, synth, variant, extra)
    if args.package:
        package_firmware(tools, variant, product)
        package_file = BUILD / 'generated' / 'FM1_OFFICIAL_OPEN.fwsc'
        package_bytes = package_file.read_bytes()
        # Original V15 recovery image, separate from the Open release.
        stock_file = SDK / 'firmware' / 'FM-1_015.fwsc'
        stock_hash = 'db1642b2b6fa5c2cccb11ffd13878068bb28601678d3644049f99dc40e7edb8a'
        if not stock_file.exists() or sha256_file(stock_file) != stock_hash:
            raise BuildError('Matching SDK original V15 recovery firmware is missing or invalid.')
        stock_bytes = stock_file.read_bytes()
        import base64
        stock = {'schema': 1, 'kind': 'stock', 'product': 'FM-1_015', 'profile': 'M-VAVE Original V15', 'family': 7, 'release_version': 'V15',
                 'build_id': stock_hash, 'sha256': stock_hash, 'bytes': len(stock_bytes),
                 'loader_sha256': 'ef1f2db89b011fa53e71df363f0122de0ea896618c53fb53e8da676322004541',
                 'data': base64.b64encode(stock_bytes).decode('ascii')}
        web_package = json.dumps({
            'schema': 1, 'product': product, 'profile': PROFILE, 'family': variant['update_family'], 'release_version': release_version,
            'build_id': build_identity, 'sha256': sha256_file(package_file), 'bytes': len(package_bytes),
            'loader_sha256': sha256_file(SDK / 'tools' / 'pack' / 'ota.bin'), 'hardware_validated': False,
            'data': base64.b64encode(package_bytes).decode('ascii'), 'stock': stock}, separators=(',', ':'))
        web = build_web.build(args.wasi_sdk, web_package, args.source_url)
        write_text(BUILD / 'web-package-report.json', json.dumps({
            'integrated': True, 'firmware_sha256': sha256_file(package_file), 'web_sha256': web['sha256'],
            'release_version': release_version, 'build_id': build_identity, 'simulator_sha256': web['simulator_sha256'],
            'simulator_source_sha256': web['simulator_source_sha256'], 'simulator_bytes': web['simulator_bytes'],
            'hardware_validated': False, 'board_access': False}, indent=2))
    print(f'Firmware: {BUILD / "sdk.elf"}')


if __name__ == '__main__':
    main()
