# -*- coding: utf-8 -*-
"""校验打好的 exe 里烧进去的是哪台授权库

为什么需要这个：PyInstaller 的 PYZ 是压缩的，`strings exe` 一个地址都看不到；
而 app_config 里两份配置（本地/远程）都作为字面量存在，光"看到 118.89..."不算数——
真正决定连哪台的是 `AUTH_DB_TARGET` 这个赋值。
所以这里做两步：解出 app_config 字节码 → 反汇编找到
`STORE_NAME AUTH_DB_TARGET` 前一个 LOAD_CONST，那个字符串才是答案。

用法：
    python scripts/verify_baked_db.py             # 检查 dist 下所有 exe
    python scripts/verify_baked_db.py a.exe       # 只查指定的
"""
import dis
import glob
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader  # noqa: E402

DIST = os.path.join(HERE, 'dist')


def load_app_config(path):
    """从 exe 里掏出 app_config 的 code 对象"""
    arch = CArchiveReader(path)
    pyz = [n for n in arch.toc if n.endswith('.pyz')]
    if not pyz:
        raise RuntimeError('包里没有 PYZ')
    tmp = os.path.join(os.environ.get('TEMP', '.'), '_verify_%d.pyz' % os.getpid())
    with open(tmp, 'wb') as f:
        f.write(arch.extract(pyz[0]))
    try:
        return ZlibArchiveReader(tmp).extract('app_config')
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def read_target(code):
    """反汇编找 AUTH_DB_TARGET = 'xxx' 的那个常量"""
    instrs = list(dis.get_instructions(code))
    last_const = None
    for ins in instrs:
        if ins.opname in ('LOAD_CONST',):
            last_const = ins.argval
        elif ins.opname == 'STORE_NAME' and ins.argval == 'AUTH_DB_TARGET':
            return last_const
    return None


def main(argv):
    if argv:
        targets = argv
    else:
        targets = [os.path.join(DIST, n) for n in os.listdir(DIST)
                   if n.lower().endswith('.exe')]
        targets.sort(key=os.path.getmtime, reverse=True)
    for p in targets:
        name = os.path.basename(p)
        print('\n=== %s' % name)
        print('    大小 %.1f MB · 打包于 %s' % (
            os.path.getsize(p) / 1048576.0,
            time.strftime('%m-%d %H:%M', time.localtime(os.path.getmtime(p)))))
        try:
            code = load_app_config(p)
        except Exception as e:
            print('    解析失败: %s: %s' % (type(e).__name__, e))
            continue
        target = read_target(code)
        host = '118.89.197.232:8306' if target == 'remote' else '127.0.0.1:3306'
        print('    AUTH_DB_TARGET = %r  →  实际连 %s' % (target, host))


if __name__ == '__main__':
    main(sys.argv[1:])
