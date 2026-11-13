# -*- coding: utf-8 -*-
"""对话框里的「真实头像」与「媒体文件」解析器（微信原始目录**全程只读**）

⚠️ 对外暴露的媒体路径一律来自 `core.media_store` 的「已解密媒体库」：
   本模块只负责在微信原始目录里只读地找到源文件、解出像素；
   归档落盘（.dat→.jpg / wxgf→PNG / 明文副本）由注入的 MediaStore 负责。
   `image_path()` / `video_archived_path()` / `file_archived_path()` 返回的
   都是媒体库里的路径，「打开所在文件夹」会把那一个已解密文件选中。

两个来源，别搞混：
    dec_dir    解密输出目录 <WeChatData>/accounts/<wxid>/db
               └ head_image/head_image.db → head_image(username, md5, image_buffer)
                 image_buffer 是明文 JPEG，直接 QPixmap.loadFromData 就能显示
    media_root 微信原始账号目录 <xwechat_files>/<wxid>（= db_dir 的上级）
               ├ msg/attach/<md5(会话ID)>/<YYYY-MM>/Img/<本地hash>*.dat
               ├ msg/video/<YYYY-MM>/<本地hash>_thumb.jpg + <本地hash>.mp4（明文）
               └ msg/file/<YYYY-MM>/<文件名>（明文）

⚠️ 本地媒体文件名 hash 的关键结论（4.2 实测）：
    message_content 的 XML 里那些 32 位 hex（md5 / aeskey / newmd5 / cdnthumbaeskey）
    全是 **CDN 侧标识**，和磁盘文件名一点关系都没有——实测 12906 条媒体 XML，
    所有属性与 attach/video 文件名的交集都是 0。
    真正的落盘 hash 在消息表的 packed_info_data（protobuf blob）里，由
    core.messages.media_key_of() 提取后随 scan_chat(with_raw=True) 传进来。
    实测图片 4136/4144（99.8%）精确命中，且就在会话专属目录下。

图片为什么仍可能显示不出来：
    4.2 的 .dat 大多离不开账号 AES key，只在微信进程内存、磁盘推不出来。
    此时 image() 返回 None，并用 state() 明确区分「文件已找到但加密」
    「HEVC 解不了」「本地没有这个文件」，让界面给出不误导的等尺寸占位画框。

关于清晰度：
    候选文件按「原图 → _h 高清 → _t 小缩略图」优先，消息流里只缓存缩到
    720px 的小图；点开大图走 full_image() 按原分辨率重解（40 系原图多为
    wxgf/HEVC，由 core.hevc 交给 PyAV 解码）。

两个可选的"补全钥匙"（账号目录 <WeChatData>/accounts/<wxid>/ 下）：
    media_key.txt   第一行写 16 字符 AES key → 所有 V2 图片立即解出
    media_cache/    把已解好的图按 <hash>.jpg/.png 放进来 → 直接命中
"""
import glob
import hashlib
import html
import os
import re
import sqlite3
import time

from PyQt5.QtCore import Qt, QRectF
from PyQt5.QtGui import (QColor, QFont, QImage, QPainter, QPainterPath,
                         QPixmap)

from core import hevc as hevc_core
from core import media as media_core

# 同一 dec_dir 的头像表在进程内只加载一次（多开对话框共享，避免重复读 20MB 库）
_AVATAR_CACHE = {}


def rounded(pix, size, radius=6):
    """把任意 QPixmap 裁成方形圆角头像（保持比例居中裁剪，不拉伸变形）"""
    if pix.isNull():
        return pix
    src = pix.scaled(size, size, Qt.KeepAspectRatioByExpanding,
                     Qt.SmoothTransformation)
    out = QPixmap(size, size)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, radius, radius)
    p.setClipPath(path)
    x = (src.width() - size) // 2
    y = (src.height() - size) // 2
    p.drawPixmap(-x, -y, src)
    p.end()
    return out


def default_avatar(size=40):
    """库里确实没有这张头像时的中性灰色默认头像（微信无头像时的样式）"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor('#C8CDD4'))
    p.drawRoundedRect(QRectF(0, 0, size, size), 6, 6)
    p.setBrush(QColor('#FFFFFF'))
    r = size / 40.0
    # 同样是 QRectF 重载：r 是 float，四个位置参数全是 float，int 重载不接受
    p.drawEllipse(QRectF(20 - 4.6 * r, 8.5 * r, 9.2 * r, 9.2 * r))
    p.drawEllipse(QRectF(11.0 * r, 20.5 * r, 18.0 * r, 14.0 * r))
    p.end()
    return pm


class AvatarResolver(object):
    """按 username 取真实头像，带进程级 bytes 缓存 + 每实例 QPixmap 缓存

    username 可以是：联系人 wxid / 群成员 wxid / 自己的 wxid（带账号文件夹后缀）。
    自己的 wxid 形如 wxid_xxx_c779，而库里存的是去掉尾缀的 wxid_xxx，
    所以 lookup 时会依次尝试「原名 → 去掉最后一段下划线后缀 → 前缀匹配」。
    """

    def __init__(self, dec_dir, self_wxid=''):
        self.db = os.path.join(dec_dir or '', 'head_image', 'head_image.db')
        self.self_wxid = self_wxid or ''
        self._pix = {}
        self._bytes = self._load_bytes()

    def _load_bytes(self):
        if not os.path.isfile(self.db):
            return {}
        key = (self.db, os.path.getmtime(self.db))
        if key in _AVATAR_CACHE:
            return _AVATAR_CACHE[key]
        out = {}
        try:
            c = sqlite3.connect('file:/%s?mode=ro&immutable=1'
                               % self.db.replace('\\', '/'), uri=True)
            for u, buf in c.execute(
                    'SELECT username, image_buffer FROM head_image '
                    'WHERE image_buffer IS NOT NULL'):
                if u and buf:
                    out[u] = bytes(buf)
            c.close()
        except Exception:
            pass
        _AVATAR_CACHE[key] = out
        return out

    def _find(self, username):
        if not username:
            return None
        b = self._bytes.get(username)
        if b:
            return b
        # 自己的 wxid 带 _c779 之类账号目录后缀，库里是不带的那份
        if '_' in username:
            b = self._bytes.get(username.rsplit('_', 1)[0])
            if b:
                return b
        for u, buf in self._bytes.items():
            if u == username or username.startswith(u + '_'):
                return buf
        return None

    def pixmap(self, username, size=40):
        key = (username, size)
        if key in self._pix:
            return self._pix[key]
        b = self._find(username)
        pm = QPixmap()
        if b:
            pm.loadFromData(b)
            pm = rounded(pm, size) if not pm.isNull() else default_avatar(size)
        else:
            pm = default_avatar(size)
        self._pix[key] = pm
        return pm

    def own_pixmap(self, size=40):
        return self.pixmap(self.self_wxid, size)


# ---------------------------------------------------------------------------
# 媒体定位
# ---------------------------------------------------------------------------
_HASH_RE = re.compile(r'\b([0-9a-fA-F]{32})\b')
# 候选优先级：**原图在最前**（清晰度由它决定），其次 "_h" 高清缩略图，
# 最后才是 "_t" 小缩略图（实测只有 150×210 左右，放大必糊）。
# 原图/高清解不出时才退到缩略图，所以老账号也不会比以前差。
_IMG_PRIOR = ('', '_h', '_W', '_t_W', '_t_NW', '_t')
# 消息流里显示的图缩到这么长边以内就够（画框 220×160，2 倍屏 440 足矣）。
# 原图动辄 1920×1080，几百张 QPixmap 全留原尺寸会吃掉几百 MB 内存；
# 点开大图时再按需重新解一次原图（见 full_image）。
_DISPLAY_SIDE = 720
_CLEAR_IMG_EXT = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')
_CLEAR_VID_EXT = ('.mp4', '.mov', '.m4v')


def _hash_from(xml, media_key=''):
    """取本地文件名 hash：packed_info_data 优先，XML 属性只作兜底

    media_key 来自 core.messages.media_key_of(packed_info_data)，才是磁盘上的
    真实文件名。XML 里的 md5/aeskey 是 CDN 标识，仅在老版本或没带出
    packed_info_data 时退而求其次（命中率很低，但不比原来更差）。
    """
    k = (media_key or '').strip().lower()
    if re.fullmatch(r'[0-9a-f]{32}', k):
        return k
    if not xml:
        return None
    for attr in ('md5', 'newmd5', 'androidmd5', 'rawmd5'):
        m = re.search(r'\b%s\s*=\s*["\']([0-9a-fA-F]{32})["\']' % attr, xml)
        if m:
            return m.group(1).lower()
    m = _HASH_RE.search(xml)
    return m.group(1).lower() if m else None


def _tag(xml, name):
    m = re.search(r'<%s>(.*?)</%s>' % (name, name), xml or '', re.S | re.I)
    if not m:
        return ''
    s = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', m.group(1), flags=re.S)
    return html.unescape(s).strip()


def parse_appmsg(xml):
    """链接 / 文件 / 小程序卡片（type 49）的标题、描述、URL、文件名"""
    return {
        'title': _tag(xml, 'title'),
        'des': _tag(xml, 'des'),
        'url': _tag(xml, 'url'),
        'filename': _tag(xml, 'filename'),
        'fileext': _tag(xml, 'fileext'),
        'sourcedisplayname': _tag(xml, 'sourcedisplayname'),
        'appname': _tag(xml, 'appname'),
    }


def voice_seconds(xml):
    """语音时长（微信 4.x XML 的 voicelength 单位是毫秒）

    兼容老版本的 duration（毫秒）/ playlength（秒）；取不到返回 0，
    调用方会退化成"语音"默认长度，气泡宽度随时长增长的规律不变。
    """
    for attr in ('voicelength', 'duration', 'playlength'):
        m = re.search(r'\b%s\s*=\s*["\'](\d+)["\']' % attr, xml or '')
        if not m:
            continue
        v = int(m.group(1))
        sec = int(round(v / 1000.0)) if attr != 'playlength' else v
        return max(1, min(600, sec))
    return 0


def video_seconds(xml):
    m = re.search(r'\bplaylength\s*=\s*["\'](\d+)["\']', xml or '')
    return int(m.group(1)) if m else 0


class MediaResolver(object):
    """按消息 XML + 本地 hash + 时间，把图片/视频/文件对应到真实文件（只读）

    一个会话一个实例，内部缓存 hash → 结果，避免同一张图重复 glob/解密。
    """

    def __init__(self, media_root, sess_username, key_file='', cache_dir='',
                 store=None):
        self.root = media_root or ''
        self.sess_dir = (hashlib.md5(sess_username.encode()).hexdigest()
                         if sess_username else '')
        self.attach = os.path.join(self.root, 'msg', 'attach')
        self.video_dir = os.path.join(self.root, 'msg', 'video')
        self.file_dir = os.path.join(self.root, 'msg', 'file')
        self._img_cache = {}
        self._img_state = {}
        self._img_path = {}
        self._full_cache = {}
        self._video_cache = {}
        self._month_cache = {}
        # 可选：账号目录里放一个 16 字符的 media_key.txt，就能把 V2 图片解出来。
        # 平时不存在也没关系，所有免密钥图片照常显示。
        self.key_file = key_file or ''
        self.aes_key = ''
        self._read_key()
        # 可选：把已经离线解好的图片按 <hash>.jpg/.png 放进这个目录，直接命中
        self.cache_dir = cache_dir if os.path.isdir(cache_dir) else ''
        # 已解密媒体库（core.media_store.MediaStore）。所有对外路径都从它出，
        # 微信原始目录只读。为 None 时退回旧行为（仅用于兼容旧调用/单测）。
        self.store = store
        self.last_archive_status = ''

    def _read_key(self):
        self.aes_key = ''
        if self.key_file and os.path.isfile(self.key_file):
            try:
                # latin-1 与 media_key.save_key 对应：key 可能是任意字节
                with open(self.key_file, 'r', encoding='latin-1',
                          errors='ignore') as f:
                    self.aes_key = f.readline().strip()[:16]
            except OSError:
                pass

    def reload_key(self):
        """刚抓到图片密钥：重读 media_key.txt 并清掉解不出来的缓存"""
        self._read_key()
        if self.store is not None:
            # 密钥指纹变了，之前 locked 的条目下次自动重新解
            self.store.reload_key()
        self._img_cache.clear()
        self._img_state.clear()
        self._img_path.clear()
        self._full_cache.clear()
        self._video_cache.clear()
        return self.aes_key

    # ------------------------------------------------------------ 归档
    def archive(self, src, when=None):
        """把一个微信原始文件解密/复制进媒体库，返回可定位的路径（失败 ''）

        when 是消息时间：媒体库按 media/YYYY-MM-DD/ 分目录；批量解密没有
        消息时间时传 None，由媒体库退回源文件的 mtime。
        这是「点开大图 / 播放 / 打开文件 / 右键定位 / 复制路径」的统一入口：
        用户真正要用这个文件时才落盘，消息流渲染零开销。
        """
        if not src:
            self.last_archive_status = 'missing'
            return ''
        if self.store is None:               # 兼容：没有媒体库时仍指向源文件
            return src if os.path.isfile(src) else ''
        path, status = self.store.archive(src, when)
        self.last_archive_status = status
        return path

    def archive_status_text(self, src):
        """最近一次归档失败的原因（给用户看的短句）"""
        if self.store is None:
            return ''
        return self.store.status_text(src) if not self.store.archived_path(src) \
            else ''

    def _months(self, ts):
        """消息时间的月份 + 前后各一个月（微信按月分目录，跨月消息会差一天）"""
        if ts in self._month_cache:
            return self._month_cache[ts]
        out = []
        for delta in (0, -1, 1):
            t = (ts or time.time()) + delta * 30 * 86400
            m = time.strftime('%Y-%m', time.localtime(t))
            if m not in out:
                out.append(m)
        self._month_cache[ts] = out
        return out

    def _img_dirs(self, months):
        """候选 Img 目录：会话专属目录优先，再退化到同月份全局目录"""
        seen, out = set(), []
        base = os.path.join(self.attach, self.sess_dir)
        if os.path.isdir(base):
            try:
                sub = sorted(os.listdir(base))
            except OSError:
                sub = []
            for m in list(months) + sub[:24]:
                d = os.path.join(base, m, 'Img')
                if os.path.isdir(d) and d not in seen:
                    seen.add(d)
                    out.append(d)
        for m in months:
            for d in glob.glob(os.path.join(self.attach, '*', m, 'Img')):
                if d not in seen:
                    seen.add(d)
                    out.append(d)
        return out[:16]

    def _candidates(self, dirs, h):
        """按免密钥优先排好候选文件，最后用「hash 开头的任意文件」兜底"""
        seen, out = set(), []
        for suf in _IMG_PRIOR:
            for d in dirs:
                for p in glob.glob(os.path.join(d, h + suf + '.*')):
                    if p not in seen:
                        seen.add(p)
                        out.append(p)
        for d in dirs:
            for p in glob.glob(os.path.join(d, h + '*')):
                if p not in seen:
                    seen.add(p)
                    out.append(p)
        return out

    def _load_candidate(self, path):
        """解一个候选文件 -> (QPixmap 或 None, 解出来的真实格式)

        格式要一起带出来：wxgf（HEVC）解出像素也仍报 'hevc'，方便上层区分
        "加密了" / "格式不支持"，不要都笼统说成缺密钥。
        """
        try:
            size = os.path.getsize(path)
            if size <= 0:
                return None, ''
            # 没有 key 时 V2 容器必然解不开，只读 8 字节签名就跳过：
            # 原图动辄几 MB，为了确认"解不开"整读一遍太浪费。
            if not self.aes_key:
                with open(path, 'rb') as f:
                    if f.read(8) in media_core.V2_SIGNATURES:
                        return None, ''
            with open(path, 'rb') as f:
                data = f.read()
        except OSError:
            return None, ''
        if not data:
            return None, ''
        # 明文图片（真实 .jpg/.png 或无扩展名明文）直接吃；.dat 走统一解密入口
        plain, fmt = media_core.decode_dat_bytes(
            data, aes_key=self.aes_key or None)
        if not plain:
            return None, ''
        if fmt == 'hevc':                       # wxgf：Qt 不认，交给 PyAV 解
            got = hevc_core.decode(plain)
            if not got:
                return None, 'hevc'
            raw, w, h, stride = got
            img = QImage(raw, w, h, stride, QImage.Format_RGB888).copy()
            if img.isNull():
                return None, 'hevc'
            return QPixmap.fromImage(img), 'hevc'
        pm = QPixmap()
        if pm.loadFromData(plain) and not pm.isNull():
            return pm, fmt or ''
        return None, fmt or ''

    def _cached(self, h):
        """离线解密缓存：用户自己解出的 <hash>.jpg/.png，优先于加密 .dat"""
        if not self.cache_dir or not h:
            return None
        for ext in _CLEAR_IMG_EXT:
            p = os.path.join(self.cache_dir, h + ext)
            if os.path.isfile(p):
                pm = QPixmap(p)
                if not pm.isNull():
                    return pm
        return None

    @staticmethod
    def _is_v2(path):
        try:
            with open(path, 'rb') as f:
                return f.read(8) in media_core.V2_SIGNATURES
        except OSError:
            return False

    @staticmethod
    def _shrink(pm, max_side):
        """消息流里用的小图：长边超过 max_side 就等比缩下来省内存"""
        if not max_side or max_side <= 0:
            return pm
        if pm.width() <= max_side and pm.height() <= max_side:
            return pm
        return pm.scaled(max_side, max_side, Qt.KeepAspectRatio,
                         Qt.SmoothTransformation)

    def _lookup(self, h, months, max_side):
        """按优先级找文件并解出图 -> (QPixmap 或 None, state)"""
        paths = self._candidates(self._img_dirs(months), h)
        if not paths:
            return None, 'missing'
        locked, hevc = False, False
        for p in paths:
            pm, fmt = self._load_candidate(p)
            if pm is not None:
                # 记住"真正解出这张图的那一个文件"，看图窗口的
                # 「打开所在文件夹」要靠它定位（原图 / _h / 缩略图都可能命中）
                self._img_path[(h, months[0])] = p
                return self._shrink(pm, max_side), 'ok'
            if fmt == 'hevc':
                hevc = True
            elif self._is_v2(p):
                locked = True
        if hevc:
            return None, 'hevc'
        return None, ('locked' if locked else 'undecodable')

    def image(self, xml, ts, media_key='', max_side=_DISPLAY_SIDE):
        """图片 / 表情：返回 QPixmap 或 None（None 时调用方降级为占位卡片）

        失败原因记在 _img_state，界面用 state() 区分「已加密」和「没找到」。
        返回的是缩到 max_side 以内的小图——够画框用且不占内存；
        点开大图走 full_image() 重新按原分辨率解一次。
        """
        h = _hash_from(xml, media_key)
        if not h:
            self._img_state[(h, ts)] = 'missing'
            return None
        pm = self._cached(h)
        if pm is not None:
            self._img_state[(h, self._months(ts)[0])] = 'ok'
            return self._shrink(pm, max_side)
        if not os.path.isdir(self.attach):
            self._img_state[(h, ts)] = 'missing'
            return None
        months = self._months(ts)
        ck = (h, months[0])
        if ck in self._img_cache:
            return self._img_cache[ck]
        found, state = self._lookup(h, months, max_side)
        self._img_cache[ck] = found
        self._img_state[ck] = state
        return found

    def full_image(self, xml, ts, media_key=''):
        """点开大图用：按**原始分辨率**重新解一次（不受消息流小图缩放的限制）"""
        h = _hash_from(xml, media_key)
        if not h:
            return None
        ck = (h, self._months(ts)[0])
        if ck in self._full_cache:
            return self._full_cache[ck]
        pm, _state = self._lookup(h, self._months(ts), 0)
        # 只留最近一张：原图 QPixmap 一张十几 MB，攒起来会吃掉半个 G
        self._full_cache.clear()
        self._full_cache[ck] = pm
        return pm

    def state(self, xml, ts, media_key=''):
        """image() 之后立刻调用：ok / locked / hevc / undecodable / missing"""
        h = _hash_from(xml, media_key)
        months = self._months(ts)
        return self._img_state.get((h, months[0]), 'missing')

    def image_source_path(self, xml, ts, media_key=''):
        """原始来源（微信的 .dat / 用户手工放进 media_cache 的明文图），仅供排查

        命中顺序和 image() 完全一致：离线解密缓存 → 实际解成功的那个文件。
        """
        h = _hash_from(xml, media_key)
        if not h:
            return ''
        if self.cache_dir:
            for ext in _CLEAR_IMG_EXT:
                p = os.path.join(self.cache_dir, h + ext)
                if os.path.isfile(p):
                    return p
        p = self._img_path.get((h, self._months(ts)[0]), '')
        return p if p and os.path.isfile(p) else ''

    def image_path(self, xml, ts, media_key=''):
        """这张图**解密后**在媒体库里的路径（看图窗口定位 / 另存为用）

        以前这里直接返回微信原始 .dat，双击打不开、右键定位过去也是加密文件。
        现在先归档再返回 .jpg/.png —— 资源管理器选中的就是能直接打开的那张。
        """
        return self.archive(self.image_source_path(xml, ts, media_key), ts)

    def video(self, xml, ts, media_key=''):
        """视频：返回 (封面 QPixmap 或 None, mp4 路径 或 '')

        4.2 的视频封面是以本地 hash 命名的**明文 jpg**（<hash>_thumb.jpg /
        <hash>.jpg），mp4 同理，都不需要解密。
        """
        h = _hash_from(xml, media_key)
        if not h or not os.path.isdir(self.video_dir):
            return None, ''
        ck = (h, self._months(ts)[0])
        if ck in self._video_cache:
            return self._video_cache[ck]
        out = (None, '')
        for m in self._months(ts):
            d = os.path.join(self.video_dir, m)
            if not os.path.isdir(d):
                continue
            pm, mp4 = None, ''
            for p in sorted(glob.glob(os.path.join(d, h + '*'))):
                ext = os.path.splitext(p)[1].lower()
                if ext in _CLEAR_IMG_EXT and pm is None:
                    q = QPixmap(p)
                    if not q.isNull():
                        pm = q
                elif ext in _CLEAR_VID_EXT and not mp4:
                    mp4 = p
            if pm is not None or mp4:
                out = (pm, mp4)
                break
        self._video_cache[ck] = out
        return out

    def video_archived_path(self, xml, ts, media_key=''):
        """视频 mp4 在媒体库里的副本路径（惰性：只有点开/定位时才复制）

        video() 在消息流渲染时被调用，返回的是**微信原始路径**，别在这里
        就 copy 一个几百 MB 的 mp4。
        """
        _pix, mp4 = self.video(xml, ts, media_key)
        return self.archive(mp4, ts)

    def file_path(self, xml, ts, filename):
        """文件消息在微信原始目录里的路径（明文落盘，无需解密）

        只用于「有没有这个文件 / 取大小」这类只读判断；对外暴露的路径一律
        走 file_archived_path()。
        """
        if not filename or not os.path.isdir(self.file_dir):
            return ''
        for m in self._months(ts):
            p = os.path.join(self.file_dir, m, filename)
            if os.path.isfile(p):
                return p
        try:
            for m in self._months(ts):
                for p in glob.glob(os.path.join(self.file_dir, m, '*')):
                    if os.path.basename(p) == filename:
                        return p
        except OSError:
            pass
        return ''

    def file_archived_path(self, xml, ts, filename):
        """收发文件在媒体库里的副本路径（点开 / 右键定位时才复制）"""
        return self.archive(self.file_path(xml, ts, filename), ts)


def fmt_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ''
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return '%d %s' % (n, unit) if unit == 'B' else '%.1f %s' % (n, unit)
        n /= 1024.0
    return '%.1f TB' % n
