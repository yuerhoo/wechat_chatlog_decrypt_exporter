# -*- coding: utf-8 -*-
"""轻量 toast：在窗口底部中间浮一条圆角提示，1.7 秒后淡出

为什么不用 QToolTip.showText()：
    · 它贴在**鼠标位置**弹系统气泡，"已复制路径"贴在手边上像是报错；
    · 鼠标一动就被下一次 hover 顶掉，经常来不及看清；
    · 消息区是 QScrollArea，tooltip 会被裁切。

配色跟随当前主题：浅色主题保留深色气泡（对比最强），深色主题换成
带细边的深灰卡片，否则深色气泡会和深色窗口糊成一片。

用法：Toast.show_for(widget, '已复制路径')
      同一个窗口只会有一个 toast，连续调用直接换文字、重置计时。
"""
from PyQt5.QtCore import QPoint, QPropertyAnimation, Qt, QTimer
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtWidgets import QGraphicsOpacityEffect, QLabel

_QSS = """
QLabel {
    background: %(bg)s;
    color: %(fg)s;
    border: %(border)s;
    border-radius: 14px;
    padding: 0 14px;
    font-family: "Microsoft YaHei";
    font-size: 12px;
}
"""


def _qss():
    """每次弹出时取一次当前主题，换肤后不用等窗口重建"""
    from . import theme
    p = theme.palette()
    if p['dark']:
        return _QSS % {'bg': '#2F3742', 'fg': p['text'],
                       'border': '1px solid %s' % p['line']}
    return _QSS % {'bg': 'rgba(34, 39, 50, 236)', 'fg': '#FFFFFF',
                   'border': 'none'}


class Toast(QLabel):
    LIFE = 1700          # 停留时长（毫秒），之后开始淡出
    FADE = 320           # 淡出时长
    HEIGHT = 30
    BOTTOM_GAP = 34      # 距窗口底部的距离

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(_qss())
        self.hide()

        self._eff = QGraphicsOpacityEffect(self)
        self._eff.setOpacity(1.0)
        self.setGraphicsEffect(self._eff)

        self._anim = QPropertyAnimation(self._eff, b'opacity', self)
        self._anim.setDuration(self.FADE)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self.hide)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._anim.start)

    # ------------------------------------------------------------ 对外
    @classmethod
    def show_for(cls, widget, text):
        """在 widget 所在窗口底部弹一条提示（同一窗口复用同一个 Toast）"""
        if widget is None or not text:
            return None
        win = widget.window()
        if win is None:
            return None
        t = getattr(win, '_wb_toast', None)
        if t is None:
            t = cls(win)
            win._wb_toast = t
        t.show_text(text)
        return t

    def show_text(self, text):
        self.setStyleSheet(_qss())          # 换主题后下一条提示立即跟上
        fm = QFontMetrics(self.font())
        max_w = max(120, self.parentWidget().width() - 48)
        # 左右内边距（QSS 的 14px）由 sizeHint 自己算进去了，所以宽度直接用
        # sizeHint；自己拿 fm.width + 常量去补，永远补不准，尾部会缺半个字
        label = fm.elidedText(text, Qt.ElideRight, max(80, max_w - 56))
        self.setText(label)
        self.resize(max(120, min(max_w, self.sizeHint().width())),
                    self.HEIGHT)
        self._place()
        self._eff.setOpacity(1.0)
        self.raise_()
        self.show()
        self._timer.start(self.LIFE)

    # ------------------------------------------------------------ 内部
    def _place(self):
        p = self.parentWidget()
        if p is None:
            return
        # 窗口底部有自己的工具条时，父窗口用 _toast_gap 告诉我们要抬多高，
        # 否则提示条会压在「导出当前会话」那排按钮上
        gap = getattr(p, '_toast_gap', self.BOTTOM_GAP)
        try:
            gap = int(gap)
        except Exception:
            gap = self.BOTTOM_GAP
        x = max(0, (p.width() - self.width()) // 2)
        y = max(0, p.height() - self.HEIGHT - gap)
        self.move(QPoint(x, y))
