# -*- coding: utf-8 -*-
"""全局交互细节：让所有按钮都有一致的手型光标

QSS 设不了 cursor（Qt 样式表没有这个属性），逐个窗口手写 setCursor 又会漏。
这里在 QApplication 上装一个事件过滤器：任何 QAbstractButton 在首次 polish
时自动得到手型光标；安装时再把已经创建好的按钮补刷一遍。

为什么要统一：主窗口/设置页/导出页/看图窗口的按钮以前有的有手型、有的
没有，鼠标移上去反馈不一致，看起来像"这个按钮不能点"。
"""
from PyQt5.QtCore import QEvent, QObject, Qt
from PyQt5.QtWidgets import QAbstractButton, QApplication

_FILTER = None


class _CursorFilter(QObject):
    def eventFilter(self, obj, e):
        if e.type() == QEvent.Polish and isinstance(obj, QAbstractButton):
            if obj.cursor().shape() != Qt.PointingHandCursor:
                obj.setCursor(Qt.PointingHandCursor)
        return False                       # 绝不吞掉事件


def install(app=None):
    """程序入口里调用一次：QApplication 建好之后、造任何窗口之前"""
    global _FILTER
    app = app or QApplication.instance()
    if app is None:
        return None
    if _FILTER is None:
        _FILTER = _CursorFilter(app)
        app.installEventFilter(_FILTER)
    refresh(app)
    return _FILTER


def refresh(app=None):
    """补刷已经存在的按钮（动态新建窗口 / 换主题后也可调用）"""
    app = app or QApplication.instance()
    if app is None:
        return
    for w in app.allWidgets():
        if (isinstance(w, QAbstractButton)
                and w.cursor().shape() != Qt.PointingHandCursor):
            w.setCursor(Qt.PointingHandCursor)
