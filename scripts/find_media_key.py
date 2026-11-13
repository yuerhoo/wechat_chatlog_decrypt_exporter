# -*- coding: utf-8 -*-
"""命令行版图片 key 抓取（持续监控，不依赖 GUI）

用法（推荐顺序）：

    1. 先跑起来：
       python scripts/find_media_key.py --seconds 120
    2. 看到「开始扫描」提示后，立刻切到微信，双击打开 1~2 张聊天图片并保持显示
    3. 抓到会自动打印 key 并写进账号目录的 media_key.txt

为什么必须"先跑脚本再开图"：微信的图片 AES key 只在图片真正显示时进内存，
图片窗口一关就卸载。反过来操作（先开图、再跑脚本）基本抓不到。

不传 --root 时自动从 WeChatData/accounts/*/account.json 里读第一条账号。
"""
import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import media_key as MK      # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def auto_detect():
    """从 WeChatData/accounts/*/account.json 里挑一个账号，返回 (acc_dir, media_root)"""
    pats = [os.path.join(HERE, 'WeChatData', 'accounts', '*', 'account.json'),
            os.path.join(HERE, 'dist', 'WeChatData', 'accounts', '*', 'account.json'),
            os.path.join(os.environ.get('LOCALAPPDATA') or '', 'WeChatData',
                         'accounts', '*', 'account.json')]
    for pat in pats:
        for f in sorted(glob.glob(pat)):
            try:
                with open(f, encoding='utf-8') as fp:
                    cfg = json.load(fp)
            except Exception:
                continue
            db_dir = cfg.get('db_dir') or ''
            if db_dir and os.path.isdir(db_dir):
                return os.path.dirname(f), os.path.dirname(db_dir)
    return '', ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default='', help='微信账号目录（含 msg/attach）')
    ap.add_argument('--acc', default='', help='保存 media_key.txt 的目录（默认自动）')
    ap.add_argument('--seconds', type=float, default=120, help='持续扫描秒数')
    args = ap.parse_args()

    acc_dir, media_root = auto_detect()
    root = args.root or media_root
    acc = args.acc or acc_dir
    if not root or not os.path.isdir(root):
        print('✗ 找不到微信账号目录，请用 --root 指定（形如 '
              r'D:\xwechat_files\wxid_xxx_1234）')
        return 1
    print('媒体目录:', root)
    print('密钥写到:', acc or '（未指定，抓到也不保存）')

    samples = MK.pick_samples(root, 2)
    if not samples:
        print('✗ 没找到可校验的 V2 加密图片样本（msg/attach 下找不到 30KB~2MB 的 .dat）')
        return 1
    print('校验样本: %d 个（首块密文 %s…）'
          % (len(samples), samples[0]['blk'][:4].hex()))

    procs = MK._key_procs(MK.list_wechat_procs())
    if not procs:
        print('✗ 没发现运行中的微信（Weixin.exe），请先登录微信')
        return 1
    print('目标进程:', ', '.join('%s:%d' % (n, p) for p, n in procs))
    print()
    print('>>> 现在切到微信，双击打开 1~2 张聊天图片，让图片保持显示 <<<')
    print('    （扫描会一直轮询，%d 秒内打开都来得及）' % int(args.seconds))
    print()

    t0 = time.time()
    last = {'rounds': 0, 'ts': 0.0}

    def on_progress(st):
        now = time.time()
        if now - last['ts'] < 0.5:      # 节流：每区都打会把终端刷爆
            return
        last['ts'] = now
        sys.stdout.write('\r  第 %d 轮 · 区 %d · 候选 %d · 已用 %ds · 剩余 %ds   '
                         % (st.get('rounds', 0), st.get('regions', 0),
                            st.get('tried', 0), int(st.get('elapsed', 0)),
                            int(st.get('left', 0))))
        sys.stdout.flush()
        last.update(st)

    key, info = MK.find_key(samples, seconds=args.seconds, progress=on_progress)
    print()
    if info.get('error'):
        print('! %s' % info['error'])
    if not key:
        print('✗ 没抓到（跑了 %d 轮、试了 %d 个候选、用了 %.0f 秒）'
              % (info.get('rounds', 0), info.get('tried', 0),
                 time.time() - t0))
        print('  再试一次：先回车重跑脚本 → 立刻切微信开图；'
              '仍不行就用管理员身份运行命令行。')
        return 2
    print('✓ 抓到 key: %s' % key.hex())
    print('  作为文本: %r' % key.decode('latin-1'))
    if acc:
        path = MK.save_key(acc, key)
        print('  已写入:', path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
