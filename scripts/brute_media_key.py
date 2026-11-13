# -*- coding: utf-8 -*-
"""图片 key 暴力枚举（多进程）：全内存 16 字节对齐窗口当候选

只在诊断时用。核心问题：微信 4.x 图片 AES key 是 16 字节二进制随机值，
内存里没有可打印特征，只能逐个对齐位置试。单进程 5.7us/候选，
568MB → 25 秒（8 进程），全部进程 3.7GB → 约 3 分钟。

用法：
    python scripts/brute_media_key.py --root <账号目录> --name Weixin --jobs 8
    python scripts/brute_media_key.py --root ... --pid 19772 --jobs 8
"""
import argparse
import ctypes
import os
import sys
import time
from ctypes import wintypes
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Crypto.Cipher import AES          # noqa: E402
from Crypto.Util import Padding        # noqa: E402

from core import media_key as mk       # noqa: E402

PAGE_RW = 0x04 | 0x08 | 0x40 | 0x80
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
MAX_REGION = 32 * 1024 * 1024


class MBI(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_void_p),
                ('AllocationBase', ctypes.c_void_p),
                ('AllocationProtect', wintypes.DWORD),
                ('RegionSize', ctypes.c_size_t),
                ('State', wintypes.DWORD),
                ('Protect', wintypes.DWORD),
                ('Type', wintypes.DWORD)]


def list_regions(pid):
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x0010 | 0x0400, False, pid)
    if not h:
        return []
    out = []
    try:
        addr = 0
        mbi = MBI()
        while addr < 0x7FFFFFFFFFFF:
            if k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                  ctypes.sizeof(mbi)) == 0:
                break
            proc = mbi.Protect or 0
            if (mbi.State == MEM_COMMIT and not (proc & PAGE_GUARD)
                    and 0 < mbi.RegionSize <= MAX_REGION and (proc & PAGE_RW)):
                out.append((addr, mbi.RegionSize))
            nxt = addr + mbi.RegionSize
            if nxt <= addr:
                break
            addr = nxt
    finally:
        k32.CloseHandle(h)
    return out


# --------------------------------------------------------------------------
# worker
# --------------------------------------------------------------------------
_G = {}


def _init(pid, blk, spans):
    _G['pid'] = pid
    _G['blk'] = blk
    _G['spans'] = spans


def _read(pid, base, size):
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x0010 | 0x0400, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_string_buffer(size)
        rd = ctypes.c_size_t(0)
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size,
                                     ctypes.byref(rd)):
            return None
        return buf.raw[:rd.value]
    finally:
        k32.CloseHandle(h)


def _full_ok(key, spans):
    for span in spans:
        try:
            d = Padding.unpad(AES.new(key, AES.MODE_ECB).decrypt(span), 16)
        except Exception:
            return False
        if not mk._ok_head(d):
            return False
    return True


def _work(item):
    """item = (base, size)。返回 (tried, hit_key_hex, hit_addr)"""
    base, size = item
    pid = _G['pid']
    blk = _G['blk']
    data = _read(pid, base, size)
    if not data:
        return 0, None, 0
    new = AES.new
    ECB = AES.MODE_ECB
    n = len(data) - 16
    tried = 0
    p = 0
    while p <= n:
        d = new(data[p:p + 16], ECB).decrypt(blk)
        tried += 1
        if d[:3] == b'\xff\xd8\xff' or d[:4] == b'\x89PNG' or d[:4] == b'GIF8':
            k = data[p:p + 16]
            if _full_ok(k, _G['spans']):
                return tried, k.hex(), base + p
        p += 16
    return tried, None, 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--name', default='', help='只扫名字匹配（Weixin / WeChatAppEx）')
    ap.add_argument('--pid', type=int, default=0)
    ap.add_argument('--jobs', type=int, default=8)
    ap.add_argument('--limit-mb', type=float, default=0, help='每个区最多扫多少 MB')
    args = ap.parse_args()

    samples = mk.pick_samples(args.root, 2)
    if not samples:
        print('没有 V2 样本')
        return 1
    blk = samples[0]['blk']
    spans = [s['span'] for s in samples]

    procs = mk.list_wechat_procs()
    if args.pid:
        procs = [p for p in procs if p[0] == args.pid]
    elif args.name:
        procs = [p for p in procs if args.name.lower() in p[1].lower()]
    print('目标进程：', procs)

    total = 0
    for pid, name in procs:
        regs = list_regions(pid)
        if not regs:
            print('[%s:%d] 打不开（权限？）' % (name, pid))
            continue
        mb = sum(s for _, s in regs) / 1048576
        print('[%s:%d] %d 个区 / %.1f MB → %d 个候选（16 对齐）'
              % (name, pid, len(regs), mb, sum(s for _, s in regs) // 16))
        # 大区切块，避免单任务过大（内存 + 负载不均）
        items = []
        for base, size in regs:
            step = 4 * 1024 * 1024
            off = 0
            while off < size:
                items.append((base + off, min(step, size - off)))
                off += step
        t0 = time.time()
        tried = 0
        with Pool(args.jobs, initializer=_init, initargs=(pid, blk, spans)) as pool:
            for n, k, a in pool.imap_unordered(_work, items, chunksize=4):
                tried += n
                if k:
                    print('\n★ 命中 key=%s @ 0x%X（%s:%d）'
                          % (k, a, name, pid))
                    print('写入：', mk.save_key('brute_key_out', k))
                    pool.terminate()
                    return 0
        dt = time.time() - t0
        total += tried
        print('  候选 %d，耗时 %.1f s（%.0f 候选/s）\n'
              % (tried, dt, tried / max(dt, 1e-6)))
    print('总计 %d 个候选，未命中' % total)
    return 0


if __name__ == '__main__':
    sys.exit(main())
