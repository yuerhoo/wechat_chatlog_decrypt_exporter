# -*- coding: utf-8 -*-
"""主题设置页：从主窗口「设置 ▾ → 主题」打开

9 套皮肤共用一份 QSS 模板，点色块即调 theme.apply() 全局换肤；
本页自己也监听 BUS.changed，所以从别处换主题时选中态会同步更新。

从原来的 SettingsDialog 里独立出来，是为了让账号设置窗专注在
数据/账号/密钥那一长页上，不再把主题色块挤在最顶上。
"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout,
                             QLabel, QPushButton, QVBoxLayout)

from . import theme


class ThemeDialog(QDialog):
    """420×300 的轻量主题页（模态）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('主题设置')
        self.setModal(True)
        self.setFixedSize(420, 300)
        self._btns = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(12)

        lb_title = QLabel('界面主题')
        lb_title.setObjectName('h1')
        root.addWidget(lb_title)
        lb_sub = QLabel('切换后立即应用到所有窗口，无需重启')
        lb_sub.setObjectName('sub')
        root.addWidget(lb_sub)

        row = QHBoxLayout()
        row.setSpacing(7)
        for name in theme.theme_names():
            b = QPushButton()
            b.setObjectName('themeSwatch')
            b.setFixedSize(34, 28)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(name)
            b.clicked.connect(lambda _c=False, n=name: self._pick(n))
            row.addWidget(b)
            self._btns[name] = b
        row.addStretch(1)
        root.addLayout(row)

        self.lb_cur = QLabel('')
        self.lb_cur.setObjectName('tipGood')
        root.addWidget(self.lb_cur)

        # 预览：主色按钮 + 选中态胶囊，换肤时能直观看到配色变化
        card = QFrame()
        card.setObjectName('card')
        cv = QVBoxLayout(card)
        cv.setContentsMargins(14, 12, 14, 12)
        cv.setSpacing(8)
        lb_prev = QLabel('预览：主操作按钮 / 选中态')
        lb_prev.setObjectName('tip')
        cv.addWidget(lb_prev)
        prow = QHBoxLayout()
        prow.setSpacing(8)
        self.b_primary = QPushButton('主操作')
        self.b_primary.setObjectName('primary')
        self.b_primary.setCursor(Qt.PointingHandCursor)
        prow.addWidget(self.b_primary)
        self.b_chip = QPushButton('选中态')
        self.b_chip.setObjectName('chip')
        self.b_chip.setCheckable(True)
        self.b_chip.setChecked(True)
        self.b_chip.setCursor(Qt.PointingHandCursor)
        prow.addWidget(self.b_chip)
        prow.addStretch(1)
        cv.addLayout(prow)
        root.addWidget(card)

        root.addStretch(1)
        brow = QHBoxLayout()
        brow.addStretch(1)
        b_close = QPushButton('关闭')
        b_close.setCursor(Qt.PointingHandCursor)
        b_close.clicked.connect(self.accept)
        brow.addWidget(b_close)
        root.addLayout(brow)

        theme.BUS.changed.connect(self._paint)
        self._paint()

    def _pick(self, name):
        theme.apply(QApplication.instance(), name)

    def _paint(self, *_args):
        """重绘色块选中态（BUS 广播或本页点选后都会走到这里）"""
        p = theme.palette()
        cur = theme.current()
        for name, b in self._btns.items():
            color = theme.THEMES[name]
            sel = (name == cur)
            # 白色块要描边，否则在白卡片上看不见
            edge = '#9AA4B2' if name == '白' else (
                '#FFFFFF' if sel else color)
            width = 3 if sel else 1
            b.setStyleSheet(
                'QPushButton#themeSwatch { background: %s; border: %dpx solid %s;'
                ' border-radius: 6px; }'
                'QPushButton#themeSwatch:hover { border: 2px solid %s; }'
                % (color, width, edge, theme.palette(name)['hover']))
        self.lb_cur.setText('✓ 当前：%s · 共 %d 套' % (cur, len(self._btns)))
        # 预览按钮用当前主色，QSS 覆盖不到运行时取值时这里兜一层
        self.b_primary.setStyleSheet(
            'background:%s; color:%s; border:1px solid %s;'
            ' border-radius:8px; padding:6px 14px; font-weight:600;'
            % (p['primary'], p['on_primary'], p['primary']))
