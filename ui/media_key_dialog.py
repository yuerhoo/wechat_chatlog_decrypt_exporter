# -*- coding: utf-8 -*-
"""图片解密向导：从微信进程内存抓 V2 图片 AES key

为什么顺序和直觉相反
--------------------
微信 4.x 的图片 AES key 不落盘，只在**图片真正显示出来**的那几秒进内存，
图片窗口一关就卸载。所以「先去开图、再回来点扫描」经常扑空——人切窗口
+ 点按钮的工夫，key 已经没了。

正确顺序是**先开扫描、再去开图**：扫描线程在后台不停轮询内存（一轮约 8 秒），
用户在这 60 秒内随时把图打开，key 一进内存就被逮到。这也是社区工具
（find_image_key_monitor.py）推荐的"持续监控"用法。

界面仍然很轻：一段说明 + 一个按钮 + 一行实时状态。
"""
import os
import threading

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                            QVBoxLayout)

from core import media_key as MK

SCAN_SECONDS = 120.0     # 一轮全扫约 20 秒（微信 4.2 主进程 460MB），
                         # 120 秒能轮 5~6 次——用户开图后平均 10 秒内命中


class _Scanner(QThread):
    """后台扫描：挑样本（磁盘 IO）+ 内存轮询都在这里，主线程只更新文字"""

    progress = pyqtSignal(dict)
    done = pyqtSignal(object, dict)

    def __init__(self, media_root, seconds):
        super().__init__()
        self.media_root = media_root
        self.seconds = seconds
        self.stop = threading.Event()

    def run(self):
        try:
            samples = MK.pick_samples(self.media_root, 2)
            key, info = MK.find_key(samples, self.seconds,
                                    progress=self.progress.emit,
                                    stop=self.stop)
        except Exception as e:                       # 任何异常都要回到界面
            key, info = None, {'error': '%s: %s' % (type(e).__name__, e),
                               'regions': 0, 'tried': 0}
        self.done.emit(key, info)


class MediaKeyDialog(QDialog):
    """抓取 V2 图片密钥（modal 到设置窗口）"""

    def __init__(self, parent, acc_dir, media_root, on_saved=None):
        super().__init__(parent)
        self.acc_dir = acc_dir or ''
        self.media_root = media_root or ''
        self.on_saved = on_saved
        self._thr = None
        self.setWindowTitle('图片解密')
        self.setMinimumWidth(540)
        self._build()

    # ---- 界面 ----
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 20, 22, 18)
        v.setSpacing(12)

        lb_title = QLabel('抓取图片密钥')
        lb_title.setObjectName('h1')
        v.addWidget(lb_title)

        lb_desc = QLabel(
            '聊天里的图片是加密存储的，密钥只存在于微信进程内存中，'
            '而且仅在你**正在查看图片**时才有。\n\n'
            '所以顺序是先扫描、后开图：\n\n'
            '  1. 点下面的「开始扫描」，扫描会持续 %d 秒\n'
            '  2. 立刻切到微信，双击打开 1~2 张聊天图片，让图片保持显示\n'
            '  3. 抓到后自动保存，重新打开对话窗口就能看到图片\n\n'
            '提示：扫描期间保持图片开着即可，不用管这边。'
            % int(SCAN_SECONDS))
        lb_desc.setWordWrap(True)
        lb_desc.setObjectName('tip')
        v.addWidget(lb_desc)

        self.lb_state = QLabel('准备就绪')
        self.lb_state.setWordWrap(True)
        self.lb_state.setObjectName('tipWarn')
        v.addWidget(self.lb_state)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.btn_scan = QPushButton('开始扫描')
        self.btn_scan.setObjectName('primary')
        self.btn_scan.setCursor(Qt.PointingHandCursor)
        self.btn_scan.clicked.connect(self._start)
        row.addWidget(self.btn_scan)
        row.addStretch(1)
        self.btn_close = QPushButton('关闭')
        self.btn_close.setObjectName('quiet')
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.clicked.connect(self._on_close)
        row.addWidget(self.btn_close)
        v.addLayout(row)

    # ---- 扫描 ----
    def _start(self):
        if self._thr is not None and self._thr.isRunning():
            self._stop()
            return
        if not os.path.isdir(self.media_root):
            self.lb_state.setText('找不到微信媒体目录，无法取样本。请先在「数据路径」里选对账号。')
            return
        if not self.acc_dir:
            self.lb_state.setText('找不到账号目录，无法保存密钥。')
            return
        self.btn_scan.setText('停止')
        self.lb_state.setText(
            '扫描已开始 —— 现在切到微信，双击打开 1~2 张聊天图片并保持显示…')
        self._thr = _Scanner(self.media_root, SCAN_SECONDS)
        self._thr.progress.connect(self._on_progress)
        self._thr.done.connect(self._on_done)
        self._thr.start()

    def _stop(self):
        if self._thr is not None and self._thr.isRunning():
            self._thr.stop.set()
            self.lb_state.setText('正在停止…')

    def _on_progress(self, st):
        self.lb_state.setText(
            '扫描中… 第 %d 轮 · 进程 %d 个 · 已试 %d 个候选 · 剩余 %d 秒'
            % (st.get('rounds', 0), st.get('procs', 0),
               st.get('tried', 0), int(st.get('left', 0))))

    def _on_done(self, key, info):
        self.btn_scan.setText('再扫一次')
        if info.get('error') and not key:
            self.lb_state.setText('扫描失败：%s' % info['error'])
            return
        if not key:
            self.lb_state.setText(
                '没抓到密钥（跑了 %d 轮、试了 %d 个候选）。\n'
                '请再试一次，这次点完「开始扫描」立刻切到微信打开图片；'
                '如果还不行，用「以管理员身份运行」启动本程序后重试。'
                % (info.get('rounds', 0), info.get('tried', 0)))
            return
        try:
            path = MK.save_key(self.acc_dir, key)
        except OSError as e:
            self.lb_state.setText('抓到密钥但保存失败：%s' % e)
            return
        shown = key.decode('latin-1') if isinstance(key, (bytes, bytearray)) else str(key)
        self.lb_state.setText(
            '已抓到并保存：%s****（%s）\n重新打开任意对话窗口即可看到图片。'
            % (shown[:4], os.path.basename(path)))
        if callable(self.on_saved):
            try:
                self.on_saved()
            except Exception:
                pass

    # ---- 关闭 ----
    def _on_close(self):
        self._stop()
        if self._thr is not None and self._thr.isRunning():
            self._thr.wait(3000)     # 线程还在读内存，等它收尾再走
        self.reject()

    def closeEvent(self, e):
        self._stop()
        if self._thr is not None and self._thr.isRunning():
            self._thr.wait(3000)
        super().closeEvent(e)
