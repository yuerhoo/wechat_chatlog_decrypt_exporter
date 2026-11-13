# -*- coding: utf-8 -*-
"""设备 ID：从硬件信息算一个稳定的指纹，用来回答「这是哪台机器」

登录日志（login_log.device_id）和导出日志（export_log.device_id）都用它，
配合 host（机器名）和 IP 一起判断：
    · 同一个 device_id 反复出现 → 还是那台机器
    · device_id 变了 → 换电脑了 / 换硬件了
host 是用户自己起的名字（DESKTOP-XXX），device_id 是硬件说了算，谁也改不了。

⚠️ 2026-11-13 改版：彻底不再起子进程
--------------------------------------------------------------------
旧实现跑两次 `wmic`（csproduct get UUID / cpu get ProcessorId），有两个硬伤：
    · 打包成 --windowed 的 GUI 程序后，每次调用都会闪一个黑色 cmd 控制台窗口，
      启动时连闪两次；WMI 首次初始化还要 1~N 秒，登录框首屏明显卡一下。
    · Win11 24H2 起 wmic 已被系统移除，这条路本来也快走不通了。

现在全部改用 Win32 / 注册表，零子进程、零窗口、毫秒级：
    1. 注册表 MachineGuid（装系统时生成，最稳）
    2. SMBIOS Type 1 的整机 UUID（读 mssmbios 的 SMBiosData 原始固件表）
    3. CPU Identifier / ProcessorNameString
    4. BIOS 里的主板与整机型号（SystemSKU / BaseBoardProduct 等）
去重后拼起来过 sha256 取前 32 个 hex 字符。刻意**不落盘**：硬件不变算出来就
永远不变，存文件反而多一个能被人改的东西。

`_clean()` 负责踢掉没有区分度的垃圾值：全 0 / 全 F 的 UUID（很多整机/虚拟机
根本没往 SMBIOS 里写）、"To be filled by O.E.M." / "Default string" 占位符。

取不到任何硬件信息就返回空串，列留空，登录/导出照常——这是辅助信息，
不值得为它挡住主流程。
"""
import hashlib
import struct
import winreg

# 进程内缓存：算一次就够（纯注册表读取，<5ms）
_device_id_cache = None

_HKLM = winreg.HKEY_LOCAL_MACHINE
_REG_CPU = r'HARDWARE\DESCRIPTION\System\CentralProcessor\0'
_REG_BIOS = r'HARDWARE\DESCRIPTION\System\BIOS'
_REG_SMBIOS = r'SYSTEM\CurrentControlSet\Services\mssmbios\Data'


def _clean(value):
    """去掉没有区分度的垃圾值，返回干净字符串（不干净返回空串）"""
    s = (value or '').strip()
    if not s:
        return ''
    low = s.lower()
    if set(low) <= set('0f-'):                 # 全 0 / 全 F（带连字符也算）
        return ''
    if ('o.e.m' in low or 'default' in low or 'none' in low
            or 'to be filled' in low or 'system serial' in low or 'n/a' == low):
        return ''
    return s


def _reg(path, name):
    """读一个 HKLM 字符串值，读不到返回空串"""
    try:
        with winreg.OpenKey(_HKLM, path) as key:
            val, _ = winreg.QueryValueEx(key, name)
            return str(val)
    except Exception:
        return ''


def _machine_guid():
    """注册表里的 MachineGuid（装系统时生成，最可靠的兜底）"""
    return _clean(_reg(r'SOFTWARE\Microsoft\Cryptography', 'MachineGuid'))


def _smbios_uuid():
    """从注册表里的原始 SMBIOS 表抠 Type 1（System Information）的 UUID

    Windows 把固件表缓存在 mssmbios\\Data\\SMBiosData（REG_BINARY）：
    前 8 字节是 SMBIOS_TABLE_HEADER，之后是一串结构，每条格式为
        [type, length, handle_lo, handle_hi] + 格式化区 + 字符串区(双 0x00 结束)
    Type 1 的 UUID 在格式化区偏移 4，共 16 字节；SMBIOS 2.6+ 前三个字段是
    小端存储，按 Windows/WMI 的规则还原成标准 UUID 字符串。
    """
    try:
        with winreg.OpenKey(_HKLM, _REG_SMBIOS) as key:
            raw, _ = winreg.QueryValueEx(key, 'SMBiosData')
    except Exception:
        return ''
    if not isinstance(raw, bytes) or len(raw) < 16:
        return ''
    data = raw[8:]                              # 跳过 8 字节表头
    i, n = 0, len(data)
    while i + 4 <= n:
        typ, ln = data[i], data[i + 1]
        if ln < 4:
            break
        if typ == 1 and ln >= 0x18 and i + ln <= n and i + 24 <= n:
            b = data[i + 8:i + 24]              # 4 字节头 + 格式化区偏移 4
            if any(b):                          # 全 0 的 UUID 没区分度
                d1, d2, d3 = struct.unpack_from('<IHH', b, 0)
                return '%08X-%04X-%04X-%s-%s' % (
                    d1, d2, d3, b[8:10].hex().upper(), b[10:16].hex().upper())
        # 跳到下一条结构：越过格式化区，再越过以 00 00 结尾的字符串区
        j = i + ln
        while j + 1 < n and not (data[j] == 0 and data[j + 1] == 0):
            j += 1
        i = j + 2
        if typ == 127:                          # End-of-Table
            break
    return ''


def _cpu_id():
    """CPU 型号标识（注册表，换 CPU 才会变）"""
    parts = []
    for name in ('Identifier', 'ProcessorNameString', 'VendorIdentifier'):
        got = _clean(_reg(_REG_CPU, name))
        if got:
            parts.append(got)
    return '|'.join(parts)


def _board_id():
    """主板 / 整机型号（不含会随 BIOS 升级变化的版本号）"""
    parts = []
    for name in ('SystemManufacturer', 'SystemProductName', 'SystemSKU',
                 'BaseBoardManufacturer', 'BaseBoardProduct'):
        got = _clean(_reg(_REG_BIOS, name))
        if got:
            parts.append(got)
    return '|'.join(parts)


def device_id():
    """硬件指纹 → 32 位 hex 的设备 ID（进程内缓存，线程安全靠 GIL）

    例：'9f2c6a7e5b1d4c8a9f2c6a7e5b1d4c8a'
    """
    global _device_id_cache
    if _device_id_cache is not None:
        return _device_id_cache

    parts = []
    for got in (_machine_guid(), _smbios_uuid(), _cpu_id(), _board_id()):
        if got and got not in parts:
            parts.append(got)

    if not parts:
        _device_id_cache = ''                  # 硬件信息全拿不到：列留空，不挡主流程
    else:
        raw = '|'.join(parts).encode('utf-8')
        _device_id_cache = hashlib.sha256(raw).hexdigest()[:32]
    return _device_id_cache
