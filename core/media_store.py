# -*- coding: utf-8 -*-
"""已解密媒体库：把微信的加密/明文媒体归档到**程序自己的目录**

为什么要有这一层
--------------------------------------------------------------------
以前 `decode_dat_bytes()` 只在内存里解出 QPixmap，用完即弃；聊天窗口的
「打开所在文件夹」「复制路径」「播放」全部指向微信原始目录里的 `.dat`。
那是个加密文件：双击打不开，微信一清理缓存就全没了，而且用户可能被引导
去改动微信自己的数据目录。

现在的规则只有一条：**微信原始目录全程只读，任何对外暴露的路径都在媒体库里**。

目录（用户定稿：只要一层日期目录，不要镜像微信的深层结构）
--------------------------------------------------------------------
    <ROOT>/WeChatData/accounts/<wxid>/media/2026-11-13/1f29b41abfc03cec6a650a2509e07392.png
    <ROOT>/WeChatData/accounts/<wxid>/media/2026-11-13/72e4f8077bd6b4a44e596944e33640eb.mp4
    <ROOT>/WeChatData/accounts/<wxid>/media/2026-11-13/合同.pdf

    media/<YYYY-MM-DD>/<原文件名>.<真实扩展名>

日期优先取消息时间（调用方传 when），批量解密没有消息时间时退回源文件的
mtime。`.dat` 换成解出来的真实扩展名，明文视频/文件保留原名。同一天出现
同名但来自不同源的文件时，给文件名补一段源路径短 hash，避免互相覆盖。
索引 `index.json` 记录「源文件 → 输出 / 大小 / mtime / 密钥指纹 / 状态」，
用于幂等与增量；`layout` 字段标记目录版本，旧版镜像结构会自动迁移一次。

关键细节
--------------------------------------------------------------------
· 4.2 的 `.dat` 后缀不可信，一律按内容魔数解密（`core.media.decode_dat_bytes`）。
· wxgf（HEVC 原图）Qt 不认，这里用 PyAV 解成 rgb24 后**自己编码成 PNG**，
  系统看图工具才能直接打开；不依赖 Pillow / numpy / Qt，纯标准库 zlib。
· `.dat` 解出的 JPEG 尾部常跟 24 字节 trailer，落盘截到 FF D9 / IEND CRC，
  保证文件干净。
· 写盘用 `.tmp` + `os.replace` 原子替换；Windows 长路径用 `\\\\?\\` 前缀做 IO，
  但**返回给界面的永远是普通绝对路径**（explorer /select 不认长路径前缀）。
· 索引按 (源文件 size/mtime + 密钥指纹) 判幂等；`locked` 状态记下来不再反复
  重试，等抓到新密钥（指纹变了）再自动补解。
· 本模块不依赖 PyQt，可在后台线程里跑批量归档。
"""
import hashlib
import json
import os
import re
import shutil
import struct
import threading
import time
import zlib

from core import hevc as hevc_core
from core import media as media_core

# 状态：ok 之外的都意味着"没有可定位的已解密文件"
OK = 'ok'
LOCKED = 'locked'                # V2 容器但缺 AES key
UNDECODABLE = 'undecodable'      # 有 key / 免密钥，但解不出任何已知格式
HEVC_FAILED = 'hevc_failed'      # wxgf 解不出（PyAV 缺失或损坏）
TOO_LARGE = 'too_large'          # 明文文件超过复制上限
MISSING = 'missing'              # 微信原始文件已不在
FAILED = 'failed'                # 写盘 / IO 错误

# 目录结构版本：1 = 镜像微信相对路径（已废弃），2 = media/日期/文件名
LAYOUT = 2

_STATUS_TEXT = {
    OK: '已解密',
    LOCKED: '缺图片密钥',
    UNDECODABLE: '格式无法识别',
    HEVC_FAILED: 'HEVC 解码失败',
    TOO_LARGE: '文件过大，未复制',
    MISSING: '微信原始文件已不存在',
    FAILED: '归档失败',
}

# 单文件复制上限（明文视频/文件），0 表示不限制
DEFAULT_MAX_COPY_MB = 200

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', 'COM1', 'COM2', 'COM3', 'COM4',
             'COM5', 'COM6', 'COM7', 'COM8', 'COM9', 'LPT1', 'LPT2',
             'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'}
_DAY_NAME = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# 批量扫描时认的源文件（无扩展名的也算，朋友圈图片就是这样）
_BATCH_EXT = (media_core.KNOWN_CLEAR_EXT |
              {'.dat', '.wxgf', '.thumb', ''})
_BATCH_DIRS = (('msg', 'attach'), ('msg', 'video'), ('msg', 'file'),
               ('cache',))


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------
def _lp(path):
    """Windows 长路径前缀，**仅用于文件 IO**

    explorer.exe /select 不认 `\\\\?\\`，所以返回给界面的路径绝不能带前缀。
    """
    if os.name != 'nt' or not path or path.startswith('\\\\?\\'):
        return path
    p = os.path.abspath(path)
    if p.startswith('\\\\'):                       # UNC
        return '\\\\?\\UNC\\' + p[2:]
    return '\\\\?\\' + p


def _safe_component(name, maxlen=110):
    """文件名安全化：去掉 Windows 非法字符 / 保留名 / 尾部空格点"""
    s = _BAD_CHARS.sub('_', name or '').strip().rstrip('. ')
    if not s:
        s = '_'
    if s.split('.')[0].upper() in _RESERVED:
        s = '_' + s
    if len(s) > maxlen:
        stem, ext = os.path.splitext(s)
        ext = ext[:20]
        s = stem[:max(1, maxlen - len(ext))] + ext
    return s


def _day_dir(when, src=''):
    """归档日期目录 YYYY-MM-DD：优先消息时间，退回源文件 mtime / 今天"""
    ts = None
    try:
        if when:
            ts = float(when)
    except (TypeError, ValueError):
        ts = None
    if ts is None or ts <= 0:
        try:
            ts = os.stat(_lp(src)).st_mtime if src else time.time()
        except OSError:
            ts = time.time()
    try:
        return time.strftime('%Y-%m-%d', time.localtime(ts))
    except (ValueError, OSError, OverflowError):
        return time.strftime('%Y-%m-%d')


def _chunk(tag, data):
    return (struct.pack('>I', len(data)) + tag + data +
            struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))


def _png_from_rgb(raw, w, h, stride):
    """rgb24 像素 → PNG 字节（不依赖 Pillow / Qt）

    PyAV 的 plane.line_size 可能大于 w*3，必须按 stride 逐行只取前 w*3 字节。
    """
    if not raw or w <= 0 or h <= 0 or stride < w * 3:
        return None
    if len(raw) < stride * (h - 1) + w * 3:
        return None
    try:
        scan = bytearray()
        for y in range(h):
            off = y * stride
            scan.append(0)                       # filter type 0 (None)
            scan += raw[off:off + w * 3]
        return (b'\x89PNG\r\n\x1a\n'
                + _chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
                + _chunk(b'IDAT', zlib.compress(bytes(scan), 6))
                + _chunk(b'IEND', b''))
    except Exception:
        return None


def _trim_trailer(plain, fmt):
    """把 .dat 解出的图片截到真正的结尾，去掉微信附加的 trailer

    JPEG 常跟 24 字节无关数据；PNG 的 IEND 之后也可能有。QPixmap 能容忍，
    但落到磁盘给系统看图工具/其它软件用时，干净的文件更可靠。
    """
    try:
        if fmt == 'jpg':
            i = plain.rfind(b'\xff\xd9')
            if i >= 0 and i + 2 < len(plain):
                return plain[:i + 2]
        elif fmt == 'png':
            i = plain.rfind(b'\x00\x00\x00\x00IEND\xaeB`\x82')
            if i >= 0 and i + 12 < len(plain):
                return plain[:i + 12]
        elif fmt == 'gif':
            i = plain.rfind(b';')
            if i >= 0 and i + 1 < len(plain):
                return plain[:i + 1]
    except Exception:
        pass
    return plain


def _decode_to_plain(data, aes_key):
    """.dat / 无扩展名加密体 -> (明文字节, 扩展名, 状态)"""
    plain, fmt = media_core.decode_dat_bytes(data, aes_key=aes_key or None)
    if plain is not None:
        if fmt == 'hevc':
            got = hevc_core.decode(plain)
            if not got:
                return None, '', HEVC_FAILED
            png = _png_from_rgb(*got)
            if not png:
                return None, '', HEVC_FAILED
            return png, 'png', OK
        return _trim_trailer(plain, fmt), (fmt or 'bin'), OK
    # 解不出来：先看是不是"只差一把钥匙"
    head = data[:8]
    if (head in media_core.V2_SIGNATURES or
            data[:6] in (media_core.V2_MAGIC_FULL, media_core.V1_MAGIC_FULL)):
        if not aes_key:
            return None, '', LOCKED
    return None, '', UNDECODABLE


# ---------------------------------------------------------------------------
# 媒体库
# ---------------------------------------------------------------------------
class MediaStore(object):
    """一个账号一份：归档、索引、幂等、批量枚举

    store_dir   程序自己的媒体库根（<账号目录>/media）
    source_root 微信原始账号目录（db_dir 的上一级），只读
    key_file    可选的 media_key.txt（16 字符 AES key）
    """

    def __init__(self, store_dir, source_root='', key_file='',
                 max_copy_mb=DEFAULT_MAX_COPY_MB):
        self.root = os.path.abspath(store_dir or '')
        self.source_root = os.path.abspath(source_root or '')
        self.key_file = key_file or ''
        self.max_copy = int(max_copy_mb or 0) * 1024 * 1024
        self.index_file = os.path.join(self.root, 'index.json')
        self._lock = threading.RLock()
        self._index = {}
        self._dirty = 0
        self._last_save = 0.0
        self._key = ''
        self._key_fp = ''
        self._key_checked = 0.0
        self._load_index()

    # ------------------------------------------------------------ 密钥
    def _read_key(self):
        """重读图片密钥；文件没变就别反复 IO（批量会调几十万次）"""
        now = time.monotonic()
        if self._key_checked and now - self._key_checked < 1.5:
            return self._key, self._key_fp
        self._key_checked = now
        key = ''
        if self.key_file and os.path.isfile(self.key_file):
            try:
                with open(_lp(self.key_file), 'r', encoding='latin-1',
                          errors='ignore') as f:
                    key = f.readline().strip()[:16]
            except OSError:
                key = ''
        fp = hashlib.md5(key.encode('latin-1', 'ignore')).hexdigest()[:8] if key else ''
        if key != self._key or fp != self._key_fp:
            self._key, self._key_fp = key, fp
        return self._key, self._key_fp

    def key_fingerprint(self):
        return self._read_key()[1]

    def reload_key(self):
        """刚抓到新密钥：强制重读，让之前 locked 的条目自动重试"""
        self._key_checked = 0.0
        return self._read_key()[0]

    # ------------------------------------------------------------ 索引 / 迁移
    def _load_index(self):
        obj = {}
        try:
            with open(_lp(self.index_file), 'r', encoding='utf-8') as f:
                obj = json.load(f)
            if not isinstance(obj, dict):
                obj = {}
        except Exception:
            obj = {}
        entries = obj.get('entries')
        self._index = entries if isinstance(entries, dict) else {}
        if obj.get('layout') != LAYOUT and self._index:
            self._migrate_layout(self._index)

    def _migrate_layout(self, entries):
        """旧版「镜像微信深层路径」→ media/YYYY-MM-DD/文件名（一次性）

        只搬索引里 status=ok 的文件；搬不动的条目直接丢掉——微信原始文件
        还在，下次点开时按需重新解密即可，不会丢数据。
        """
        for k, e in list(entries.items()):
            out = (e.get('out') or '').replace('/', os.sep)
            if e.get('status') != OK or not out:
                continue
            parts = [p for p in out.split(os.sep) if p]
            if len(parts) == 2 and _DAY_NAME.match(parts[0]):
                e['out'] = os.path.join(*parts)      # 已经是新结构
                continue
            old = os.path.join(self.root, out)
            if not os.path.isfile(_lp(old)):
                entries.pop(k, None)
                continue
            sig = e.get('sig') or []
            day = _day_dir(sig[1] if len(sig) > 1 else None, k)
            rel = self._unique_rel(
                os.path.join(day, _safe_component(parts[-1])), k)
            new = os.path.join(self.root, rel)
            try:
                os.makedirs(_lp(os.path.dirname(new)), exist_ok=True)
                if os.path.isfile(_lp(new)):
                    entries.pop(k, None)             # 别覆盖，交给下次重解
                    continue
                shutil.move(_lp(old), _lp(new))
            except (OSError, shutil.Error):
                entries.pop(k, None)
                continue
            e['out'] = rel
        self._prune_old_tree()
        self._dirty += 1
        self._write_index()

    def _prune_old_tree(self):
        """删掉旧镜像目录里剩下的**空目录**（绝不删文件）"""
        for top in ('msg', 'cache', '_external'):
            base = _lp(os.path.join(self.root, top))
            if not os.path.isdir(base):
                continue
            for dirpath, _dirs, _files in os.walk(base, topdown=False):
                try:
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                except OSError:
                    pass
            try:
                if not os.listdir(base):
                    os.rmdir(base)
            except OSError:
                pass

    def _write_index(self):
        self._dirty = 0
        self._last_save = time.monotonic()
        if not self.root:
            return
        try:
            os.makedirs(_lp(self.root), exist_ok=True)
            tmp = self.index_file + '.tmp'
            with open(_lp(tmp), 'w', encoding='utf-8') as f:
                json.dump({'version': 1, 'layout': LAYOUT,
                           'entries': self._index},
                          f, ensure_ascii=False, separators=(',', ':'))
            os.replace(_lp(tmp), _lp(self.index_file))
        except OSError:
            pass

    def _maybe_save(self):
        """节流写盘：单条归档几乎立刻落盘，批量不会退化成 O(n²)"""
        if self._dirty and (self._dirty >= 100 or
                            time.monotonic() - self._last_save > 1.0):
            self._write_index()

    def flush(self):
        with self._lock:
            if self._dirty:
                self._write_index()

    def stats(self):
        with self._lock:
            out = {}
            for e in self._index.values():
                s = e.get('status') or OK
                out[s] = out.get(s, 0) + 1
            return out

    def status_text(self, src):
        with self._lock:
            e = self._index.get(self._key_of(src))
            return _STATUS_TEXT.get((e or {}).get('status'), '')

    def clear_failed(self):
        """清掉非 ok 的记录，让下次批量重新尝试"""
        with self._lock:
            for k in [k for k, v in self._index.items()
                      if v.get('status') != OK]:
                self._index.pop(k, None)
            self._dirty += 1
            self._write_index()

    # ------------------------------------------------------------ 路径
    def _key_of(self, src):
        return os.path.normcase(os.path.abspath(src or ''))

    def _unique_rel(self, rel, key=None):
        """同一天同名（不同来源）→ 补一段源路径短 hash，避免互相覆盖"""
        if not key:
            return rel
        norm = rel.replace('/', os.sep)
        used = any(k != key and (v.get('out') or '').replace('/', os.sep) == norm
                   for k, v in self._index.items())
        if not used:
            return rel
        day, base = os.path.split(rel)
        stem, ex = os.path.splitext(base)
        tag = hashlib.md5(key.encode('utf-8', 'ignore')).hexdigest()[:8]
        return os.path.join(day, '%s_%s%s' % (stem, tag, ex))

    def out_path(self, src, ext=None, when=None, key=None):
        """算出归档路径（不写盘）：<root>/YYYY-MM-DD/<文件名>.<ext>

        只保留一层日期目录；ext 不为 None 时把 .dat 换成真实扩展名。
        """
        name = _safe_component(os.path.basename(src) or '_')
        if ext:
            name = '%s.%s' % (os.path.splitext(name)[0], ext)
        rel = os.path.join(_day_dir(when, src), name)
        return os.path.join(self.root, self._unique_rel(rel, key))

    def archived_path(self, src):
        """已归档则返回普通绝对路径，否则 ''（不做任何解密 / 复制）"""
        with self._lock:
            e = self._index.get(self._key_of(src))
            if not e or e.get('status') != OK:
                return ''
            p = os.path.join(self.root, e.get('out', ''))
            return p if e.get('out') and os.path.isfile(_lp(p)) else ''

    # ------------------------------------------------------------ 归档
    def archive(self, src, when=None):
        """确保 src 对应的**明文文件**在媒体库里，返回 (路径, 状态)

        when 是消息时间戳（秒）：按聊天日期分目录；批量解密没有消息时间时
        传 None，退回源文件的 mtime。幂等：源文件没变、密钥没变、目标还在
        就直接返回。返回路径永远是普通绝对路径，可直接喂给 Shell API 定位。
        """
        if not src:
            return '', MISSING
        with self._lock:
            k = self._key_of(src)
            key, fp = self._read_key()
            try:
                st = os.stat(_lp(src))
            except OSError:
                self._index.pop(k, None)
                self._dirty += 1
                self._maybe_save()
                return '', MISSING
            sig = (int(st.st_size), int(st.st_mtime))
            old = self._index.get(k)
            if (old and old.get('status') == OK and old.get('sig') == list(sig)
                    and old.get('key') == fp and old.get('out')):
                p = os.path.join(self.root, old['out'])
                if os.path.isfile(_lp(p)):
                    self._maybe_save()
                    return p, OK
            # 同一个解不出的文件（且密钥没换），别每次点击/每轮批量都重解一遍：
            # 几十万 .dat 里 locked / 损坏的比例不低，反复试会把批量拖成几小时。
            # 抓到新密钥后 fp 变了，这里自然放行重新解。
            if (old and old.get('status') in (LOCKED, UNDECODABLE, HEVC_FAILED)
                    and old.get('sig') == list(sig) and old.get('key') == fp):
                return '', old['status']

            path, status, out_rel = self._do_archive(src, key, sig[0], when, k)
            entry = {'out': out_rel, 'sig': list(sig), 'key': fp,
                     'status': status}
            if old:
                self._index[k].update(entry)
            else:
                self._index[k] = entry
            self._dirty += 1
            self._maybe_save()
            return path, status

    def _do_archive(self, src, key, size, when, store_key):
        """真正干活：判断明文 / 解密 / 格式转换 -> 原子写，返回 (路径, 状态, 相对路径)"""
        ext_src = os.path.splitext(src)[1].lower()
        if ext_src in media_core.KNOWN_CLEAR_EXT:
            if self.max_copy and size > self.max_copy:
                return '', TOO_LARGE, ''
            out = self.out_path(src, when=when, key=store_key)
            if not out:
                return '', FAILED, ''
            try:
                os.makedirs(_lp(os.path.dirname(out)), exist_ok=True)
                tmp = out + '.tmp'
                shutil.copy2(_lp(src), _lp(tmp))
                os.replace(_lp(tmp), _lp(out))
            except (OSError, shutil.Error):
                return '', FAILED, ''
            return out, OK, os.path.relpath(out, self.root)

        try:
            with open(_lp(src), 'rb') as f:
                data = f.read()
        except OSError:
            return '', FAILED, ''
        if not data:
            return '', UNDECODABLE, ''
        plain, ext, status = _decode_to_plain(data, key)
        if status != OK or not plain:
            return '', status, ''
        out = self.out_path(src, ext, when, store_key)
        if not out:
            return '', FAILED, ''
        try:
            os.makedirs(_lp(os.path.dirname(out)), exist_ok=True)
            tmp = out + '.tmp'
            with open(_lp(tmp), 'wb') as f:
                f.write(plain)
            os.replace(_lp(tmp), _lp(out))
        except OSError:
            return '', FAILED, ''
        return out, OK, os.path.relpath(out, self.root)

    # ------------------------------------------------------------ 批量
    def iter_sources(self, cancel=None):
        """枚举微信账号目录里所有值得归档的媒体文件（供设置页批量解密）"""
        if not self.source_root or not os.path.isdir(self.source_root):
            return
        for parts in _BATCH_DIRS:
            base = os.path.join(self.source_root, *parts)
            if not os.path.isdir(base):
                continue
            for dirpath, _dirs, files in os.walk(_lp(base)):
                if cancel is not None and cancel():
                    return
                for fn in files:
                    if os.path.splitext(fn)[1].lower() not in _BATCH_EXT:
                        continue
                    # os.walk 用了长路径前缀时，拼出来的也是长路径；
                    # 归档要的是普通路径（索引 key 才稳定）
                    p = os.path.join(dirpath, fn)
                    if p.startswith('\\\\?\\UNC\\'):
                        p = '\\\\' + p[8:]
                    elif p.startswith('\\\\?\\'):
                        p = p[4:]
                    yield p


def default_store_dir(acc_dir):
    """约定位置：<账号目录>/media（可被设置项覆盖）"""
    return os.path.join(acc_dir, 'media')


# ---------------------------------------------------------------------------
# 进程内共享：多个对话框 + 设置页批量线程必须共用同一份索引与锁，
# 否则各写各的 index.json 会互相覆盖。
# ---------------------------------------------------------------------------
_STORES = {}
_STORES_LOCK = threading.Lock()


def get_store(store_dir, source_root='', key_file='', max_copy_mb=DEFAULT_MAX_COPY_MB):
    k = os.path.normcase(os.path.abspath(store_dir or ''))
    root = os.path.abspath(source_root or '')
    with _STORES_LOCK:
        s = _STORES.get(k)
        if s is None or os.path.abspath(s.source_root or '') != root:
            s = MediaStore(store_dir, source_root, key_file, max_copy_mb)
            _STORES[k] = s
        elif key_file and s.key_file != key_file:
            s.key_file = key_file
        return s


def forget_store(store_dir):
    """媒体库根目录被改了：丢掉旧实例，下次按新目录重建"""
    k = os.path.normcase(os.path.abspath(store_dir or ''))
    with _STORES_LOCK:
        s = _STORES.pop(k, None)
        if s is not None:
            s.flush()
        return s
