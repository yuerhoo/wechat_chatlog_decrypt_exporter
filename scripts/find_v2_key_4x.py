# -*- coding: utf-8 -*-
"""尽力从运行中的微信进程内存里提取 4.2 V2 媒体 AES key（有界扫描，可能失败）

⚠️ 建议改用 `core/media_key.py`（设置页「图片解密」用的就是它）：
    · 它按 offset 15 取 AES 段（本脚本已同步修正，旧版取 31 是错的）；
    · 它用整段 PKCS7 + magic 复核，并要求多个样本同时通过，
      避免单块校验 1/16 的假阳性；
    · 它会多轮重扫 + 只校验新增候选，适合"用户刚把图片打开"的时序。
本脚本保留作命令行备用，校验较松，命中后请用 decrypt_v2 完整验证一次。

微信 4.2 的 V2 .dat 容器（头 07 08 56 32 08 07 00 04）用"按账号"的 16 字节 AES key +
流密码（CTR/CFB/OFB 之类）加密，key 只在进程内存里。本脚本：
    1) 枚举微信进程（WeChatAppEx.exe / WeChat.exe / Weixin.exe）；
    2) 遍历可读写内存区，抠 16/32 位 ASCII 候选；
    3) 对每个候选，用多种 AES 模式（ECB/CBC/CTR/CFB/OFB）+ 多个 IV/nonce 试解一个 V2 样本，
       能解出图片 magic 即命中。

需要 Windows + 本进程能 OpenProcess（普通用户通常够，管理员更稳）。
这是重活，用 --timeout 兜底（默认 60s）。找不到返回非零退出码。
"""
import argparse
import os
import re
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import media as M  # noqa: E402


def load_v2_sample(sample_path):
    raw = open(sample_path, 'rb').read()
    assert raw[:8] in M.V2_SIGNATURES, '样本不是 V2 容器'
    L = struct.unpack('<H', raw[10:12])[0]
    field16 = raw[15:31]
    # 关键：AES 密文从 offset 15 开始（decrypt_v2 里 offset = 15）。
    # 旧版这里取 raw[31:]，把 raw/XOR 段当密文，偏移错 16 字节，
    # 导制任何 key 都试不出来（历史 0 命中的直接原因）。
    payload = raw[15:]
    return raw, L, field16, payload


def try_candidate(key16, sample):
    raw, L, field16, payload = sample
    from Crypto.Cipher import AES
    head_magics = (b'\xff\xd8\xff', b'\x89PNG', b'GIF8', b'RIFF', b'\x00\x00\x00\x18ftyp')
    def ok(b):
        return any(b[:len(m)] == m for m in head_magics)
    # 只解前 64 字节就能看到 magic，省时间
    head = payload[:64]
    ivs = {
        'field16': field16,
        'zeros': b'\x00' * 16,
        'first16': payload[:16],
    }
    for ivname, iv in ivs.items():
        # CBC（IV 占前 16 字节）
        try:
            d = AES.new(key16, AES.MODE_CBC, iv).decrypt(payload)
            if ok(d): return ('CBC', ivname)
        except Exception:
            pass
        # CTR（nonce 取 IV 前 8 字节，counter=0）
        try:
            d = AES.new(key16, AES.MODE_CTR, nonce=iv[:8],
                        initial_value=int.from_bytes(iv[8:16], 'big')).decrypt(head)
            if ok(d): return ('CTR', ivname)
        except Exception:
            pass
        # CFB / OFB
        for mode in (AES.MODE_CFB, AES.MODE_OFB):
            try:
                d = AES.new(key16, mode, iv).decrypt(payload)
                if ok(d): return (mode_name(mode), ivname)
            except Exception:
                pass
    # ECB 解前 16 字节（如果首块就是 AES-ECB）
    try:
        d = AES.new(key16, AES.MODE_ECB).decrypt(payload[:16])
        if ok(d): return ('ECB', 'first16')
    except Exception:
        pass
    return None


def mode_name(m):
    return {AES.MODE_CFB: 'CFB', AES.MODE_OFB: 'OFB'}.get(m, str(m))


def scan(pid, sample, timeout):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    PAGE_RW = 0x04 | 0x08 | 0x40 | 0x80
    h = k32.OpenProcess(0x0010 | 0x0400, False, pid)
    if not h:
        return None
    REGEX = re.compile(rb'(?<![a-zA-Z0-9])(?:[a-zA-Z0-9]{32}|[a-zA-Z0-9]{16})(?![a-zA-Z0-9])')
    class MBI(ctypes.Structure):
        _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p),
                    ('AllocationProtect', wintypes.DWORD), ('RegionSize', ctypes.c_size_t),
                    ('State', wintypes.DWORD), ('Protect', wintypes.DWORD),
                    ('Type', wintypes.DWORD)]
    try:
        addr = 0
        mbi = MBI()
        start = time.time()
        while addr < 0x7FFFFFFFFFFF:
            if time.time() - start > timeout:
                return None
            if k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
                break
            if (mbi.State == 0x1000 and (mbi.Protect & 0x100) == 0
                    and mbi.RegionSize <= 64 * 1024 * 1024 and (mbi.Protect & PAGE_RW)):
                size = mbi.RegionSize
                buf = ctypes.create_string_buffer(size)
                rd = ctypes.c_size_t(0)
                if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(rd)):
                    d = buf.raw[:rd.value]
                    for m in REGEX.finditer(d):
                        kb = m.group()
                        for cand in (kb[:16], kb):
                            if len(cand) != 16:
                                continue
                            r = try_candidate(cand, sample)
                            if r:
                                return cand.decode('ascii', 'ignore'), r
            nxt = addr + mbi.RegionSize
            if nxt <= addr:
                break
            addr = nxt
        return None
    finally:
        k32.CloseHandle(h)


def main():
        ap = argparse.ArgumentParser()
        ap.add_argument('sample', help='一个 V2 .dat 样本（如 .../Img/xxx_t.dat）')
        ap.add_argument('--timeout', type=float, default=60.0)
        args = ap.parse_args()
        sample = load_v2_sample(args.sample)
        print('V2 样本: %s  L=%d  field16=%s' % (args.sample, sample[1], sample[2].hex()))
        pids = M.get_wechat_pids()
        print('微信进程: %s' % (pids or '无'))
        if not pids:
            print('没找到微信进程，无法提 key（请确认微信在运行）')
            sys.exit(2)
        for pid in pids:
            print('扫描 PID=%d ...' % pid)
            r = scan(pid, sample, args.timeout)
            if r:
                key, mode = r
                print('命中! key=%s  mode=%s' % (key, mode))
                sys.exit(0)
        print('在内存里没找到能解出图片 magic 的 key（4.2 可能用其它 IV/模式，或 key 不在明文 ascii 区）')
        sys.exit(1)


if __name__ == '__main__':
    main()
