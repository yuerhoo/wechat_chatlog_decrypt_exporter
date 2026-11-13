# -*- coding: utf-8 -*-
"""看图窗口：滚轮缩放 · 拖拽平移 · 旋转 · 另存为 · 打开所在文件夹

早期版本是「全屏无边框遮罩」，实际用起来三个硬伤：
    · 铺满整块屏幕，看着像卡住了；没有标题栏就没有最小化 / 最大化 / 还原
    · 全屏无边框窗口在 Windows 上四边仍挂着 resize 命中区，鼠标一靠近
      边缘就变成左右箭头，连右上角的 ✕ 都会被判成拖拽
    · 图片稍微小一点，中间一小块图配一大片黑，很不体面

现在是普通窗口：系统标题栏（最小化 / 最大化 / 关闭由系统提供）+ 可拖动
边框 + 自由缩放，默认尺寸按图片给但最多 1180×830、不超屏幕 86%。

配色**完全跟随当前主题皮肤**（以前这里写死了一整套浅蓝灰，用户切成橙/黑
主题后这个窗口就成了唯一的"外来户"）：画布用 bg、工具条用 card、按钮和
浮层用 line/soft 派生色，换主题时通过 BUS.changed 即时重刷，不用重开窗口。

交互：
    · 滚轮以光标为锚点缩放 0.02x ~ 16x，拖拽平移，双击在「适应 / 1:1」切换
    · 窗口拉伸时，如果用户没有手动缩放过，会自动重新适应窗口
    · 底部工具条按钮组严格居中（左右等权 stretch），窗口多宽都在正中
    · 键盘：Esc 关闭、+ 放大、- 缩小、0 适应窗口、1 原始像素、R 旋转

用法：ImageViewer.show_image(pixmap, name='', path='', parent=None)
    path 是这张图在**程序媒体库**里已解密的 .jpg/.png 绝对路径，
    「打开所在文件夹」用它在资源管理器里定位并选中该文件；parent 一般传发起点击的聊天窗口，
    看图窗口会贴着它居中，多屏时不会跑错屏幕。
"""
import os
import sys

from PyQt5.QtCore import Qt, QPointF, QRectF, QTimer, QUrl
from PyQt5.QtGui import (QColor, QCursor, QPainter, QPixmap,
                         QTransform)
from PyQt5.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout,
                             QLabel, QPushButton, QVBoxLayout, QWidget)

# 「打开所在文件夹」和视频播放共用同一套 shell 调用（别在这里另写 Popen）
from .shell_open import (last_reveal_error, path_exists,
                         reveal_in_explorer)
from . import theme

MIN_SCALE = 0.02
MAX_SCALE = 16.0


def _btn_qss(p):
    """工具条按钮：全部取主题令牌，圆角/间距和主窗口按钮保持一致"""
    return """
QPushButton {
    color: %(text)s;
    background: %(card)s;
    border: 1px solid %(line)s;
    border-radius: 8px;
    padding: 5px 12px;
    font-family: 'Microsoft YaHei';
    font-size: 12px;
}
QPushButton:hover { background: %(hover_bg)s; border-color: %(border)s; }
QPushButton:pressed { background: %(soft)s; }
QPushButton:focus { border-color: %(primary)s; outline: none; }
QPushButton:disabled { color: %(sub)s; background: %(card)s; border-color: %(line)s; }
""" % p


def _info_qss(p):
    """画布左上角的尺寸标签：低调的卡片"""
    return ("color:%s; font-family:'Microsoft YaHei'; font-size:12px;"
            " background:%s; border:1px solid %s; border-radius:6px;"
            " padding:3px 8px;" % (p['sub'], p['card'], p['line']))


def _tip_qss(p):
    """底部提示浮层：主题浅色底 + 主色左标，比原来的深色气泡更贴合皮肤"""
    return ("color:%s; font-family:'Microsoft YaHei'; font-size:12px;"
            " background:%s; border:1px solid %s; border-left:3px solid %s;"
            " border-radius:8px; padding:6px 12px;"
            % (p['text'], p['soft'], p['border'], p['primary']))


class _Canvas(QWidget):
    """图像绘制与鼠标事件的落点"""

    def __init__(self, viewer):
        super().__init__(viewer)
        self.v = viewer
        self.setMouseTracking(True)
        self.setCursor(Qt.OpenHandCursor)
        self.setAutoFillBackground(False)

    def paintEvent(self, e):
        self.v.paint_image(self)

    def wheelEvent(self, e):
        self.v.on_wheel(e)

    def mousePressEvent(self, e):
        self.v.on_press(e)

    def mouseMoveEvent(self, e):
        self.v.on_move(e)

    def mouseReleaseEvent(self, e):
        self.v.on_release(e)

    def mouseDoubleClickEvent(self, e):
        self.v.on_double(e)


class ImageViewer(QWidget):
    """单例式看图窗口：同一时刻只留一个"""

    _inst = None

    def __init__(self, pixmap, name='', path='', parent=None):
        # 不带 FramelessWindowHint：标题栏、边框、三按钮都交给系统，
        # 缩放和关闭的行为才和用户预期一致
        super().__init__(parent, Qt.Window)
        self.pix = QPixmap(pixmap)
        self.name = name or ''
        self.path = path if path and path_exists(path) else ''

        self.scale = 1.0
        self.rotation = 0
        self.offset = QPointF(0.0, 0.0)     # 图像中心相对视口中心的位移
        self._last = None
        self._moved = False
        self._auto_fit = True               # 用户没手动缩放过：窗口变化时自动适应
        self._ready = False
        self._p = theme.palette()           # 自绘画布用，QSS 覆盖不到

        self.setWindowTitle(self._title())
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(640, 460)

        self._build_ui()
        self._apply_theme()
        # 全局换肤时即时重刷；窗口销毁后 Qt 会自动断开这个连接
        theme.BUS.changed.connect(self._apply_theme)
        self._place_window()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.canvas = _Canvas(self)
        root.addWidget(self.canvas, 1)

        self.bar = QFrame()
        self.bar.setObjectName('imgBar')
        self.bar.setFixedHeight(52)
        bl = QHBoxLayout(self.bar)
        bl.setContentsMargins(18, 10, 18, 10)
        bl.setSpacing(10)

        # 左右各一个等权 stretch：不管窗口多宽，按钮组都落在正中
        bl.addStretch(1)
        self.btn_out = self._btn('−', lambda: self.zoom_by(1 / 1.2), '缩小 (-)')
        self.btn_out.setFixedWidth(34)
        self.lb_pct = QLabel('100%')
        self.lb_pct.setAlignment(Qt.AlignCenter)
        self.lb_pct.setFixedWidth(52)
        self.btn_in = self._btn('+', lambda: self.zoom_by(1.2), '放大 (+)')
        self.btn_in.setFixedWidth(34)
        for w in (self.btn_out, self.lb_pct, self.btn_in):
            bl.addWidget(w)

        self._sep = QFrame()
        self._sep.setFixedWidth(1)
        self._sep.setFixedHeight(18)
        bl.addWidget(self._sep)

        for w in (self._btn('适应窗口', self.fit, '缩放到刚好放得下 (0)'),
                  self._btn('1:1', self.actual, '原始像素 (1)'),
                  self._btn('旋转', lambda: self.rotate(90), '顺时针旋转 90° (R)'),
                  self._btn('另存为', self.save_as, '把当前图片另存到本地')):
            bl.addWidget(w)
        self.btn_open = self._btn('打开所在文件夹', self.open_location,
                                  '在资源管理器中定位并选中已解密的图片文件\n'
                                  '（程序媒体库里的 .jpg/.png，不是微信的 .dat）')
        self.btn_open.setEnabled(bool(self.path))
        if not self.path:
            self.btn_open.setToolTip(
                '这张图还没解密到媒体库，没有可定位的文件')
        bl.addWidget(self.btn_open)
        bl.addStretch(1)

        root.addWidget(self.bar)

        # 尺寸信息与提示都浮在画布上，不参与工具条布局——否则会把按钮组挤偏
        self.lb_info = QLabel('', self.canvas)
        self.lb_info.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self.lb_tip = QLabel('', self.canvas)
        # 定位结果要显示完整路径（用户需要看清文件在哪），允许自动换行
        self.lb_tip.setWordWrap(True)
        self.lb_tip.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.lb_tip.hide()

        self._tip_timer = QTimer(self)
        self._tip_timer.setSingleShot(True)
        self._tip_timer.timeout.connect(self.lb_tip.hide)

    def _apply_theme(self, *_args):
        """换肤：画布 / 工具条 / 按钮 / 浮层一次性重刷

        QSS 能覆盖控件，但画布是自绘的、工具条有自己的局部样式表，
        所以这里显式重设，保证 9 套主题下这个窗口都不是"外来户"。
        """
        p = theme.palette()
        self._p = p
        self.setStyleSheet('background: %s;' % p['bg'])
        self.bar.setStyleSheet(
            '#imgBar { background: %s; border-top: 1px solid %s; }'
            % (p['card'], p['line']) + _btn_qss(p))
        self.lb_pct.setStyleSheet(
            "color:%s; font-family:'Microsoft YaHei'; font-size:12px;"
            % p['sub'])
        self._sep.setStyleSheet('background: %s;' % p['line'])
        self.lb_info.setStyleSheet(_info_qss(p))
        self.lb_tip.setStyleSheet(_tip_qss(p))
        self.canvas.update()
        if self.lb_tip.isVisible():
            self._place_tip()

    def _btn(self, text, slot, tip=''):
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setFocusPolicy(Qt.NoFocus)
        b.clicked.connect(slot)
        if tip:
            b.setToolTip(tip)
        return b

    def _title(self):
        base = os.path.basename(self.name) if self.name else ''
        return '查看图片 - %s' % base if base else '查看图片'

    def _place_window(self):
        """默认尺寸：图片等比缩到 1120×760 以内再各留一点边距

        不按原图像素开窗——手机原图动辄 4000×3000，直接照搬就又是一块
        铺满屏幕的大窗口。窗口默认不超过 1180×830，也不超过屏幕的 86%。
        """
        sc = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        host = self.parentWidget()
        if host is not None and host.isVisible():
            host_frame = host.window().frameGeometry()
            sc = QApplication.screenAt(host_frame.center()) or sc
        avail = sc.availableGeometry() if sc else self.geometry()
        pm = self.pix
        iw = pm.width() if pm is not None and not pm.isNull() else 1000
        ih = pm.height() if pm is not None and not pm.isNull() else 700
        k = min(1.0, 1120.0 / max(1, iw), 760.0 / max(1, ih))
        w = int(iw * k) + 40
        h = int(ih * k) + 52 + 48
        w = min(max(w, 700), 1180, max(700, int(avail.width() * 0.86)))
        h = min(max(h, 520), 830, max(520, int(avail.height() * 0.86)))
        self.resize(w, h)
        anchor = (self.parentWidget().window().frameGeometry().center()
                  if host is not None and host.isVisible() else avail.center())
        x = max(avail.x(), min(anchor.x() - w // 2,
                               avail.x() + avail.width() - w))
        y = max(avail.y(), min(anchor.y() - h // 2,
                               avail.y() + avail.height() - h))
        self.move(x, y)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        w = self.canvas.width()
        self.lb_info.move(14, 12)
        if self.lb_tip.isVisible():
            self._place_tip()
        if not self._ready and w > 100:
            self._ready = True
            self._fit_initial()
        elif self._auto_fit:
            # 用户只是拉伸窗口：保持"整张图刚好放得下"的观感
            self.scale = max(MIN_SCALE, self.fit_scale())
            self.offset = QPointF(0.0, 0.0)
            self._sync_ui()

    def _place_tip(self):
        """提示浮层停在画布底部居中，离工具条 16px"""
        self.lb_tip.setMaximumWidth(max(240, self.canvas.width() - 32))
        self.lb_tip.adjustSize()
        self.lb_tip.move(
            max(8, (self.canvas.width() - self.lb_tip.width()) // 2),
            max(8, self.canvas.height() - self.lb_tip.height() - 16))

    # -------------------------------------------------------------- 视图计算
    def _rotated_size(self):
        pm = self.pix
        w, h = pm.width(), pm.height()
        if self.rotation % 180:
            w, h = h, w
        return w, h

    def fit_scale(self):
        if self.pix is None or self.pix.isNull():
            return 1.0
        w, h = self._rotated_size()
        vw = max(1, self.canvas.width() - 64)
        vh = max(1, self.canvas.height() - 56)
        return min(vw / float(w), vh / float(h))

    def _fit_initial(self):
        self.scale = max(MIN_SCALE, min(self.fit_scale(), 1.0))
        self.offset = QPointF(0.0, 0.0)
        self._sync_ui()

    def image_rect(self):
        """当前缩放下图像在视口里占据的矩形（未旋转时精确，旋转时按外接框）"""
        w, h = self._rotated_size()
        w *= self.scale
        h *= self.scale
        c = QPointF(self.canvas.width() / 2.0,
                    self.canvas.height() / 2.0) + self.offset
        return QRectF(c.x() - w / 2.0, c.y() - h / 2.0, w, h)

    def set_scale(self, s, anchor=None):
        s = max(MIN_SCALE, min(MAX_SCALE, s))
        if abs(s - self.scale) < 1e-6:
            return
        vw, vh = self.canvas.width(), self.canvas.height()
        center = QPointF(vw / 2.0, vh / 2.0)
        if anchor is None:
            anchor = center
        # 让 anchor 下面的那个图像点在新比例下仍然停在 anchor 上
        k = s / self.scale
        self.offset = anchor - (anchor - (center + self.offset)) * k - center
        self.scale = s
        self._auto_fit = False
        self._sync_ui()

    def zoom_by(self, factor, anchor=None):
        self.set_scale(self.scale * factor, anchor)

    def fit(self):
        self.scale = max(MIN_SCALE, self.fit_scale())
        self.offset = QPointF(0.0, 0.0)
        self._auto_fit = True
        self._sync_ui()

    def actual(self):
        self.scale = 1.0
        self.offset = QPointF(0.0, 0.0)
        self._auto_fit = False
        self._sync_ui()

    def rotate(self, deg=90):
        self.rotation = (self.rotation + deg) % 360
        self.fit()

    def _sync_ui(self):
        self.lb_pct.setText('%d%%' % round(self.scale * 100))
        self.lb_info.setText(self._info_text())
        self.lb_info.adjustSize()
        self.btn_out.setEnabled(self.scale > MIN_SCALE * 1.01)
        self.btn_in.setEnabled(self.scale < MAX_SCALE * 0.99)
        self.canvas.update()

    def _info_text(self):
        pm = self.pix
        if pm is None or pm.isNull():
            return ''
        w, h = self._rotated_size()
        return '%d × %d' % (w, h)

    # ---------------------------------------------------------------- 绘制
    def paint_image(self, widget):
        p = QPainter(widget)
        c = self._p
        p.fillRect(widget.rect(), QColor(c['bg']))
        pm = self.pix
        if pm is None or pm.isNull():
            p.end()
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = self.image_rect()
        # 很淡的一层投影：浅底上用黑、深底上用白，边界才清晰又不抢眼
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 20) if c['dark']
                   else QColor(17, 24, 39, 22))
        p.drawRoundedRect(rect.adjusted(-1, 1, 1, 5), 3, 3)
        p.setBrush(Qt.NoBrush)

        center = QPointF(widget.width() / 2.0,
                         widget.height() / 2.0) + self.offset
        p.translate(center)
        p.scale(self.scale, self.scale)
        p.rotate(self.rotation)
        p.translate(-pm.width() / 2.0, -pm.height() / 2.0)
        p.drawPixmap(0, 0, pm)
        p.end()

    # ---------------------------------------------------------------- 事件
    def on_wheel(self, e):
        dy = e.angleDelta().y()
        if not dy:
            return
        self.set_scale(self.scale * (1.18 ** (dy / 120.0)),
                       QPointF(e.pos()))

    def on_press(self, e):
        if e.button() == Qt.LeftButton:
            self._last = e.pos()
            self._moved = False
            self.canvas.setCursor(Qt.ClosedHandCursor)

    def on_move(self, e):
        if self._last is None:
            return
        d = e.pos() - self._last
        if not self._moved and (abs(d.x()) > 3 or abs(d.y()) > 3):
            self._moved = True
        if self._moved:
            self.offset += QPointF(d.x(), d.y())
            self.canvas.update()
        self._last = e.pos()

    def on_release(self, e):
        self._last = None
        self._moved = False
        self.canvas.setCursor(Qt.OpenHandCursor)

    def on_double(self, e):
        fit = min(self.fit_scale(), 1.0)
        if abs(self.scale - fit) < 0.02 and self.fit_scale() < 1.0:
            self.set_scale(1.0, QPointF(e.pos()))
        else:
            self.fit()

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key_Escape:
            self.close()
        elif k in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom_by(1.2)
        elif k in (Qt.Key_Minus, Qt.Key_Underscore):
            self.zoom_by(1 / 1.2)
        elif k == Qt.Key_0:
            self.fit()
        elif k == Qt.Key_1:
            self.actual()
        elif k == Qt.Key_R:
            self.rotate(90)
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        if ImageViewer._inst is self:
            ImageViewer._inst = None
        super().closeEvent(e)

    # ---------------------------------------------------------------- 保存
    def save_as(self):
        pm = self.pix
        if pm is None or pm.isNull():
            return
        if self.rotation:
            pm = pm.transformed(QTransform().rotate(self.rotation),
                                Qt.SmoothTransformation)
        base = self.name or '微信图片'
        base = os.path.splitext(os.path.basename(base))[0]
        # 默认落在媒体库里这张已解密图的同目录，别再让用户从「图片」翻起
        start_dir = (os.path.dirname(self.path) if self.path and
                     os.path.isdir(os.path.dirname(self.path))
                     else os.path.join(os.path.expanduser('~'), 'Pictures'))
        path = os.path.join(start_dir, base + '.png')
        out, _flt = QFileDialog.getSaveFileName(
            self, '另存为', path, 'PNG 图片 (*.png);;JPEG 图片 (*.jpg)')
        if not out:
            return
        if pm.save(out):
            self.show_tip('已保存：%s' % os.path.basename(out))
        else:
            self.show_tip('保存失败')

    # ------------------------------------------------------- 打开所在文件夹
    def open_location(self):
        """在资源管理器里**选中**媒体库中已解密的那一个文件

        提示里带完整路径：用户需要确认定位的是程序媒体库里的 .jpg/.png，
        而不是微信原始目录里的 .dat；失败时也要能看到路径和具体原因。
        """
        if not self.path or not path_exists(self.path):
            self.show_tip('找不到已解密的图片文件\n%s'
                          % (self.path or '（这张图还没归档到媒体库）'))
            return
        if reveal_in_explorer(self.path):
            self.show_tip('已定位并选中：%s\n%s'
                          % (os.path.basename(self.path), self.path))
        else:
            self.show_tip('定位失败（%s）：\n%s'
                          % (last_reveal_error() or '未知原因', self.path))

    def show_tip(self, text):
        self.lb_tip.setText(text)
        self._place_tip()
        self.lb_tip.show()
        self.lb_tip.raise_()
        # 带完整路径的两行提示需要多留一会儿才看得完
        self._tip_timer.start(4800 if '\n' in text else 2600)

    # ---------------------------------------------------------------- 入口
    @classmethod
    def show_image(cls, pixmap, name='', path='', parent=None):
        if pixmap is None or pixmap.isNull():
            return None
        if cls._inst is not None:
            try:
                cls._inst.close()
            except Exception:
                pass
        win = cls(pixmap, name, path, parent)
        cls._inst = win
        win.show()
        # show() 之后 canvas 才有真实尺寸，这里补一次"适应窗口"
        if not win._ready and win.canvas.width() > 100:
            win._ready = True
            win._fit_initial()
        win.raise_()
        win.activateWindow()
        win.setFocus()
        return win
