# -*- coding: utf-8 -*-
"""微信对话框查看器（无边框 · 可拖动 · 八方向缩放 · 多实例）

入口：MainWindow 双击会话列表行 → open_chat(sess) → ChatWindow(...)

布局（自上而下）：
    标题栏    真实头像 + 名称 + 群人数/微信号 + 右：搜索按钮 + 关闭 X
    搜索栏    点搜索按钮展开；模糊匹配 + 上/下条 + 定位高亮（默认隐藏）
    消息流    QScrollArea，文本气泡 / 图片 / 表情 / 视频封面 / 语音条 /
              链接·文件·位置卡片 / 系统提示 / 居中时间戳
    底栏      居中的「导出」按钮 → 弹导出窗口，导出清单只有当前这一个会话

数据源：
    · 真实头像 dec_dir/head_image/head_image.db（明文 JPEG BLOB）
    · 媒体文件 media_root = 微信原始账号目录（由 acc.db_dir 上溯一级），
      4.2 的图片 .dat 优先找免密钥缩略图；视频封面/收发文件本身是明文
    · 主题样式全部来自 ui.theme，切换主题时自动重刷（含已打开的对话框）

消息流是「滚动窗口」而不是一次性铺开：
    · 初次只渲染最后 BATCH(100) 条；
    · 滚到顶部自动再加载更早的 100 条，点顶部提示也可以手动加载；
    · 搜索跳到历史消息时，以命中行为中心重建窗口，不会把上万条一次渲染出来；
    · 构造控件按 STEP(20) 条分帧进行，顶部提示显示动态 loading 文案，
      整个过程不锁界面；插入时用「锚点 widget」保持住原来的视觉位置。

说明：
    · 自己消息判定统一用 sname == '(自己)'（messages.scan_chat 已规整）
    · 群聊显示成员昵称；私聊对方气泡不显示昵称（与微信原生一致）
    · 自己发的消息靠右（按 self_wxid 归一化比对，不再只看 sname=='(自己)'）
    · 图片/视频/表情都是固定尺寸画框（220×160 / 96×96），解析失败给等尺寸占位；
      图片按「原图 → _h → _t」优先取，点开走 image_viewer（滚轮缩放 / 拖拽 / 另存为）；
      语音气泡宽度随时长增长，_VoiceCard.set_transcript() 预留转文字位置
    · 语音/视频/文件点击可用系统默认程序打开本地文件
"""
import html
import os
import re
import sys
import time as _time

from PyQt5.QtCore import (Qt, QSize, QThread, pyqtSignal, QPoint, QTimer, QUrl,
                          QRectF)
from PyQt5.QtGui import (QColor, QCursor, QFont, QFontMetrics, QPainter,
                         QPainterPath, QPixmap, QDesktopServices, QTextDocument)
from PyQt5.QtWidgets import (QApplication, QWidget, QLabel, QVBoxLayout,
                             QHBoxLayout, QScrollArea, QPushButton, QFrame,
                             QLineEdit, QSizePolicy, QMenu, QFileDialog)

from . import icons, theme
from .chat_media import (AvatarResolver, MediaResolver, parse_appmsg,
                         voice_seconds, video_seconds, fmt_size, rounded)
from .image_viewer import ImageViewer
from .shell_open import (last_reveal_error, list_video_players, open_path,
                         open_with_exe, path_exists, reveal_in_explorer)
from .toast import Toast
from core import messages


def fmt_chat_time(ts):
    """'今天 15:16' / '昨天 21:27' / '02-03 19:13' / '2025-12-31 08:00'"""
    if not ts:
        return ''
    t = _time.localtime(ts)
    now = _time.localtime()
    hm = '%02d:%02d' % (t.tm_hour, t.tm_min)
    if (t.tm_year, t.tm_mon, t.tm_mday) == (now.tm_year, now.tm_mon, now.tm_mday):
        return '今天 ' + hm
    y = _time.localtime(_time.time() - 86400)
    if (t.tm_year, t.tm_mon, t.tm_mday) == (y.tm_year, y.tm_mon, y.tm_mday):
        return '昨天 ' + hm
    if t.tm_year == now.tm_year:
        return '%02d-%02d %s' % (t.tm_mon, t.tm_mday, hm)
    return '%04d-%02d-%02d %s' % (t.tm_year, t.tm_mon, t.tm_mday, hm)


class _Ctx(object):
    """一次对话框会话的共享上下文（避免每条消息都重复传一堆参数）"""

    def __init__(self, dec_dir, media_root, self_wxid, is_group,
                 media_store=None):
        self.avatars = AvatarResolver(dec_dir, self_wxid)
        # 账号目录里可选放 media_key.txt（16 字符）与 media_cache/（已解好的图），
        # 有就能直接把 V2 图片显示出来；没有也不影响免密钥图片/视频封面。
        acc_dir = os.path.dirname(dec_dir or '')
        # media_store = 程序自己的已解密媒体库。所有「打开/定位/复制路径」都
        # 经它归档后指向媒体库；微信原始目录只读，绝不暴露给用户去点。
        self.media = MediaResolver(
            media_root, '',
            key_file=os.path.join(acc_dir, 'media_key.txt'),
            cache_dir=os.path.join(acc_dir, 'media_cache'),
            store=media_store)
        self.is_group = is_group
        self.self_wxid = self_wxid
        self.sess_username = ''
        self.bubble_font = None   # 由 ChatWindow 探测 QSS 真实字体后填入

    def set_session(self, username):
        import hashlib
        self.sess_username = username or ''
        self.media.sess_dir = hashlib.md5(self.sess_username.encode()).hexdigest()


# ---------------------------------------------------------------------------
# 标题栏：只保留「搜索」+「关闭 X」
# ---------------------------------------------------------------------------
class _TitleBar(QWidget):
    def __init__(self, win, sess, ctx):
        super().__init__(win)
        self._win = win
        self.setFixedHeight(52)
        self.setObjectName('chatTitle')

        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 10, 0)
        lay.setSpacing(10)

        self._av = QLabel()
        self._av.setPixmap(ctx.avatars.pixmap(sess.username, 34))
        lay.addWidget(self._av)

        col = QVBoxLayout()
        col.setSpacing(1)
        col.setContentsMargins(0, 0, 0, 0)
        self._name = QLabel(sess.display)
        self._name.setObjectName('chatTitleName')
        col.addWidget(self._name)
        sub = sess.username if ctx.is_group else (sess.alias or sess.username)
        self._sub = QLabel(sub)
        self._sub.setObjectName('chatTitleSub')
        col.addWidget(self._sub)
        lay.addLayout(col)
        lay.addStretch(1)

        self.b_search = QPushButton()
        self.b_search.setObjectName('chatTopBtn')
        self.b_search.setToolTip('搜索聊天记录')
        self.b_search.setCursor(Qt.PointingHandCursor)
        self.b_search.setFixedSize(32, 30)
        self.b_search.clicked.connect(win.toggle_search)
        lay.addWidget(self.b_search)

        self.b_close = QPushButton()
        self.b_close.setObjectName('chatTopClose')
        self.b_close.setToolTip('关闭')
        self.b_close.setCursor(Qt.PointingHandCursor)
        self.b_close.setFixedSize(36, 30)
        self.b_close.clicked.connect(win.close)
        lay.addWidget(self.b_close)

    def retheme(self):
        self._win._refresh_title_icons()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPos()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if getattr(self, '_drag', None) is not None and e.buttons() & Qt.LeftButton:
            if self._win.isMaximized():
                self._win.showNormal()
            self._win.move(self._win.pos() + e.globalPos() - self._drag)
            self._drag = e.globalPos()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag = None
        super().mouseReleaseEvent(e)


# ---------------------------------------------------------------------------
# 后台读取消息（数据库查询放线程里，不碰 UI）
# ---------------------------------------------------------------------------
class _Loader(QThread):
    loaded = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, dec_dir, sess, data, self_wxid=''):
        super().__init__()
        self.dec_dir, self.sess, self.data = dec_dir, sess, data
        self.self_wxid = self_wxid

    def run(self):
        try:
            rows = messages.scan_chat(self.dec_dir, self.sess, self.data,
                                      with_raw=True, self_wxid=self.self_wxid)
            self.loaded.emit(rows or [])
        except Exception as e:
            self.failed.emit(str(e))


# ---------------------------------------------------------------------------
# 可点击的媒体卡片
# ---------------------------------------------------------------------------
def _menu_qss():
    """右键菜单跟随主题配色（默认 QMenu 是系统白底，深色主题下会突兀）"""
    return """
    QMenu { background: %(card)s; color: %(text)s; border: 1px solid %(line)s;
            padding: 4px; font-family: 'Microsoft YaHei'; font-size: 12px; }
    QMenu::item { padding: 6px 24px 6px 14px; border-radius: 4px; }
    QMenu::item:selected { background: %(soft)s; color: %(primary_text)s; }
    QMenu::item:disabled { color: %(sub)s; }
    QMenu::separator { height: 1px; background: %(line)s; margin: 4px 8px; }
    """ % theme.palette()


class _ClickLabel(QLabel):
    """点击后 看大图 / 播视频 / 打开文件 / 打开链接

    kind 决定左键做什么：
        'image'  看大图（big_pix 已拿到就直接看；只有 loader 时点开才按原
                 分辨率重解一次，消息流里存的只是缩到 720px 的小图）
        'video'  用系统默认播放器播放，右键可以换播放器 / 定位文件
        'file'   用系统默认程序打开（4.x 的收发文件本身是明文）
        'link'   用浏览器打开

    视频为什么不内嵌播放器：4.2 的视频在磁盘上就是明文 mp4，交给系统播放器
    零成本、格式支持最好（HEVC 也能播）；Qt5 的 Multimedia 在 Windows 走
    DirectShow，没装系统 HEVC 扩展的机器会黑屏有声，反而更像"功能坏了"。
    """

    def __init__(self, target='', big_pix=None, parent=None, loader=None,
                 source=None, kind=''):
        super().__init__(parent)
        self._target = target or ''
        self._pix = big_pix
        self._loader = loader
        self._source = source          # 字符串或「取路径的 callable」
        self._kind = kind or ('link' if str(self._target).startswith('http')
                              else '')
        if self._target or big_pix is not None or loader is not None:
            self.setCursor(Qt.PointingHandCursor)

    # ---------------------------------------------------------------- 工具
    def _resolve_target(self):
        """target 允许是「取路径的 callable」：点开/右键时才归档，渲染零开销"""
        t = self._target
        if callable(t):
            try:
                t = t()
            except Exception:
                t = ''
        return t or ''

    def _action_path(self):
        """这次动作真正该用的文件 —— 一律是**已解密、在媒体库里**的那一个

        图片走 _src_path()（MediaResolver.image_path → 归档后返回 .jpg）；
        视频 / 收发文件走惰性 target（归档副本的 mp4 / pdf）。
        这是「右键打开位置必须指向程序自己的目录且选中文件」的关键。
        """
        if self._kind == 'image':
            return self._src_path()
        return self._resolve_target()

    def _src_path(self):
        try:
            return (self._source() if callable(self._source)
                    else (self._source or ''))
        except Exception:
            return ''

    def _open_viewer(self, pm):
        path = self._src_path()
        name = os.path.basename(path) if path else ''
        ImageViewer.show_image(pm, name, path, parent=self)

    def _warn(self, text):
        Toast.show_for(self, text)

    def _open_target(self):
        """左键点视频 / 文件：交给系统默认程序打开媒体库里的副本"""
        t = self._resolve_target()
        if t.startswith(('http://', 'https://')):
            QDesktopServices.openUrl(QUrl(t))
            return
        if not t or not path_exists(t):
            if self._kind == 'video':
                # 微信 4.x 视频按需下载：只下封面，点开播放过才有 <hash>.mp4。
                # 实测 6061 个封面里只有 68 个 mp4，所以这个分支是常态。
                self._warn('视频原文件不在本地，先在微信里点开播放一次')
            else:
                self._warn('本地未找到该文件，可能已被微信清理')
            return
        if not open_path(t):
            self._warn('系统里没有能打开它的程序，'
                       '试试右键「用其他播放器打开」'
                       if self._kind == 'video' else '打开失败')

    def _copy_target(self):
        """复制文件路径 —— 必须给可见反馈，否则用户不知道点没点上

        以前只 setText 就完事，剪贴板确实写进去了，但界面上一点动静都没有，
        用户会以为按钮坏了（这次反馈的就是这个）。
        """
        t = self._action_path()
        if not t:
            self._warn('没有可复制的路径')
            return
        QApplication.clipboard().setText(t)
        # toast 只报文件名：整条路径太长，弹出来既看不清也没必要
        self._warn('已复制文件路径：%s' % os.path.basename(t))

    def _browse_player(self):
        """挑一个本机任意程序打开这个视频（不依赖系统的「打开方式」对话框）"""
        start = os.environ.get('ProgramFiles', 'C:\\Program Files')
        path, _f = QFileDialog.getOpenFileName(
            self, '选择用来播放的程序', start, '程序 (*.exe)')
        if not path:
            return
        if not open_with_exe(path, self._resolve_target()):
            self._warn('这个程序打不开该视频，换一个试试')
            return
        try:
            from app_config import Prefs
            Prefs().set('player/last_exe', path)    # 记住，下次排在列表里
        except Exception:
            pass
        self._warn('已用「%s」打开' % os.path.splitext(os.path.basename(path))[0])

    # ---------------------------------------------------------------- 事件
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            if self._pix is not None:
                self._open_viewer(self._pix)
            elif self._loader is not None:
                self._open_big()
            elif self._target or self._kind in ('video', 'file'):
                self._open_target()
        super().mousePressEvent(e)

    def contextMenuEvent(self, e):
        """图片 / 视频 / 文件的右键菜单：打开、定位文件、复制路径

        图片以前没有右键菜单，只能点开大图再找「打开所在文件夹」；现在
        三类媒体统一在画框上右键，定位的都是**媒体库里已解密的那个文件**。
        """
        if self._kind not in ('image', 'video', 'file'):
            return super().contextMenuEvent(e)
        menu = self.build_menu()
        if menu is not None:
            menu.exec_(e.globalPos())

    def _players(self):
        """默认播放器 + 本机检测到的其他播放器（上次手选的排最前）"""
        items = list_video_players()
        try:
            from app_config import Prefs
            last = Prefs().get('player/last_exe', '') or ''
        except Exception:
            last = ''
        if last and path_exists(last):
            lastn = os.path.normcase(os.path.normpath(last))
            if not any(os.path.normcase(os.path.normpath(t)) == lastn
                       for _n, t in items):
                name = os.path.splitext(os.path.basename(last))[0]
                items.insert(1 if items else 0, (name, last))
        return items

    def build_menu(self):
        """构造右键菜单（拆出来是为了能单测菜单项，不用真的弹出阻塞）

        弹菜单这一下就会触发归档：所以「打开所在文件夹」点下去时，
        媒体库里的 .jpg / .mp4 / .pdf 必然已经就位，explorer /select 选中它。
        """
        t = self._action_path()
        exists = bool(t) and path_exists(t)
        menu = QMenu(self)
        menu.setStyleSheet(_menu_qss())
        if exists:
            players = self._players() if self._kind == 'video' else []
            # 第一项直接写明会用谁打开，别让用户点了才知道
            if self._kind == 'image':
                head = menu.addAction('查看大图')
                head.triggered.connect(self._open_big)
            elif self._kind == 'video':
                head_txt = ('用「%s」播放' % players[0][0].replace('（默认）', '')
                            if players else '播放')
                head = menu.addAction(head_txt)
                head.triggered.connect(self._open_target)
            else:
                head = menu.addAction('打开文件')
                head.triggered.connect(self._open_target)
            if self._kind == 'video':
                sub = menu.addMenu('用其他播放器打开')
                for name, exe in players[1:]:
                    a = sub.addAction(name)
                    # 默认参数绑定，避免闭包全都指到最后一个 exe
                    a.triggered.connect(
                        lambda _c=False, x=exe: self._play_with(x))
                if players[1:]:
                    sub.addSeparator()
                sub.addAction('浏览其他程序…').triggered.connect(
                    self._browse_player)
            menu.addSeparator()
            loc = menu.addAction('打开所在文件夹（选中文件）')
            loc.setToolTip('在资源管理器里定位并选中「%s」\n'
                           '这是程序媒体库里已解密的文件，不是微信的 .dat'
                           % os.path.basename(t))
            loc.triggered.connect(lambda: self._reveal(t))
            menu.addAction('复制文件路径').triggered.connect(self._copy_target)
        else:
            if self._kind == 'video':
                a = menu.addAction('视频原文件不在本地')
                a.setEnabled(False)
                b = menu.addAction('在微信里点开播放一次就会下载到本地')
                b.setEnabled(False)
            elif self._kind == 'image':
                a = menu.addAction('这张图还没解密（可能缺图片密钥）')
                a.setEnabled(False)
                b = menu.addAction('到「设置 → 图片解密」抓取密钥')
                b.setEnabled(False)
            else:
                a = menu.addAction('本地未找到文件（可能已被微信清理）')
                a.setEnabled(False)
            menu.addSeparator()
            if t:
                menu.addAction('复制文件路径').triggered.connect(
                    self._copy_target)
        return menu

    def _open_big(self):
        """右键「查看大图」：按原分辨率重新解一次再打开看图窗口"""
        if self._pix is not None and self._loader is None:
            self._open_viewer(self._pix)
            return
        if self._loader is None:
            self._warn('这张图本地已经找不到')
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            pm = self._loader()
        finally:
            QApplication.restoreOverrideCursor()
        if pm is not None and not pm.isNull():
            self._open_viewer(pm)
        else:
            self._warn('这张图本地解不出来（加密原图且缺密钥）')

    def _reveal(self, path):
        # 系统会定位到目录并**选中这一个已解密文件**，不是"只把文件夹打开"。
        if reveal_in_explorer(path):
            self._warn('已定位并选中：%s' % os.path.basename(path))
        else:
            self._warn('定位失败：%s' % (last_reveal_error() or '未知原因'))

    def _play_with(self, exe):
        if not open_with_exe(exe, self._resolve_target()):
            self._warn('这个播放器打不开该视频，换一个试试')




# 图片 / 表情 / 视频统一固定画框：不管原图是 4000px 还是 40px 缩略图，
# 在消息流里都占同一个尺寸，布局不会忽大忽小、也不会随窗口宽度乱跑。
MEDIA_BOX = (220, 160)
EMOJI_BOX = (96, 96)


class _MediaFrame(_ClickLabel):
    """固定尺寸媒体画框：解析成功显示真实像素，失败显示同尺寸占位

    · 图片 / 表情：等比缩放居中；点开大图走 loader 按原分辨率重新解，
      消息流里留的只是缩到 720px 的小图（省内存，画框也够用）
    · 视频：封面 + 播放角标 + 时长，点击用系统播放器打开 mp4
    · 加密 / 缺文件：仍是同一尺寸的灰底画框 + 图标 + 一行说明，绝不折行
    """

    def __init__(self, pix=None, box=MEDIA_BOX, icon='image', label='',
                 target='', big_pix=None, seconds=0, play=False, loader=None,
                 source=None, kind='', badge=''):
        super().__init__(target=target, big_pix=big_pix, loader=loader,
                         source=source, kind=kind)
        self.setObjectName('chatImg')
        # 仅用于排查/自检：这一格是真图还是占位
        self.has_image = pix is not None and not pix.isNull()
        w, h = box
        self.setFixedSize(w, h)
        p = theme.palette()
        pm = QPixmap(w, h)
        pm.fill(QColor(p['line']))
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing, True)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, w, h), 8, 8)
        painter.setClipPath(clip)

        if pix is not None and not pix.isNull():
            fit = pix.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            painter.drawPixmap((w - fit.width()) // 2,
                              (h - fit.height()) // 2, fit)
        else:
            ic_sz = 40 if w >= 180 else 30
            ic = icons.pixmap(icon, p['sub'], ic_sz)
            cy = (h - ic.height()) // 2 - (9 if label else 0)
            painter.drawPixmap((w - ic.width()) // 2, cy, ic)
            if label:
                painter.setFont(QFont('Microsoft YaHei', 9))
                painter.setPen(QColor(p['sub']))
                # 固定画框 + 单行绘制：话术永远不会换行
                painter.drawText(QRectF(4, h - 23, w - 8, 18),
                                 Qt.AlignCenter, label)

        if play:
            r = 26
            painter.setBrush(QColor(0, 0, 0, 130))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QRectF((w - r * 2) / 2.0, (h - r * 2) / 2.0,
                                      r * 2, r * 2))
            painter.drawPixmap((w - 24) // 2, (h - 24) // 2,
                               icons.pixmap('videoplay', '#FFFFFF', 24))
        if seconds:
            painter.setBrush(QColor(0, 0, 0, 140))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(QRectF(w - 52, h - 24, 46, 18), 4, 4)
            painter.setPen(QColor('#FFFFFF'))
            painter.setFont(QFont('Microsoft YaHei', 8))
            painter.drawText(QRectF(w - 52, h - 24, 46, 18),
                             Qt.AlignCenter, '%d″' % seconds)
        if badge:
            # 左上角状态角标（如「未下载」）：有时长角标在右下，两者不打架
            f = QFont('Microsoft YaHei', 8)
            tw = QFontMetrics(f).width(badge) + 12
            painter.setBrush(QColor(0, 0, 0, 125))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(QRectF(6, 6, tw, 17), 4, 4)
            painter.setPen(QColor('#FFFFFF'))
            painter.setFont(f)
            painter.drawText(QRectF(6, 6, tw, 17), Qt.AlignCenter, badge)
        painter.end()
        self.setPixmap(pm)
        if kind == 'video':
            # 鼠标一悬停就知道这一格能不能播：微信只按需下载，大多只有封面
            self.setToolTip(
                '点击播放 · 右键换播放器或定位已归档的视频' if play
                else '视频原文件不在本地：在微信里点开播放一次即可下载')
        elif kind == 'image':
            self.setToolTip('点击查看大图 · 右键可定位已解密的图片文件')


class _VoiceBubble(QWidget):
    """语音气泡本体：宽度随时长线性增长，右侧显示秒数"""

    def __init__(self, seconds, own):
        super().__init__()
        self.own = own
        self.sec = max(1, seconds or 1)
        self.setFixedHeight(38)
        # 微信原生规律：语音越长气泡越宽；1 秒 92px，60 秒封顶 260px
        self.setFixedWidth(min(260, 88 + self.sec * 4))

    def paintEvent(self, e):
        p = theme.palette()
        pnt = QPainter(self)
        pnt.setRenderHint(QPainter.Antialiasing, True)
        pnt.setPen(Qt.NoPen)
        pnt.setBrush(QColor(p['own_bubble'] if self.own else p['peer_bubble']))
        pnt.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 8, 8)
        ic = icons.pixmap('voice', p['chat_text'], 20)
        pnt.drawPixmap(8, 9, ic)
        # 声波条数量按可用宽度算，避免短时长气泡里和秒数挤在一起
        pnt.setBrush(QColor(p['chat_text']))
        n = max(4, min(11, (self.width() - 78) // 6))
        for i in range(n):
            h = 4 + (i % 4) * 2.6
            pnt.drawRoundedRect(QRectF(32 + i * 6, 19 - h / 2, 2.6, h),
                               1.3, 1.3)
        pnt.setFont(QFont('Microsoft YaHei', 10))
        pnt.setPen(QColor(p['chat_text']))
        pnt.drawText(self.width() - 44, 0, 38, self.height(),
                     Qt.AlignVCenter | Qt.AlignRight, '%d″' % self.sec)
        pnt.end()


class _VoiceCard(QWidget):
    """语音消息：气泡 + 预留的「转文字」文字位（本版不做识别）

    后续接语音转文字时，对这张卡片调用 set_transcript(text) 就会在气泡
    下方出现文字块；现在不调用，界面上只显示按时长变化的语音条。
    """

    def __init__(self, seconds, own):
        super().__init__()
        self.own = own
        self.transcript = ''
        self._text_lb = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        self.bubble = _VoiceBubble(seconds, own)
        lay.addWidget(self.bubble, 0, Qt.AlignRight if own else Qt.AlignLeft)

    def set_transcript(self, text):
        text = (text or '').strip()
        self.transcript = text
        if not text:
            if self._text_lb is not None:
                self._text_lb.hide()
            return
        if self._text_lb is None:
            lb = QLabel(text)
            lb.setObjectName('chatVoiceText')
            lb.setWordWrap(True)
            lb.setFixedWidth(max(180, self.bubble.width()))
            self.layout().addWidget(
                lb, 0, Qt.AlignRight if self.own else Qt.AlignLeft)
            self._text_lb = lb
        else:
            self._text_lb.setText(text)
            self._text_lb.show()


class _LinkCard(QFrame):
    """链接 / 文件 / 位置 / 加密图片的微信原生卡片：标题 + 描述 + 右侧图标

    宽度跟着文字走，上限是窗口内容区的固定百分比（由 reflow 传入）。
    占位类卡片 nowrap_desc=True：描述强制单行，绝不再折成两行。
    """

    def __init__(self, title, desc, icon='link', target='', tag='',
                 max_width=300, nowrap_desc=False):
        super().__init__()
        self.setObjectName('chatMediaCard')
        self._max_width = 0
        self._nowrap_desc = nowrap_desc
        self._title = title or '消息'
        self._desc = desc or ''
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 9, 9, 9)
        lay.setSpacing(8)
        col = QVBoxLayout()
        col.setSpacing(4)
        t = QLabel(self._title)
        t.setObjectName('chatCardTitle')
        t.setWordWrap(True)
        t.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(t)
        self.lb_title = t
        self.lb_desc = None
        if self._desc:
            d = QLabel(self._desc)
            d.setObjectName('chatCardDesc')
            d.setWordWrap(not nowrap_desc)
            if not nowrap_desc:
                d.setMaximumHeight(46)
            col.addWidget(d)
            self.lb_desc = d
        if tag:
            g = QLabel(tag)
            g.setObjectName('chatCardTag')
            col.addWidget(g)
        lay.addLayout(col, 1)
        ic = QLabel()
        ic.setPixmap(icons.pixmap(icon, theme.palette()['sub'], 40))
        ic.setFixedSize(42, 42)
        ic.setAlignment(Qt.AlignCenter)
        lay.addWidget(ic)
        self._target = target or ''
        if target:
            self.setCursor(Qt.PointingHandCursor)
        self.ensurePolished()
        self._apply_width(max_width)

    def _apply_width(self, max_width):
        """链接 / 文件卡片也用固定宽度，避免同一类消息一张宽一张窄"""
        self._max_width = max(180, int(max_width))
        self.setFixedWidth(int(min(280, self._max_width)))

    def reflow(self, max_width):
        if abs(int(max_width) - self._max_width) >= 2:
            self._apply_width(max_width)

    def _path(self):
        """文件卡片的 target 可以是惰性归档的 callable（点开/右键才复制）"""
        t = self._target
        if callable(t):
            try:
                t = t()
            except Exception:
                t = ''
        return t or ''

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self._target:
            t = self._path()
            if t.startswith(('http://', 'https://')):
                QDesktopServices.openUrl(QUrl(t))
            elif not t or not path_exists(t):
                Toast.show_for(self, '本地未找到该文件，可能已被微信清理')
            elif not open_path(t):
                Toast.show_for(self, '系统里没有能打开它的程序')
        super().mousePressEvent(e)

    def contextMenuEvent(self, e):
        """收发文件卡片：打开 / 定位媒体库里的副本 / 复制路径

        以前文件卡片只有左键打开，没有右键，用户没法找到文件在哪。
        """
        t = self._path()
        if not t or t.startswith(('http://', 'https://')):
            return super().contextMenuEvent(e)
        menu = QMenu(self)
        menu.setStyleSheet(_menu_qss())
        if path_exists(t):
            menu.addAction('打开文件').triggered.connect(
                lambda: open_path(t) or Toast.show_for(self, '打开失败'))
            menu.addSeparator()
            loc = menu.addAction('打开所在文件夹（选中文件）')
            loc.setToolTip('在资源管理器里定位并选中「%s」\n'
                           '这是程序媒体库里的副本，不是微信原始目录'
                           % os.path.basename(t))
            loc.triggered.connect(lambda: self._reveal(t))
            menu.addAction('复制文件路径').triggered.connect(
                lambda: self._copy(t))
        else:
            a = menu.addAction('本地未找到文件（可能已被微信清理）')
            a.setEnabled(False)
        menu.exec_(e.globalPos())

    def _reveal(self, path):
        if reveal_in_explorer(path):
            Toast.show_for(self, '已定位并选中：%s' % os.path.basename(path))
        else:
            Toast.show_for(self, '定位失败：%s'
                           % (last_reveal_error() or '未知原因'))

    def _copy(self, path):
        QApplication.clipboard().setText(path)
        Toast.show_for(self, '已复制文件路径：%s' % os.path.basename(path))


# ---------------------------------------------------------------------------
# 单条消息
# ---------------------------------------------------------------------------
_URL_RE = re.compile(r'(https?://[^\s<>"）)]+)')


def _linkify(text):
    """把纯文本里的 URL 变成可点的蓝色链接（微信原生就是这样）"""
    import html as _html
    out, last = [], 0
    esc = _html.escape(text or '')
    for m in _URL_RE.finditer(esc):
        out.append(esc[last:m.start()])
        u = m.group(1)
        out.append('<a href="%s" style="color:%s; text-decoration:none;">%s</a>'
                   % (u, theme.palette()['primary'], u))
        last = m.end()
    out.append(esc[last:])
    return ''.join(out).replace('\n', '<br/>')


class MsgItem(QWidget):
    """一行消息：头像固定在两端，内容列按自身宽度收缩、绝不铺满整行

    之前这里只有「头像 + 内容列」两项、没有伸缩项，QHBoxLayout 会把剩余
    宽度摊到各个格子里：内容 widget 的最小宽度一变（卡片 220 / 图片 / 文本），
    头像的位置就跟着漂，同时内容列被拉满 → 白底气泡铺满整行。
    现在明确让对侧 addStretch(1) 吃掉所有剩余空间，内容列用 Maximum 策略
    保持 sizeHint 宽度，头像就永远贴在 x=14（自己则贴右边缘）。
    """

    def __init__(self, row, is_own, ctx, max_bubble):
        super().__init__()
        self.row_index = -1
        ts, sender, sname = row[1], row[2], row[3]
        type_code, _tname, brief = row[4], row[5], row[6]
        raw = row[7] if len(row) > 7 else brief
        # 索引 8 = packed_info_data 里的本地媒体文件名 hash（4.2 的关键）
        media_key = row[8] if len(row) > 8 else ''
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setContentsMargins(0, 0, 0, 0)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 4, 14, 4)
        lay.setSpacing(8)

        # 真实头像：自己用账号 wxid；对方优先用发送人 wxid（群成员），
        # 解析不到发送人时退回会话 username（私聊对方）
        who = (ctx.self_wxid if is_own
               else (sender or ctx.sess_username or ctx.self_wxid))
        av = QLabel()
        av.setPixmap(ctx.avatars.pixmap(who, 40))
        av.setFixedSize(40, 40)
        self.avatar = av

        # 内容列：包成容器并限制为 Maximum，宽度由内容自己决定
        box = QWidget()
        box.setAttribute(Qt.WA_StyledBackground, True)
        col = QVBoxLayout(box)
        col.setSpacing(3)
        col.setContentsMargins(0, 0, 0, 0)
        align = Qt.AlignRight if is_own else Qt.AlignLeft
        if ctx.is_group and not is_own and sname:
            nick = QLabel(sname)
            nick.setObjectName('chatNick')
            nick.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
            col.addWidget(nick, 0, align)

        widget = self._build_body(type_code, raw, ts, brief, is_own, ctx,
                                  max_bubble, media_key)
        widget.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        col.addWidget(widget, 0, align)
        box.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.body = widget

        if is_own:
            lay.addStretch(1)
            lay.addWidget(box, 0, Qt.AlignTop | Qt.AlignRight)
            lay.addWidget(av, 0, Qt.AlignTop)
        else:
            lay.addWidget(av, 0, Qt.AlignTop)
            lay.addWidget(box, 0, Qt.AlignTop | Qt.AlignLeft)
            lay.addStretch(1)

    # QLabel 富文本的左右 padding 11*2 + 边框 1*2 + QTextDocument 自带 4*2
    # 边距 ≈ 32px；实测垫 26px 时「刚好一行」的文字仍会被挤成两行，
    # 这就是之前"窗口拉宽了消息还是换行"的真正原因。留 38px 安全余量。
    BUBBLE_PAD = 38

    @staticmethod
    def bubble_width(lb, html_text, max_bubble, font=None):
        """按可见文字逐行量宽度：短消息=文字宽，只有真超长才压到 max_bubble 换行

        不用 QTextDocument.idealWidth()：它对连续空格 / U+2005 之类特殊空格
        的宽度估计偏小（实测差 20~30px），量出来的气泡刚好把一行文字挤成两行，
        正是"窗口拉宽了消息还是折行"的残留问题。QFontMetrics.width() 与
        QLabel 实际渲染用的是同一把尺子，逐行取最大值最靠谱。
        """
        f = font or lb.font()
        fm = QFontMetrics(f)
        plain = re.sub(r'<br\s*/?>', '\n', html_text or '', flags=re.I)
        plain = html.unescape(re.sub(r'<[^>]+>', '', plain))
        need = max((fm.width(s) for s in plain.split('\n')), default=0)
        return int(min(max(64.0, need + MsgItem.BUBBLE_PAD),
                       float(max_bubble)))

    @classmethod
    def _bubble(cls, html_text, own, max_bubble, font=None):
        """文本气泡：宽度跟着文字走，窗口拉宽后由 reflow() 重新量"""
        lb = QLabel(html_text)
        lb.setObjectName('chatOwn' if own else 'chatPeer')
        lb.setWordWrap(True)
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse |
                                    Qt.LinksAccessibleByMouse)
        lb.setOpenExternalLinks(True)
        # 构造时 QLabel 还没挂进窗口树，lb.font() 是默认 12pt 而不是 QSS 的
        # 14px，量出来的宽度和实际渲染不一致 → 折行。统一用窗口探测到的字体。
        f = font or lb.font()
        lb.setFont(f)
        lb.ensurePolished()
        lb._bubble_html = html_text
        lb._bubble_font = f
        lb.setFixedWidth(cls.bubble_width(lb, html_text, max_bubble, f))
        return lb

    def reflow(self, max_bubble):
        """窗口尺寸变了：按新的宽度百分比上限重算气泡 / 卡片宽度

        QLabel 的 fixedWidth 是构造时写死的，只更新 _max_bubble 不会让已渲染
        的消息变宽；这里把当前窗口里所有气泡重新量一遍，窗口够宽时原本
        被迫换行的消息就会回到一行。媒体画框是固定尺寸，不需要重排。
        """
        b = self.body
        html_text = getattr(b, '_bubble_html', None)
        if html_text is not None:
            b.setFixedWidth(self.bubble_width(
                b, html_text, max_bubble, getattr(b, '_bubble_font', None)))
        elif isinstance(b, _LinkCard):
            b.reflow(max_bubble)

    def _build_body(self, ty, raw, ts, brief, own, ctx, max_bubble,
                    media_key=''):
        if ty == 10000:
            lb = QLabel(brief)
            lb.setObjectName('chatSys')
            lb.setWordWrap(True)
            lb.setMaximumWidth(max_bubble + 60)
            return lb

        if ty in (3, 47):            # 图片 / 表情：统一固定尺寸画框
            # 4.2 的本地文件名 hash 在 packed_info_data 里；表情消息没有，
            # 退回 XML 的 md5（老版本/表情缓存目录可能命中）
            mk = media_key if ty == 3 else ''
            box = EMOJI_BOX if ty == 47 else MEDIA_BOX
            ic = 'emoji' if ty == 47 else 'image'
            pix = ctx.media.image(raw, ts, mk) if ctx.media.root else None
            if pix is not None and not pix.isNull():
                # 消息流里只留 720px 小图，点开时才按原分辨率重解（大图才清晰）
                loader = (None if not ctx.media.root
                          else (lambda: ctx.media.full_image(raw, ts, mk)))
                source = (None if not ctx.media.root
                          else (lambda: ctx.media.image_path(raw, ts, mk)))
                return _MediaFrame(pix=pix, box=box, icon=ic, loader=loader,
                                   source=source, kind='image')
            # 区分「文件找到了但加密」和「文件本来就不在」；固定画框 + 单行话术
            st = ctx.media.state(raw, ts, mk) if ctx.media.root else 'missing'
            if st == 'locked':
                label = '图片已加密 · 需图片密钥'
            elif st == 'hevc':
                label = 'HEVC 高清图 · 暂不支持预览'
            elif st == 'undecodable':
                label = '图片格式暂不支持'
            else:
                label = '本地未找到图片'
            return _MediaFrame(box=box, icon=ic,
                               label='' if ty == 47 else label)

        if ty == 43:                  # 视频：固定画框 + 明文封面 + 播放角标
            pix, mp4 = (ctx.media.video(raw, ts, media_key)
                        if ctx.media.root else (None, ''))
            sec = video_seconds(raw)
            # 封面和 mp4 是两条独立信息：有视频没封面照样能播（标签写"点击播放"），
            # mp4 被微信清理了就别再画播放圆点，免得点了才发现是空的
            if pix is not None:
                label = ''
            elif mp4:
                label = '点击播放'
            else:
                # 时长已经由右下角标显示了，这里再写一遍会挤到它
                label = '视频未下载'
            # target 是「惰性归档」的 callable：消息流渲染不复制 mp4，
            # 点开播放 / 右键定位时才把它复制进程序媒体库并指向副本。
            target = ((lambda s=mp4, t=ts: ctx.media.archive(s, t))
                      if mp4 else '')
            return _MediaFrame(pix=pix, box=MEDIA_BOX, icon='videoplay',
                               label=label, target=target, seconds=sec,
                               play=bool(mp4), kind='video',
                               badge='未下载' if (pix is not None and not mp4)
                               else '')

        if ty == 34:                 # 语音：声波 + 时长
            return _VoiceCard(voice_seconds(raw), own)

        if ty == 49:                 # 链接 / 文件 / 小程序
            info = parse_appmsg(raw)
            title = info['title'] or brief or '[链接]'
            if info['filename']:
                path = ctx.media.file_path(raw, ts, info['filename'])
                desc = info['filename']
                if path:
                    try:
                        desc += ' · ' + fmt_size(os.path.getsize(path))
                    except OSError:
                        pass
                # 同视频：渲染时只判断文件在不在，点开/右键才归档到媒体库
                target = ((lambda s=path, t=ts: ctx.media.archive(s, t))
                          if path else '')
                return _LinkCard(title, desc, 'file', target,
                                 '点击打开本地文件' if path else '文件',
                                 max_width=max_bubble)
            desc = info['des'] or info['sourcedisplayname'] or ''
            tag = info['sourcedisplayname'] or info['appname'] or '链接'
            return _LinkCard(title, desc[:110], 'link', info['url'], tag,
                             max_width=max_bubble)

        if ty == 48:                 # 位置
            return _LinkCard(brief or '[位置]', '位置消息', 'link', '', '位置',
                             max_width=max_bubble)

        return self._bubble(_linkify(brief if brief else '[消息]'), own,
                            max_bubble, ctx.bubble_font)


# ---------------------------------------------------------------------------
# 对话框主窗口
# ---------------------------------------------------------------------------
class ChatWindow(QWidget):
    BATCH = 100        # 每批消息条数：初次 / 上滑加载 / 搜索窗口都用这个
    STEP = 20          # 每帧最多构造多少条，避免长时间占住主线程
    BUBBLE_RATIO = 0.62  # 气泡/卡片最大宽度 = 窗口宽度的固定百分比

    def __init__(self, sess, dec_dir, data, parent=None, media_root='',
                 self_wxid='', media_store=None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.Window)
        self.sess = sess
        self.dec_dir = dec_dir
        self.data = data
        self.is_group = sess.category == 'group'
        self._rows = []
        self._start = 0             # 已渲染区间左边界（含）
        self._end = 0               # 已渲染区间右边界（不含）
        self._row_widgets = {}
        self._resize_margin = 6
        self._hits = []
        self._hit_pos = -1
        self._search_on = False
        # 分批加载状态
        self._loading = False
        self._job = None
        self._dots = 0
        self._armed_top = True
        self._armed_bottom = True
        self._empty_text = '正在加载消息…'
        self._failed = ''            # 非空表示上次加载失败的原因

        self.ctx = _Ctx(dec_dir, media_root, self_wxid, self.is_group,
                        media_store=media_store)
        self.ctx.set_session(sess.username)

        self.setMinimumSize(400, 500)
        self.resize(480, 700)
        self._max_bubble = max(220, int(self.width() * self.BUBBLE_RATIO))
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(theme.chat_qss())
        # 气泡构造时 QLabel 还没挂进窗口树，拿不到 QSS 字体；先在已上样式的
        # 窗口上探一次真实的 14px 字体，后续所有气泡都用同一把尺子量宽度。
        probe = QLabel('', self)
        probe.setObjectName('chatPeer')
        probe.ensurePolished()
        self.ctx.bubble_font = QFont(probe.font())
        probe.setParent(None)
        probe.deleteLater()

        root = QFrame(self)
        root.setObjectName('chatRoot')
        rlay = QVBoxLayout(root)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.setSpacing(0)
        top = QVBoxLayout(self)
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(root)
        self._root = root

        self.title = _TitleBar(self, sess, self.ctx)
        rlay.addWidget(self.title)
        self._refresh_title_icons()
        rlay.addWidget(self._build_search())

        self.scroll = QScrollArea()
        self.scroll.setObjectName('chatScroll')
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.content.setObjectName('chatContent')
        self.clay = QVBoxLayout(self.content)
        self.clay.setContentsMargins(0, 8, 0, 8)
        self.clay.setSpacing(2)
        self.scroll.setWidget(self.content)
        rlay.addWidget(self.scroll, 1)

        rlay.addWidget(self._build_bottom())

        # 顶部常驻提示：既是「加载中」动画，也是手动加载更早消息的按钮
        self.top_hint = QPushButton('加载消息中…')
        self.top_hint.setObjectName('chatMore')
        self.top_hint.setCursor(Qt.PointingHandCursor)
        self.top_hint.clicked.connect(lambda: self._load_earlier(force=True))
        self._hint_wrap = self._center(self.top_hint)
        self.clay.addWidget(self._hint_wrap)
        placeholder = QLabel(self._empty_text)
        placeholder.setObjectName('chatSys')
        placeholder.setAlignment(Qt.AlignCenter)
        self.clay.addWidget(placeholder)
        self.clay.addStretch(1)

        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(260)
        self._anim_timer.timeout.connect(self._tick_loading)
        sb = self.scroll.verticalScrollBar()
        sb.valueChanged.connect(self._on_scroll)
        sb.rangeChanged.connect(self._on_range_changed)
        # 拖动改变窗口宽度时不要每帧重排，停手后统一按新百分比重算气泡
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.setInterval(120)
        self._reflow_timer.timeout.connect(self._reflow)
        # 初次加载完成后要停在最新消息，等滚动条算出真实范围再滚
        self._pending_bottom = False

        self.loader = _Loader(dec_dir, sess, data, self.ctx.self_wxid)
        self.loader.loaded.connect(self._on_loaded)
        self.loader.failed.connect(self._on_failed)
        if data is not None:
            self._start_loading()
            self.loader.start()
        else:
            self._empty_text = '数据未加载'
            self._update_hints()

        theme.BUS.changed.connect(self._on_theme)

    # ---- 搜索栏 ----
    def _build_search(self):
        self.search_bar = QFrame()
        self.search_bar.setObjectName('chatSearchBar')
        self.search_bar.setVisible(False)
        lay = QHBoxLayout(self.search_bar)
        lay.setContentsMargins(12, 7, 10, 7)
        lay.setSpacing(6)
        self.ed_search = QLineEdit()
        self.ed_search.setObjectName('chatSearchEdit')
        self.ed_search.setPlaceholderText('搜索聊天记录（支持空格分隔多个关键词）')
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.textChanged.connect(self._search_changed)
        self.ed_search.returnPressed.connect(lambda: self._goto_delta(1))
        lay.addWidget(self.ed_search, 1)
        self.lb_hits = QLabel('')
        self.lb_hits.setObjectName('chatSearchInfo')
        lay.addWidget(self.lb_hits)
        self.b_prev = QPushButton('↑')
        self.b_prev.setObjectName('chatSearchBtn')
        self.b_prev.setFixedWidth(28)
        self.b_prev.setToolTip('上一条')
        self.b_prev.clicked.connect(lambda: self._goto_delta(-1))
        lay.addWidget(self.b_prev)
        self.b_next = QPushButton('↓')
        self.b_next.setObjectName('chatSearchBtn')
        self.b_next.setFixedWidth(28)
        self.b_next.setToolTip('下一条')
        self.b_next.clicked.connect(lambda: self._goto_delta(1))
        lay.addWidget(self.b_next)
        b_close = QPushButton('✕')
        b_close.setObjectName('chatSearchBtn')
        b_close.setFixedWidth(28)
        b_close.setToolTip('关闭搜索')
        b_close.clicked.connect(self.toggle_search)
        lay.addWidget(b_close)
        return self.search_bar

    def toggle_search(self):
        self._search_on = not self._search_on
        self.search_bar.setVisible(self._search_on)
        if self._search_on:
            self.ed_search.setFocus()
            self._search_changed(self.ed_search.text())
        else:
            self.ed_search.clear()
            self._hits = []
            self._hit_pos = -1
            self._clear_highlight()
            self._refresh_title_icons()

    def _search_changed(self, text):
        """模糊匹配：不区分大小写的子串；空格分隔多词时要求全部命中"""
        self._clear_highlight()
        words = [w.lower() for w in (text or '').strip().split() if w]
        self._hits = []
        if words:
            for i, row in enumerate(self._rows):
                brief = (row[6] or '').lower()
                raw = (row[7] or '') if len(row) > 7 else ''
                # XML 类消息只取可见文本片段，别拿标签/属性当正文
                plain = re.sub(r'<[^>]+>', ' ', raw).lower()
                blob = brief + ' ' + plain + ' ' + (row[3] or '').lower()
                if all(w in blob for w in words):
                    self._hits.append(i)
        self._hit_pos = 0 if self._hits else -1
        self.lb_hits.setText('%d/%d' % (1, len(self._hits)) if self._hits else '无结果')
        if self._hits:
            self._goto_index(0)

    def _goto_delta(self, d):
        if not self._hits:
            return
        self._hit_pos = (self._hit_pos + d) % len(self._hits)
        self._goto_index(self._hit_pos)

    def _goto_index(self, pos):
        if not (0 <= pos < len(self._hits)):
            return
        idx = self._hits[pos]
        # 命中行不在当前窗口里：以它为中心重建窗口，而不是把 _start 一路
        # 拉到 0（那会一次渲染上万条，比不分页还慢）
        if idx < self._start or idx >= self._end:
            if self._loading and self._job:
                # 放弃还没跑完的分批加载，避免它和窗口重建同时改布局
                self._job = None
                self._stop_loading()
            n = len(self._rows)
            self._start = max(0, idx - 20)
            self._end = min(n, max(idx + self.BATCH,
                                   self._start + self.BATCH))
            self._render()
            self._update_hints()
        self._clear_highlight()
        w = self._row_widgets.get(idx)
        if w is not None:
            w.setProperty('hit', True)
            w.style().unpolish(w)
            w.style().polish(w)
            QTimer.singleShot(0, lambda w=w: self.scroll.ensureWidgetVisible(w, 0, 60))
        self.lb_hits.setText('%d/%d' % (pos + 1, len(self._hits)))

    def _clear_highlight(self):
        for w in self._row_widgets.values():
            if w.property('hit'):
                w.setProperty('hit', False)
                try:
                    w.style().unpolish(w)
                    w.style().polish(w)
                except RuntimeError:
                    pass

    # ---- 底栏：居中的导出按钮 ----
    def _build_bottom(self):
        bar = QFrame()
        bar.setObjectName('chatBottom')
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.addStretch(1)
        self.b_export = QPushButton('导出当前会话')
        self.b_export.setObjectName('chatExport')
        self.b_export.setCursor(Qt.PointingHandCursor)
        self.b_export.setToolTip('导出这个会话的聊天记录（导出清单只含当前会话）')
        self.b_export.clicked.connect(self._export_one)
        lay.addWidget(self.b_export)
        lay.addStretch(1)
        self._bottom = bar          # toast 要排在它上方，见 _toast_gap
        return bar

    @property
    def _toast_gap(self):
        """toast 距窗口底部的距离：抬到底部工具条上方，别压住导出按钮"""
        b = getattr(self, '_bottom', None)
        try:
            return max(34, b.height() + 12)
        except Exception:
            return 34

    def _export_one(self):
        """弹出现有导出窗口，清单里只放这一个会话（群聊就是这个群）"""
        mw = self.parent()
        fn = getattr(mw, '_do_export', None)
        if callable(fn):
            fn([self.sess])
        else:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.information(self, '无法导出', '主窗口尚未加载数据。')

    # ---- 主题 ----
    def _refresh_title_icons(self):
        if not hasattr(self, 'title'):
            return
        p = theme.palette()
        c = p['primary'] if self._search_on else p['sub']
        self.title.b_search.setIcon(icons.icon('search', c))
        self.title.b_search.setIconSize(QSize(18, 18))
        self.title.b_close.setIcon(icons.icon('close', p['sub']))
        self.title.b_close.setIconSize(QSize(16, 16))

    def _on_theme(self, _name):
        self.setStyleSheet(theme.chat_qss())
        self._refresh_title_icons()
        # 语音气泡是自绘的，重画一遍才能吃到新配色
        for w in self._row_widgets.values():
            w.update()
        self.content.update()

    # ---- 消息渲染（滚动窗口） ----
    def _on_loaded(self, rows):
        self._rows = rows or []
        self._failed = ''
        n = len(self._rows)
        self._end = n
        self._start = max(0, n - self.BATCH)
        if n == 0:
            # 查到了但确实没有消息：别再说"正在加载"，否则和顶部提示重复
            self._empty_text = '这个会话没有可显示的消息'
        self._stop_loading()
        self._render()
        self._update_hints()
        # 初次要停在最后 100 条的最新一条；布局高度要过一帧才算得出来
        self._pending_bottom = True
        QTimer.singleShot(0, self._scroll_bottom)

    def _on_failed(self, e):
        self._failed = str(e)
        self._empty_text = ('加载失败：%s\n（关掉窗口重新双击可重试）' % e)
        self._stop_loading()          # 内部会 _update_hints，空结果时隐藏顶部提示
        self._render()

    # ---- 重新加载（关掉再打开时若上次是空的，自动重试） ----
    def needs_reload(self):
        return (not self._rows) or bool(self._failed)

    def reload(self):
        """重新起一个加载线程；用于上次空结果 / 失败后的恢复"""
        if self._loading:
            return
        self._rows = []
        self._failed = ''
        self._empty_text = '正在加载消息…'
        self._render()
        self._start_loading()
        self.loader = _Loader(self.dec_dir, self.sess, self.data,
                              self.ctx.self_wxid)
        self.loader.loaded.connect(self._on_loaded)
        self.loader.failed.connect(self._on_failed)
        self.loader.start()

    def refresh_media(self):
        """刚抓到图片密钥：清掉解不出来的缓存并重建消息，图片立刻可见"""
        key = self.ctx.media.reload_key()
        if not self._rows:
            return key
        self._row_widgets = {}
        self._render()
        QTimer.singleShot(0, self._reflow)
        return key

    def _scroll_bottom(self):
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_range_changed(self, _min, _max):
        """QScrollArea 算完真实高度才会发 rangeChanged，这时滚到底才有效

        只在初次加载时生效一次；分批插入顶部内容时也会发这个信号，
        但 _pending_bottom 已是 False，滚动位置交给锚点逻辑去保持。
        """
        if self._pending_bottom and _max > 0:
            self._pending_bottom = False
            self.scroll.verticalScrollBar().setValue(_max)

    def _is_own(self, row):
        """自己发的消息一律靠右：scan_chat 已标 (自己)，这里再按 wxid 兜一次"""
        if row[3] == '(自己)':
            return True
        return messages.is_self_wxid(row[2], self.ctx.self_wxid)

    def _make_range(self, a, b):
        """构造 [a, b) 的 widget 列表，并把消息 widget 登记进 _row_widgets"""
        out = []
        prev_ts = self._rows[a - 1][1] if a > 0 else None
        for i in range(a, b):
            row = self._rows[i]
            ts = row[1]
            if prev_ts is None or (ts - prev_ts) > 300:
                pill = QLabel(fmt_chat_time(ts))
                pill.setObjectName('chatTime')
                pill.setAlignment(Qt.AlignCenter)
                out.append(self._center(pill))
            prev_ts = ts

            if row[4] == 10000:
                sys_lb = QLabel(row[6])
                sys_lb.setObjectName('chatSys')
                sys_lb.setWordWrap(True)
                sys_lb.setAlignment(Qt.AlignCenter)
                wrap = self._center(sys_lb)
                wrap.row_index = i
                out.append(wrap)
                self._row_widgets[i] = wrap
                continue

            item = MsgItem(row, self._is_own(row), self.ctx, self._max_bubble)
            item.row_index = i
            out.append(item)
            self._row_widgets[i] = item
        return out

    def _render(self):
        """全量重建当前窗口（顶部提示 widget 保留，其它清掉）"""
        for i in reversed(range(self.clay.count())):
            it = self.clay.itemAt(i)
            w = it.widget() if it is not None else None
            if w is self._hint_wrap:
                continue
            self.clay.removeItem(it)
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._row_widgets = {}

        if not self._rows:
            box = QWidget()
            v = QVBoxLayout(box)
            v.setContentsMargins(20, 0, 20, 0)
            v.setSpacing(10)
            lb = QLabel(self._empty_text)
            lb.setObjectName('chatSys')
            lb.setAlignment(Qt.AlignCenter)
            lb.setWordWrap(True)
            v.addWidget(lb)
            if self._failed or self._empty_text != '正在加载消息…':
                b = QPushButton('重新加载')
                b.setObjectName('chatMore')
                b.setCursor(Qt.PointingHandCursor)
                b.clicked.connect(self.reload)
                v.addWidget(b, 0, Qt.AlignCenter)
            self.clay.addWidget(box)
            self.clay.addStretch(1)
            return

        for w in self._make_range(self._start, self._end):
            self.clay.addWidget(w)
        self.clay.addStretch(1)
        # 新一批气泡挂进窗口树后再量一次，确保用真实 QSS 字体定宽（不折行）
        QTimer.singleShot(0, self._reflow)

    # ---- loading 动画 ----
    def _start_loading(self):
        self._loading = True
        self._dots = 0
        self._hint_wrap.setVisible(True)
        self.top_hint.setEnabled(False)
        self.top_hint.setText('正在加载消息·')
        self._anim_timer.start()

    def _tick_loading(self):
        self._dots = (self._dots + 1) % 4
        self.top_hint.setText('正在加载消息' + '·' * (self._dots + 1))

    def _stop_loading(self):
        self._loading = False
        self._anim_timer.stop()
        self.top_hint.setEnabled(True)
        self._update_hints()

    def _update_hints(self):
        if self._loading:
            return
        if not self._rows:
            # 空结果只留中间那一句说明：顶部再挂一个同样的话会变成两条
            # "加载消息中"，用户以为程序卡死。
            self._hint_wrap.setVisible(False)
            self.top_hint.setText(self._empty_text)
            self.top_hint.setEnabled(False)
            return
        self._hint_wrap.setVisible(True)
        if self._start > 0:
            self.top_hint.setText('↑ 上滑或点击，加载更早的 %d 条' % self.BATCH)
            self.top_hint.setEnabled(True)
        else:
            self.top_hint.setText('已经到最早的消息')
            self.top_hint.setEnabled(False)

    # ---- 分批加载：每帧 STEP 条，期间界面照常可点可滚 ----
    def _load_earlier(self, force=False):
        if self._loading or self._start <= 0 or not self._rows:
            return
        self._begin_job('earlier', max(0, self._start - self.BATCH))

    def _load_later(self):
        if self._loading or self._end >= len(self._rows) or not self._rows:
            return
        self._begin_job('later', min(len(self._rows), self._end + self.BATCH))

    def _begin_job(self, kind, target):
        self._start_loading()
        sb = self.scroll.verticalScrollBar()
        # 锚点 = 加载前视口里最靠上的一条消息；插入后让它在视口里的 y 不变
        anchor = None
        for i in range(self._start, self._end):
            w = self._row_widgets.get(i)
            if w is None:
                continue
            try:
                y = w.mapTo(self.content, QPoint(0, 0)).y()
            except RuntimeError:
                continue
            if y >= sb.value():
                anchor = w
                break
        if anchor is None:
            anchor = self._row_widgets.get(self._start)
        anchor_y = 0
        if anchor is not None:
            try:
                anchor_y = anchor.mapTo(self.content, QPoint(0, 0)).y()
            except RuntimeError:
                anchor = None
        self._job = {
            'kind': kind,
            'target': target,
            'cursor': self._start if kind == 'earlier' else self._end,
            'anchor': anchor,
            'anchor_view': anchor_y - sb.value(),
        }
        QTimer.singleShot(0, self._job_step)

    def _job_step(self):
        job = self._job
        if not job:
            return
        kind = job['kind']
        if kind == 'earlier':
            k = min(self.STEP, job['cursor'] - job['target'])
            if k <= 0:
                QTimer.singleShot(0, self._finish_job)
                return
            lo = job['cursor'] - k
            widgets = self._make_range(lo, job['cursor'])
            for j, w in enumerate(widgets):
                self.clay.insertWidget(1 + j, w)
            job['cursor'] = lo
        else:
            k = min(self.STEP, job['target'] - job['cursor'])
            if k <= 0:
                QTimer.singleShot(0, self._finish_job)
                return
            hi = job['cursor'] + k
            widgets = self._make_range(job['cursor'], hi)
            pos = self.clay.count() - 1      # 末尾 stretch 之前
            for j, w in enumerate(widgets):
                self.clay.insertWidget(pos + j, w)
            job['cursor'] = hi

        QTimer.singleShot(0, self._keep_anchor)
        more = ((kind == 'earlier' and job['cursor'] > job['target']) or
                (kind == 'later' and job['cursor'] < job['target']))
        QTimer.singleShot(0, self._job_step if more else self._finish_job)

    def _keep_anchor(self):
        job = self._job
        if not job or job.get('anchor') is None:
            return
        w = job['anchor']
        try:
            y = w.mapTo(self.content, QPoint(0, 0)).y()
        except RuntimeError:
            return
        sb = self.scroll.verticalScrollBar()
        want = y - job['anchor_view']
        if want != sb.value():
            sb.setValue(want)

    def _finish_job(self):
        job = self._job or {}
        if job.get('kind') == 'earlier':
            self._start = job.get('target', self._start)
        elif job.get('kind') == 'later':
            self._end = job.get('target', self._end)
        self._job = None
        self._stop_loading()
        # 一次滚动只加载一批；用户离开顶部/底部一小段后再重新武装
        self._armed_top = False
        self._armed_bottom = False

    def _on_scroll(self, v):
        sb = self.scroll.verticalScrollBar()
        mx = sb.maximum()
        if v > 30:
            self._armed_top = True
        if v < mx - 30:
            self._armed_bottom = True
        if self._loading or not self._rows or mx <= 0:
            return
        if self._armed_top and v <= 24 and self._start > 0:
            self._armed_top = False
            self._load_earlier()
        elif (self._armed_bottom and v >= mx - 24
              and self._end < len(self._rows)):
            self._armed_bottom = False
            self._load_later()

    def resizeEvent(self, e):
        # 不重建消息（会丢滚动位置），只按新的窗口宽度重算上限
        self._max_bubble = max(220, int(self.width() * self.BUBBLE_RATIO))
        t = getattr(self, '_reflow_timer', None)
        if t is not None:
            t.start(120)
        super().resizeEvent(e)

    def _reflow(self):
        """窗口宽度变了：把已渲染的气泡 / 卡片按新上限重新定宽

        窗口拉宽时上限变大，原本被迫换行的中长消息就能回到一行；
        拉窄时同理收回，避免气泡超出可视区。
        """
        for w in self._row_widgets.values():
            if isinstance(w, MsgItem):
                w.reflow(self._max_bubble)
        self.content.updateGeometry()

    @staticmethod
    def _center(widget):
        w = QWidget()
        w.setAttribute(Qt.WA_StyledBackground, True)
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addStretch(1)
        h.addWidget(widget)
        h.addStretch(1)
        return w

    # ---- Windows 原生八方向缩放（非 Win 下退回系统默认）----
    HTCLIENT = 1
    HTLEFT, HTRIGHT, HTTOP = 10, 11, 12
    HTTOPLEFT, HTTOPRIGHT = 13, 14
    HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17

    def nativeEvent(self, eventType, message):
        if sys.platform == 'win32' and eventType == 'windows_generic_MSG':
            try:
                from ctypes.wintypes import MSG
                msg = MSG.from_address(int(message))
            except Exception:
                return super().nativeEvent(eventType, message)
            if msg.message == 0x0084:  # WM_NCHITTEST
                # ⚠️ 这里以前是自己从 lParam 拆屏幕坐标，两个坑：
                #   1) lParam 的两段是**有符号** 16 位，直接 & 0xFFFF 时，
                #      窗口在负坐标的副屏（本机就是 -1920 起）会被算成 6 万多；
                #   2) Win32 消息给的是物理像素，mapFromGlobal 要逻辑像素，
                #      DPI 缩放 125%/150% 下整体错位。
                # 两个坑的表现完全一样：rx 恒大于 w-6 → 返回 HTRIGHT，
                # 于是**鼠标在窗口任何位置都是左右箭头，点击还被当成拖拽**，
                # 关闭按钮自然怎么点都没反应。用 Qt 自己的光标坐标（已经是
                # 逻辑像素、允许负数）就没有换算了。
                return True, self.hit_test_code(
                    self.mapFromGlobal(QCursor.pos()))
        return super().nativeEvent(eventType, message)

    def hit_test_code(self, loc):
        """窗口坐标 loc 处应该返回哪个 HT 码（拆出来方便单测）

        · 最大化/全屏：没有可拖的边，一律算客户区
        · 边缘 6px 内压着按钮/输入框：算客户区，否则按钮点不动
        """
        if self.isMaximized() or self.isFullScreen():
            return self.HTCLIENT
        if not self.rect().adjusted(-2, -2, 2, 2).contains(loc):
            return self.HTCLIENT
        m, w, h = self._resize_margin, self.width(), self.height()
        rx, ry = loc.x(), loc.y()
        L, R, T, B = rx < m, rx > w - m, ry < m, ry > h - m
        if (L or R or T or B) and self._hit_button(loc):
            return self.HTCLIENT
        if L and T:
            return self.HTTOPLEFT
        if R and T:
            return self.HTTOPRIGHT
        if L and B:
            return self.HTBOTTOMLEFT
        if R and B:
            return self.HTBOTTOMRIGHT
        if L:
            return self.HTLEFT
        if R:
            return self.HTRIGHT
        if T:
            return self.HTTOP
        if B:
            return self.HTBOTTOM
        return self.HTCLIENT

    def _hit_button(self, loc):
        """loc 处是否压着可交互控件（按钮 / 输入框）——这些地方不能当边框拖拽"""
        try:
            w = self.childAt(loc)
        except Exception:
            return False
        while w is not None and w is not self:
            if w.inherits('QPushButton') or w.inherits('QLineEdit'):
                return True
            w = w.parentWidget()
        return False
