# -*- coding: utf-8 -*-
"""微信 4.x 全媒体分类 + 解密状态扫描（实测：视频/文件/语音多为明文，图片多为 V2 加密）

用法：
    python scripts/scan_media.py <xwechat账号根目录> [--out 输出目录] [--max-scan N] [--aes-key KEY]

行为：
    · 明文扩展名文件（mp4/pdf/docx/zip/mp3/wav/jpg/png …）→ 直接归档（不解密，本来就是清文）
    · .dat / 无扩展名文件 → 按内容魔数判定：
        - V2 签名（4.2: 07 08 56 32 08 07 00 04）→ 需按账号 AES key（--aes-key 可试解，否则标记 locked）
        - 其余 → 单字节 XOR(0x0E) 或明文，免密钥解密（落入 --out）
    · 给出按媒体类型（image / sns_image / video / file / voice）的分类统计 + 解密状态。

注：微信 4.2 的视频/文件/语音在本账号实测均以真实扩展名明文落盘，无需解密；
真正加密的只有图片（主图库 .dat 与朋友圈无扩展名文件），且以 V2 加密为主。
"""
import argparse
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import media as M  # noqa: E402


class BudgetDone(Exception):
    pass


def iter_media(root, max_files, budget):
    """有界遍历 root 下的文件，遇到全局预算上限即停（用异常干净终止 os.walk）"""
    count = 0
    try:
        for dp, dirs, files in os.walk(root):
            # 跳过明显的非媒体大目录（数据库/缓存碎片）以省时间
            low = dp.lower()
            if '\\db_storage' in low or '/db_storage' in low or '\\config' in low:
                dirs[:] = []
                continue
            for fn in files:
                count += 1
                budget[0] += 1
                yield os.path.join(dp, fn)
                if budget[0] >= max_files:
                    raise BudgetDone()
    except BudgetDone:
        return


def category_of(path, base):
    rel = '/' + os.path.relpath(path, base).lower().replace('\\', '/')
    if '/msg/attach/' in rel and '/img/' in rel:
        return 'image(主图库)'
    if '/sns/img/' in rel:
        return 'image(朋友圈)'
    if '/msg/video/' in rel:
        return 'video'
    if '/msg/file/' in rel:
        return 'file'
    if rel.endswith(('.mp3', '.wav', '.wma', '.ogg', '.m4a', '.amr', '.silk')):
        return 'voice'
    if '/sns/video/' in rel:
        return 'video(朋友圈)'
    return 'other'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('account_root', help='xwechat_files/<账号目录>')
    ap.add_argument('--out', default=None, help='免密钥解密图片的输出目录')
    ap.add_argument('--max-scan', type=int, default=20000, help='全局扫描文件预算上限')
    ap.add_argument('--aes-key', default=None, help='可选：4.2 V2 按账号 AES key（16 字符）')
    args = ap.parse_args()

    base = os.path.abspath(args.account_root)
    out = args.out
    if out:
        os.makedirs(out, exist_ok=True)

    stat = defaultdict(Counter)        # category -> {clear, decrypted, locked_v2, other}
    aes_key = args.aes_key.encode('ascii')[:16] if args.aes_key else None
    t0 = time.time()

    # 每个媒体目录独立预算（避免主图库 44 万 .dat 一次性耗尽预算导致其它类型扫不到）
    roots = [
        os.path.join(base, 'msg', 'attach'),   # 主图库 .dat
        os.path.join(base, 'msg', 'video'),     # 视频（实测明文 mp4）
        os.path.join(base, 'msg', 'file'),      # 文件（实测明文真实扩展名）
        os.path.join(base, 'cache'),            # 朋友圈 Sns/Img|Video + 语音 mp3/wav
    ]

    for root in roots:
        if not os.path.isdir(root):
            continue
        budget = [0]   # 每根独立预算
        for path in iter_media(root, args.max_scan, budget):
            try:
                cat = category_of(path, base)
            except ValueError:
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext in M.KNOWN_CLEAR_EXT:
                stat[cat]['clear'] += 1
                continue
            # .dat 或无扩展名 → 判定是否需要解密
            try:
                with open(path, 'rb') as f:
                    head = f.read(16)
            except OSError:
                continue
            if len(head) >= 8 and head[:8] in M.V2_SIGNATURES:
                # V2 加密容器
                if aes_key:
                    plain, dext = M.decode_dat_bytes(head, aes_key)
                    if plain:
                        stat[cat]['decrypted'] += 1
                        if out:
                            _save(out, path, plain, dext)
                        continue
                stat[cat]['locked_v2'] += 1
                continue
            # 非 V2：XOR(0x0E) / 明文 → 免密钥
            plain, dext = M.decode_dat_bytes(head)
            if plain:
                stat[cat]['decrypted'] += 1
                if out:
                    _save(out, path, plain, dext)
            else:
                # 可能是被误用 .dat 名的明文文件（如文本），原样归档
                stat[cat]['other'] += 1

    dt = time.time() - t0
    print('=== 微信 4.x 全媒体分类 + 解密状态（扫描预算 %d，耗时 %.1fs）===' % (budget[0], dt))
    print('%-18s %8s %8s %8s %8s' % ('媒体类型', '明文', '已解密', 'V2锁', '其它'))
    for cat in sorted(stat):
        c = stat[cat]
        print('%-18s %8d %8d %8d %8d' % (cat, c['clear'], c['decrypted'], c['locked_v2'], c['other']))
    tot = sum((c['clear'] + c['decrypted'] + c['locked_v2'] + c['other']) for c in stat.values())
    print('%-18s %8d %8d %8d %8d' % ('合计',
          sum(c['clear'] for c in stat.values()),
          sum(c['decrypted'] for c in stat.values()),
          sum(c['locked_v2'] for c in stat.values()),
          sum(c['other'] for c in stat.values())))
    print('说明：图片(主图库+朋友圈)以 V2 加密为主，需按账号 AES key（--aes-key）才能解；'
          '视频/文件/语音本账号实测均为明文真实扩展名，无需解密。')
    if out:
        print('免密钥解密图片已落入: %s' % out)


def _save(out, src_path, plain, ext):
    base = M._strip_suffix(os.path.splitext(os.path.basename(src_path))[0])
    dst = os.path.join(out, '%s.%s' % (base, ext))
    try:
        with open(dst, 'wb') as f:
            f.write(plain)
    except OSError:
        pass


if __name__ == '__main__':
    main()
