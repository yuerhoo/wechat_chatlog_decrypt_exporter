# -*- coding: utf-8 -*-
"""图片 key 取证脚本（诊断用，命令行）

目的：找出「微信 4.x 图片 AES key 在内存里到底以什么形态存在」，
别再靠猜候选格式（旧版只扫可打印 ASCII 串，那 20 万候选天然覆盖不到
二进制 16 字节随机 key）。

三个阶段，逐级变慢、逐级更强：
    A. 特征侦查：在微信可写内存里搜
       · 样本 AES 段首块密文（16B）——命中说明微信正在/刚处理这张图
       · 明文 JPEG/PNG 头 ——命中说明图片已解密进内存
       · 可打印 ASCII / UTF-16 候选数量
    B. 热点暴力：以密文命中点为中心 ±N MB，逐字节切 16 字节窗口当 key 试
    C. 全区暴力：全内存按 16 字节对齐窗口枚举（慢，需要 --brute）

用法：
    python scripts/probe_media_key.py --root <微信账号目录>            # A + B
    python scripts/probe_media_key.py --root ... --brute --seconds 180  # 加 C
"""
import argparse
import ctypes
import os
import sys
import time
from ctypes import wintypes

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


def iter_regions(h, k32):
    """产出 (base_addr, data)；只取已提交、非 guard、可写、大小合理的区"""
    addr = 0
    mbi = MBI()
    while addr < 0x7FFFFFFFFFFF:
        if k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                             ctypes.sizeof(mbi)) == 0:
            break
        proc = mbi.Protect or 0
        if (mbi.State == MEM_COMMIT and not (proc & PAGE_GUARD)
                and 0 < mbi.RegionSize <= MAX_REGION and (proc & PAGE_RW)):
            size = mbi.RegionSize
            buf = ctypes.create_string_buffer(size)
            rd = ctypes.c_size_t(0)
            if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size,
                                     ctypes.byref(rd)):
                yield addr, buf.raw[:rd.value]
        nxt = addr + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt


def quick_hit(key, blk):
    """只解样本 AES 段首块：省掉全段 + unpad 的开销（约 3 倍提速）"""
    try:
        d = AES.new(key, AES.MODE_ECB).decrypt(blk)
    except Exception:
        return False
    return d[:3] == b'\xff\xd8\xff' or d[:4] == b'\x89PNG' or d[:4] == b'GIF8'


def full_hit(key, samples, need=1):
    from Crypto.Util import Padding as P
    hits = 0
    for s in samples:
        try:
            d = AES.new(key, AES.MODE_ECB).decrypt(s['span'])
            d = P.unpad(d, 16)
        except Exception:
            continue
        if mk._ok_head(d):
            hits += 1
    return hits >= need


def scan_region_for_needles(base, data, needles):
    """在 data 里搜 needles，返回 [(tag, abs_addr)]"""
    out = []
    for tag, nd in needles:
        i = data.find(nd)
        while i >= 0:
            out.append((tag, base + i))
            i = data.find(nd, i + 1)
    return out


def brute_window(base, data, start, end, samples, blk, step=1, deadline=None):
    """在 data[start:end] 里逐 step 字节切 16B 窗口，返回命中 key"""
    tried = 0
    lo = max(0, start)
    hi = min(len(data), end)
    p = lo
    while p + 16 <= hi:
        if deadline and time.time() > deadline:
            return None, tried
        k = data[p:p + 16]
        tried += 1
        if quick_hit(k, blk) and full_hit(k, samples):
            return k, tried
        p += step
    return None, tried


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True, help='微信账号目录（含 msg/attach）')
    ap.add_argument('--pid', type=int, default=0, help='只扫这个进程')
    ap.add_argument('--hot-mb', type=float, default=2.0, help='热点半径 MB')
    ap.add_argument('--brute', action='store_true', help='启用全区 16 对齐暴力')
    ap.add_argument('--seconds', type=float, default=180, help='暴力阶段总时限')
    args = ap.parse_args()

    samples = mk.pick_samples(args.root, 2)
    if not samples:
        print('!! 找不到 V2 样本，检查 --root', args.root)
        return 1
    blk = samples[0]['blk']
    print('样本 %d 个；首块密文 %s' % (len(samples), blk.hex()))

    procs = mk.list_wechat_procs()
    if args.pid:
        procs = [p for p in procs if p[0] == args.pid] or [(args.pid, '?')]
    print('进程：', procs)

    k32 = ctypes.windll.kernel32
    needles = [(f'cipher{i}', s['blk']) for i, s in enumerate(samples)]
    needles += [('jpeg', b'\xff\xd8\xff\xe0'), ('png', b'\x89PNG\r\n\x1a\n')]

    total_bytes = 0
    cipher_hits = []
    counts = {}
    t0 = time.time()
    windows = []   # (base, data, hit_offset) 供阶段 B 用

    for pid, name in procs:
        h = k32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            print('  [%s:%d] OpenProcess 失败（需要管理员权限？）' % (name, pid))
            continue
        nreg = 0
        nbytes = 0
        try:
            for base, data in iter_regions(h, k32):
                nreg += 1
                nbytes += len(data)
                found = scan_region_for_needles(base, data, needles)
                for tag, a in found:
                    counts[tag] = counts.get(tag, 0) + 1
                    if tag.startswith('cipher'):
                        cipher_hits.append((pid, name, a, base, len(data)))
                        if len(windows) < 24:
                            windows.append((base, data, a - base))
                del data
        finally:
            k32.CloseHandle(h)
        total_bytes += nbytes
        print('  [%s:%d] 区=%d  读取=%.1f MB' % (name, pid, nreg, nbytes / 1048576))

    print('---- 阶段 A ----')
    print('总读取 %.1f MB，耗时 %.1f s' % (total_bytes / 1048576, time.time() - t0))
    print('特征命中：', counts or '无')
    if cipher_hits:
        for pid, name, a, base, sz in cipher_hits[:6]:
            print('  密文 @ %s:%d 0x%X（区 base=0x%X size=%.1fMB）'
                  % (name, pid, a, base, sz / 1048576))

    if not cipher_hits:
        print('\n⚠ 内存里没找到样本的 AES 密文块 → 微信已释放该图的密文 buffer，'
              '热点法无效，只能走全区暴力（--brute）')

    # ---- 阶段 B：热点暴力 ----
    if windows:
        print('\n---- 阶段 B：热点 ±%.0fMB 逐字节枚举 ----' % args.hot_mb)
        span = int(args.hot_mb * 1024 * 1024)
        deadline = time.time() + args.seconds
        tried = 0
        for base, data, off in windows:
            k, n = brute_window(base, data, off - span, off + span, samples,
                                blk, 1, deadline)
            tried += n
            print('  窗口 base=0x%X 试 %d 个%s' % (base, n, '  ★命中 %s' % k.hex() if k else ''))
            if k:
                print('命中 key =', k.hex())
                print('写盘：', mk.save_key(os.path.join('probe_key_out'), k))
                return 0
            if time.time() > deadline:
                print('  超时')
                break
        print('  热点暴力 %d 个候选，未命中' % tried)

    # ---- 阶段 C：全区 16 对齐暴力 ----
    if args.brute:
        print('\n---- 阶段 C：全区 16 对齐暴力（限 %.0f s）----' % args.seconds)
        deadline = time.time() + args.seconds
        tried = 0
        for pid, name in procs:
            h = k32.OpenProcess(0x0010 | 0x0400, False, pid)
            if not h:
                continue
            try:
                for base, data in iter_regions(h, k32):
                    k, n = brute_window(base, data, 0, len(data), samples,
                                        blk, 16, deadline)
                    tried += n
                    if k:
                        print('★命中 key =', k.hex(), '（%s:%d base=0x%X）'
                              % (name, pid, base))
                        print('写盘：', mk.save_key('probe_key_out', k))
                        return 0
                    if time.time() > deadline:
                        print('超时，累计 %d 个候选' % tried)
                        break
                    del data
            finally:
                k32.CloseHandle(h)
            if time.time() > deadline:
                break
        print('全区暴力 %d 个候选，未命中（%.0f s）' % (tried, args.seconds))
    return 0


if __name__ == '__main__':
    sys.exit(main())
