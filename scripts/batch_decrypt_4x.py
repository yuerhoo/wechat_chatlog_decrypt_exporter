# -*- coding: utf-8 -*-
"""微信 4.x 媒体批量解密（仅解密"免密钥"那部分，验证可行性）

微信 4.2 的 .dat 文件"后缀不可信"，必须按内容魔数判定：
    · 头 8 字节 == 07 08 56 32 ...  -> V2 加密容器，需要按账号 AES key（本脚本不碰，计入待办）
    · 头已是 jpg/png/gif/webp...   -> 明文（如 4.2 的 _t_NW）
    · 其余                        -> 单字节 XOR（如 4.2 的 _W / _t_W，key 由 magic 反推）

用法：
    python scripts/batch_decrypt_4x.py <attach根目录> [--out 输出目录] [--max-scan N] [--max-decrypt N]

仅 Windows 需要，但解密逻辑本身跨平台（XOR/明文不需要 pycryptodome 也能跑 3.x/4.2 免密钥部分）。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import media as M  # noqa: E402


def classify(raw_head):
    """返回 'v2' / 'plain' / 'xor' / 'unknown'"""
    if len(raw_head) >= 8 and raw_head[:8] in M.V2_SIGNATURES:
        return 'v2'
    if len(raw_head) >= 4 and raw_head[:4] == b'\x07\x08\x56\x32':
        return 'v2'
    if M.detect_format(raw_head[:16]) != 'bin':
        return 'plain'
    return 'xor'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('attach')
    ap.add_argument('--out', default=None)
    ap.add_argument('--max-scan', type=int, default=60000)
    ap.add_argument('--max-decrypt', type=int, default=2000)
    args = ap.parse_args()

    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.attach.rstrip('/\\'))), 'decrypted_batch')
    os.makedirs(out, exist_ok=True)

    stat = {'scanned': 0, 'v2': 0, 'plain': 0, 'xor': 0, 'unknown': 0, 'dec_ok': 0, 'dec_fail': 0}
    t0 = time.time()

    for root, dirs, files in os.walk(args.attach):
        for fn in files:
            if not fn.lower().endswith('.dat'):
                continue
            stat['scanned'] += 1
            p = os.path.join(root, fn)
            try:
                with open(p, 'rb') as f:
                    head = f.read(8)
            except OSError:
                continue
            kind = classify(head)
            stat[kind] = stat.get(kind, 0) + 1

            if kind in ('plain', 'xor'):
                if stat['dec_ok'] + stat['dec_fail'] >= args.max_decrypt:
                    continue
                path, ext = M.decrypt_dat_file(p, out)
                if path:
                    stat['dec_ok'] += 1
                else:
                    stat['dec_fail'] += 1

            if stat['scanned'] >= args.max_scan:
                break
        if stat['scanned'] >= args.max_scan:
            break

    dt = time.time() - t0
    print('=== 批量解密统计（前 %d 个 .dat，耗时 %.1fs）===' % (stat['scanned'], dt))
    print('  免密钥可解(明文 plain): %d' % stat['plain'])
    print('  免密钥可解(XOR   xor): %d' % stat['xor'])
    print('  V2 加密容器(需账号key): %d' % stat['v2'])
    print('  其它/未知:            %d' % stat['unknown'])
    print('  实际解密成功: %d  失败: %d' % (stat['dec_ok'], stat['dec_fail']))
    print('  输出目录: %s' % out)
    nokey = stat['plain'] + stat['xor']
    if stat['scanned']:
        print('  免密钥占比(样本内): %.1f%%' % (100.0 * nokey / stat['scanned']))


if __name__ == '__main__':
    main()
