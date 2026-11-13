# -*- coding: utf-8 -*-
"""从运行中的微信进程内存里取 4.x V2 图片 AES key

背景
----
微信 4.x 把聊天图片存成 V2 容器（头 07 08 56 32 08 07）：
    sig(6) + aes_size(u32@6) + xor_size(u32@10) + pad(1@14) + AES-ECB 密文@15
AES 段用「按账号」的 16 字节 ASCII key，不落盘（keys.json 里那串是数据库
SQLCipher key，不是它），只在微信**真正显示出图片**时临时进内存。

实测结论（写在这里免得后人重走）
    · key 是 16 字符 ASCII（alnum）串，不是二进制随机值 —— 所以旧版只从
      可打印 ASCII 窗口切 16 字节是对的，但必须配「alnum 精确边界」才能
      把候选从 20 万压到 2 万，轮询才跑得动；
    · 「微信只是登录着」扫 460MB 主进程 16 万候选 0 命中（2026-11-12 实测），
      打开图片的那几秒才有 —— 因此必须**先开扫描、再让用户去开图**，
      单次点一下扫完就退，命中率取决于运气；
    · AES-128-ECB，首块密文在偏移 15（不是 31）；
    · 单块 magic 校验有 1/16 假阳性，必须 PKCS7 unpad + magic 双校验。

对外接口
    list_wechat_procs()                     -> [(pid, name)]
    pick_samples(media_root, n=2)           -> [sample]
    find_key(samples, seconds, progress,
             stop=None)                     -> (key: bytes|None, info: dict)
    save_key(acc_dir, key) / load_key(acc_dir)
"""
import glob
import os
import re
import struct
import time

MAGICS = (b'\xff\xd8\xff', b'\x89PNG', b'GIF8', b'RIFF', b'wxgf',
          b'\x00\x00\x00\x18ftyp', b'\x00\x00\x00\x20ftyp', b'BM')
V2_SIG = bytes.fromhex('070856320807')

# 候选串：纯 alnum 连续段（不加前后环视——环视让正则慢 1.7 倍，实测
# 453MB 上 9.7s vs 5.6s；边界影响靠下面按 16 字节切片抹平）。
RE_ALNUM = re.compile(rb'[0-9A-Za-z]{16,}')


def _ok_head(b):
    return any(b[:len(m)] == m for m in MAGICS)


# ---------------------------------------------------------------------------
# 样本：随便两个真实存在的 V2 图片，能解出 magic 就是 key 对了
# ---------------------------------------------------------------------------
def _load_sample(path):
    """只读签名 + AES 段（通常 1024 字节），不把 20MB 原图读进内存"""
    try:
        with open(path, 'rb') as f:
            head = f.read(15)
            if len(head) < 15 or head[:6] != V2_SIG:
                return None
            aes_size = struct.unpack_from('<I', head, 6)[0]
            aligned = (aes_size // 16 + 1) * 16
            if aes_size <= 0 or aligned > 4 * 1024 * 1024:
                return None
            span = f.read(aligned)
    except OSError:
        return None
    if len(span) < aligned:
        return None
    return {'path': path, 'aes_size': aes_size, 'aligned': aligned,
            'blk': span[:16], 'span': span}


def pick_samples(media_root, n=2):
    """从 msg/attach 里挑 n 个 V2 样本，尽量来自不同会话、体积适中"""
    if not media_root:
        return []
    pattern = os.path.join(media_root, 'msg', 'attach', '*', '*', 'Img', '*.dat')
    try:
        paths = glob.glob(pattern)
    except OSError:
        return []

    def size_of(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return 0

    paths = [p for p in paths if 30 * 1024 <= size_of(p) <= 2 * 1024 * 1024]
    paths.sort(key=size_of)
    seen_dir = set()
    out = []
    for p in paths:
        sess = os.path.dirname(os.path.dirname(os.path.dirname(p)))
        if sess in seen_dir:
            continue
        s = _load_sample(p)
        if s:
            out.append(s)
            seen_dir.add(sess)
        if len(out) >= n:
            break
    return out


def _check(key, samples):
    """用完整 AES 段校验：解出 1040 字节目录 + PKCS7 unpad + magic

    只解 AES 段（1024 字节明文），不碰原图/XOR 段——判断 key 对不对完全够用。
    返回解开 magic 的样本数。
    """
    from Crypto.Cipher import AES
    from Crypto.Util import Padding
    hits = 0
    for s in samples:
        try:
            dec = AES.new(key, AES.MODE_ECB).decrypt(s['span'])
            dec = Padding.unpad(dec, 16)
        except Exception:
            continue
        if _ok_head(dec):
            hits += 1
    return hits


# ---------------------------------------------------------------------------
# 进程枚举
# ---------------------------------------------------------------------------
def list_wechat_procs():
    """[(pid, name)]；按内存占用降序——主进程（几百 MB）排最前面

    图片 key 只在主进程里，先扫它能让用户在开图后最快命中（小进程
    加起来也要几百 MB，白白挤占每轮时间）。
    """
    if os.name != 'nt':
        return []
    try:
        import subprocess
        # 同上：GUI 程序里别让 tasklist 弹出黑色控制台窗口
        out = subprocess.run(['tasklist.exe', '/FO', 'CSV', '/NH'],
                             capture_output=True, text=True,
                             encoding='mbcs', errors='ignore',
                             creationflags=0x08000000).stdout
    except Exception:
        return []
    procs = []
    for line in out.splitlines():
        parts = [x.strip('"') for x in line.split('","')]
        if len(parts) < 2:
            continue
        name = parts[0].strip('"')
        if not re.match(r'(?i)^(Weixin|WeChat|WeChatAppEx)\.exe$', name):
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        mem = 0
        if len(parts) >= 5:
            digits = re.sub(r'[^0-9]', '', parts[4])
            mem = int(digits or 0)
        procs.append((mem, 0 if name.lower().startswith('weixin') else 1,
                      pid, name))
    procs.sort(key=lambda x: (-x[0], x[1]))
    return [(pid, name) for _mem, _k, pid, name in procs]


def _key_procs(procs):
    """真正持有图片 key 的进程：Weixin.exe（微信 4.x 主程序）

    WeChatAppEx.exe 是内嵌浏览器/小程序容器，内存大但基本不碰聊天图片，
    扫它纯属浪费——3GB 扫一轮要十几秒，直接把轮询频率打死。
    """
    main = [p for p in procs if p[1].lower() == 'weixin.exe']
    return main or [p for p in procs if 'appex' not in p[1].lower()] or procs


# ---------------------------------------------------------------------------
# 内存扫描
# ---------------------------------------------------------------------------
def _cands_from(data):
    """从一个内存区里提 key 候选

    key 是 16 字符 alnum 串；内存里常见的持有形态是 32 字符（前/后 16 各一把
    钥匙）。所以对 alnum 段统一按 16 字节网格切片——不管是 16、32 还是被
    其他字符黏住的 40 字符，都能切到正确答案。

    不额外做"任意可打印串"兜底：实测它让单轮扫描从 8 秒涨到 20 秒，
    而 key 是 alnum 已由社区多份实现交叉验证。
    """
    out = set()
    for s in RE_ALNUM.findall(data):
        n = len(s)
        if n == 32:
            out.add(s[:16])
            out.add(s[16:])
        elif n > 64:
            continue            # 超长 base64 之类，切了全是垃圾
        else:
            for i in range(0, n - 15, 16):
                out.add(s[i:i + 16])
    return out


def _scan_pid(h, k32, ctypes, wintypes, samples, deadline, seen, stats,
              on_region=None, stop=None):
    class MBI(ctypes.Structure):
        _fields_ = [('BaseAddress', ctypes.c_void_p),
                    ('AllocationBase', ctypes.c_void_p),
                    ('AllocationProtect', wintypes.DWORD),
                    ('RegionSize', ctypes.c_size_t),
                    ('State', wintypes.DWORD),
                    ('Protect', wintypes.DWORD),
                    ('Type', wintypes.DWORD)]

    from Crypto.Cipher import AES
    from Crypto.Util import Padding

    PAGE_RW = 0x04 | 0x08 | 0x40 | 0x80
    addr = 0
    mbi = MBI()
    last_cb = 0.0
    while addr < 0x7FFFFFFFFFFF:
        if time.time() > deadline or (stop is not None and stop.is_set()):
            return None
        if k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                             ctypes.sizeof(mbi)) == 0:
            break
        proc = mbi.Protect or 0
        if (mbi.State == 0x1000 and (proc & 0x100) == 0
                and 0 < mbi.RegionSize <= 32 * 1024 * 1024
                and (proc & PAGE_RW)):
            size = mbi.RegionSize
            buf = ctypes.create_string_buffer(size)
            rd = ctypes.c_size_t(0)
            if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size,
                                     ctypes.byref(rd)):
                data = buf.raw[:rd.value]
                stats['regions'] += 1
                for c in _cands_from(data):
                    if c in seen or len(c) != 16:
                        continue
                    seen.add(c)
                    stats['tried'] += 1
                    # 先解首块快筛（16 字节），过了再做全段 PKCS7 校验
                    try:
                        d = AES.new(c, AES.MODE_ECB).decrypt(samples[0]['blk'])
                    except Exception:
                        continue
                    if not _ok_head(d):
                        continue
                    ok = 0
                    for s in samples:
                        try:
                            dd = Padding.unpad(
                                AES.new(c, AES.MODE_ECB).decrypt(s['span']), 16)
                        except Exception:
                            continue
                        if _ok_head(dd):
                            ok += 1
                    if ok >= 1:
                        return (c, ok)
                now = time.time()      # 回调节流：每区都发会让 UI 忙死
                if on_region is not None and now - last_cb > 0.25:
                    last_cb = now
                    on_region(dict(stats))
        nxt = addr + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return None


def find_key(samples, seconds=60.0, progress=None, stop=None):
    """反复扫微信主进程，直到命中或超时

    关键：**先启动扫描、再让用户去微信打开图片**。key 只在图片真正显示时
    进内存，单次扫完就退的命中率取决于用户点按钮前是否已经开着图。

    progress(dict) 频繁回调（rounds/regions/tried/elapsed/left），供 UI 显示。
    stop: threading.Event，置位后尽快退出（用户点「停止」或关窗口）。
    返回 (key_bytes|None, info_dict)
    """
    t0 = time.time()
    info = {'regions': 0, 'tried': 0, 'procs': 0, 'rounds': 0,
            'samples': len(samples), 'error': '', 'elapsed': 0.0, 'left': 0.0}
    if os.name != 'nt':
        info['error'] = '仅支持 Windows'
        return None, info
    if not samples:
        info['error'] = '找不到可用于校验的加密图片样本'
        return None, info
    procs = list_wechat_procs()
    if not procs:
        info['error'] = '没发现运行中的微信，请先登录微信'
        return None, info
    procs = _key_procs(procs)
    info['procs'] = len(procs)

    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    seen = set()
    stats = {'regions': 0, 'tried': 0}
    deadline = t0 + max(5.0, float(seconds))
    rounds = 0
    while time.time() < deadline:
        if stop is not None and stop.is_set():
            break
        rounds += 1
        info['rounds'] = rounds
        for pid, name in procs:
            h = k32.OpenProcess(0x0010 | 0x0400, False, pid)
            if not h:
                info['error'] = '打不开微信进程（试试以管理员身份运行本程序）'
                continue
            try:
                r = _scan_pid(h, k32, ctypes, wintypes, samples, deadline,
                              seen, stats, stop=stop,
                              on_region=(lambda st: progress and progress(
                                  {**info, **st,
                                   'elapsed': time.time() - t0,
                                   'left': max(0.0, deadline - time.time())})))
                if r:
                    info.update(stats)
                    info['pid'] = pid
                    info['proc'] = name
                    info['elapsed'] = time.time() - t0
                    return r[0], info
            finally:
                k32.CloseHandle(h)
        info.update(stats)
        info['elapsed'] = time.time() - t0
        info['left'] = max(0.0, deadline - time.time())
        if progress:
            progress(info)
        if time.time() >= deadline:
            break
        time.sleep(0.15)     # 别把 CPU 打满，也免得轮询太密
    info['elapsed'] = time.time() - t0
    return None, info


def save_key(acc_dir, key):
    """把 key 写进账号目录的 media_key.txt（MediaResolver 启动时读它）

    用 latin-1 做字节保真：内存里的 key 可能含 0x80~0xFF 的字节，
    utf-8 往返虽然也能对上，但 decrypt_v2 只该看到 16 个原始字节。
    """
    if isinstance(key, (bytes, bytearray)):
        key = bytes(key).decode('latin-1')
    os.makedirs(acc_dir, exist_ok=True)
    path = os.path.join(acc_dir, 'media_key.txt')
    with open(path, 'w', encoding='latin-1', errors='ignore') as f:
        f.write(key.strip() + '\n')
    return path


def load_key(acc_dir):
    path = os.path.join(acc_dir or '', 'media_key.txt')
    try:
        with open(path, 'r', encoding='latin-1', errors='ignore') as f:
            return f.read().strip()
    except OSError:
        return ''
