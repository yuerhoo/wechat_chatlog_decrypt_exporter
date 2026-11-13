# -*- coding: utf-8 -*-
"""应用外壳：单实例 + 任务栏/系统托盘 + 登录页/主窗口生命周期

用户要求（2026-11-13）：
    · 一启动（包括登录页）就要在任务栏有按钮、右侧通知区有托盘图标；
    · 托盘右键菜单固定顺序：
          显示窗口 / 隐藏窗口 / ─── / 刷新所有数据 / ─── / 注销 / 退出
      （两条分割线位置不变，「注销」加在「退出」正上方）
    · 「退出」必须弹窗确认；「注销」不退出进程，停后台后回到登录页；
    · 右上角 X 只是关闭窗口、程序不退出（托盘常驻，可随时显示回来）；
    · 双击 exe 只能有一个实例，第二次双击把已有窗口叫出来，不再开第二个。

为什么要有这一层
--------------------------------------------------------------------
原来是 `LoginDialog.exec_() → MainWindow.show() → app.exec_()` 三段式，
登录框跑在**嵌套事件循环**里，没有应用级对象能同时管托盘、单实例和窗口切换。
现在改成「一个 QApplication + 一个事件循环」，登录框改非模态，
由 AppShell 统一持有当前窗口（登录页 → 主窗口）。

窗口关闭的三种来源（用 `_close_mode` 标记，见各窗口 closeEvent）：
    hide   用户点 X        → 只隐藏，后台任务照常跑，进程不退出
    logout 托盘「注销」    → 停后台、销毁主窗口、回登录页，进程不退出
    exit   托盘「退出」    → 停后台、销毁窗口、结束进程（先弹确认）

⚠️ 三个必须记住的坑：
    1. `setQuitOnLastWindowClosed(False)` —— 否则用户点 X / 「隐藏窗口」或关掉
       最后一个窗口，Qt 会直接结束进程，托盘瞬间消失。
    2. 备份/解密等 `--bootstrap-*` 子进程由 main.py 的 __main__ 提前分派，
       根本不会走到这里，所以单实例保护不会误伤它们。
    3. 真退出时先 `stop_background()`：QThread 还在跑会拖住进程退出。
"""
import os
import sys

from PyQt5.QtCore import QObject, Qt, QTimer
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (QApplication, QMenu, QMessageBox,
                             QSystemTrayIcon)

from app_config import APP_NAME, APP_TITLE, BUNDLE_DIR

# 同一 Windows 会话内唯一；第二个进程连上就说明已有实例
SERVER_NAME = 'wechat_chatlog_export_single_v1'
_ACTIVATE = b'activate'
_APP_ID = 'WeChatExport.Desktop.V1'


def set_app_user_model_id():
    """Windows 任务栏图标/分组的关键：显式设置 AppUserModelID

    不设的话 PyInstaller 打包的程序常被 Windows 归到「python」宿主组，
    表现为任务栏图标不显示、或和别的 Python 程序挤成一堆。
    必须在任何窗口创建之前调用（QApplication 之前也行）。
    """
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        fn = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        fn.argtypes = [ctypes.c_wchar_p]
        fn.restype = ctypes.c_long
        fn(_APP_ID)
    except Exception:
        pass


def app_icon():
    """托盘 / 窗口图标（打包后从 _MEIPASS/assets 取）"""
    p = os.path.join(BUNDLE_DIR, 'assets', 'app_icon.ico')
    return QIcon(p) if os.path.isfile(p) else QApplication.instance().windowIcon()


def confirm_exit(parent=None):
    """统一的退出确认（托盘「退出」用；登录框「取消」/ Esc 各有自己的）"""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Question)
    box.setWindowTitle('退出')
    box.setText('确定要退出「%s」吗？' % APP_NAME)
    btn_cancel = box.addButton('取消', QMessageBox.NoRole)
    btn_exit = box.addButton('退出', QMessageBox.YesRole)
    box.setDefaultButton(btn_cancel)
    box.exec_()
    return box.clickedButton() is btn_exit


class AppShell(QObject):
    """持有托盘、单实例 server、当前窗口；整个进程只有一份"""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.login = None
        self.main = None
        self.tray = None
        self._menu = None
        self.server = None
        self._quitting = False

    # ------------------------------------------------------------ 启动 / 单实例
    def start(self):
        """返回 False 表示已有实例在跑（已通知它显示窗口），本进程应直接结束"""
        sock = QLocalSocket()
        sock.connectToServer(SERVER_NAME)
        if sock.waitForConnected(300):
            sock.write(_ACTIVATE)
            sock.flush()
            sock.waitForBytesWritten(300)
            sock.disconnectFromServer()
            return False

        # 上次进程异常退出可能留下同名 server；先清掉再监听
        QLocalServer.removeServer(SERVER_NAME)
        self.server = QLocalServer(self)
        if self.server.listen(SERVER_NAME):
            self.server.newConnection.connect(self._on_new_instance)
        else:
            self.server = None            # 监听不了就退化为不保护，别挡住主流程

        # 托盘常驻：关掉最后一个窗口也不能让 Qt 自动结束进程
        self.app.setQuitOnLastWindowClosed(False)
        self.app.aboutToQuit.connect(self.cleanup)
        self._build_tray()
        self._show_login()
        return True

    def _on_new_instance(self):
        """第二次双击 exe：把已有窗口叫出来，而不是再开一个"""
        conn = self.server.nextPendingConnection() if self.server else None
        if conn is not None:
            try:
                if bytes(conn.readAll()) != _ACTIVATE:
                    pass                  # 不认识的消息也不影响，照样显示窗口
            except Exception:
                pass
            conn.disconnectFromServer()
            conn.deleteLater()
        self.show_window()
        self._notify('程序已经在运行，已为你显示窗口')

    def cleanup(self):
        if self.tray is not None:
            self.tray.hide()
        if self.server is not None:
            self.server.close()

    # ------------------------------------------------------------ 托盘
    def _build_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(APP_TITLE)
        menu = QMenu()
        # 顺序是用户定稿的：两条分割线的位置不能动，注销贴在退出上方
        menu.addAction('显示窗口', self.show_window)
        menu.addAction('隐藏窗口', self.hide_window)
        menu.addSeparator()
        menu.addAction('刷新所有数据', self.refresh_all)
        menu.addSeparator()
        menu.addAction('注销', self.request_logout)
        menu.addAction('退出', self.request_exit)
        self.tray.setContextMenu(menu)
        self._menu = menu                # Python 侧持有引用，别被 GC 掉
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()
        # 第一次启动时给一条气泡：Windows 常把新图标折叠到 ^ 里，
        # 弹气泡会把图标临时提到通知区，用户能立刻找到右键菜单在哪。
        QTimer.singleShot(1200, lambda: self._notify(
            '已启动 · 右键这里可显示/隐藏窗口、刷新数据或退出'))

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_window()

    def _notify(self, text):
        if self.tray is not None:
            self.tray.showMessage(APP_TITLE, text,
                                  QSystemTrayIcon.Information, 2600)

    # ------------------------------------------------------------ 窗口
    def _current_window(self):
        """登录成功后一律以主窗口为准（哪怕它当前是隐藏的）"""
        return self.main if self.main is not None else self.login

    def show_window(self):
        w = self._current_window()
        if w is None:
            return
        w.setWindowState(w.windowState() & ~Qt.WindowMinimized)
        w.show()
        w.raise_()
        w.activateWindow()

    def hide_window(self):
        w = self._current_window()
        if w is None:
            return
        if not w.isVisible():
            self._notify('窗口已经是隐藏状态')
            return
        w.hide()
        self._notify('窗口已隐藏，右键托盘图标可重新显示')

    def on_window_hidden(self, w):
        """右上角 X：告诉用户程序还在后台，别以为点了没反应或直接没了"""
        self._notify('窗口已关闭，程序仍在后台运行 · 右键托盘可显示窗口或退出')

    def refresh_all(self):
        """刷新所有数据 = 同步微信新消息 + 重读本地数据（MainWindow.refresh）"""
        if self.main is None:
            self.show_window()
            self._notify('请先登录后再刷新数据')
            return
        self.show_window()
        try:
            self.main.refresh()
        except Exception:
            self._notify('刷新失败，请到主窗口查看状态')
            return
        self._notify('正在刷新所有数据：同步新消息 + 重新读取')

    # ------------------------------------------------------------ 登录 / 主窗口
    def _show_login(self, reset=False):
        """显示登录页；注销后复用同一个对话框，避免重复构造和重复采集设备 ID"""
        if self.login is None:
            from .login_dialog import LoginDialog
            self.login = LoginDialog()
            self.login.accepted.connect(self._on_login_ok)
            self.login.rejected.connect(self._on_login_cancel)
        self.login._shell = self
        if reset:
            self.login.reset_for_relogin()
        self.login.setModal(False)       # 非模态：托盘菜单要能随时操作
        self.login.show()
        self.login.raise_()
        self.login.activateWindow()

    def _on_login_ok(self):
        info = self.login.info or {}
        self.login.hide()
        # 设备 ID 已改成纯注册表读取（<5ms），登录框里早就算好并缓存了，
        # 不需要再在这里预热；以前那次 wmic 预热正是首屏黑窗的来源之一。
        from .main_window import MainWindow
        self.main = MainWindow(login_user=info.get('username') or '')
        self.main._shell = self
        self.main.show()
        self.main.raise_()
        self.main.activateWindow()

    def _on_login_cancel(self):
        # LoginDialog.reject() 里已经弹过退出确认了，这里只负责收尾
        self._quitting = True
        self.app.quit()

    # ------------------------------------------------------------ 后台收尾
    def _stop_all_background(self):
        """注销 / 退出前：停掉设置页的解密线程 + 主窗口的同步与统计线程"""
        try:
            from .settings_dialog import SettingsDialog
            for w in QApplication.topLevelWidgets():
                if isinstance(w, SettingsDialog):
                    try:
                        w.force_stop()
                        w.close()
                    except Exception:
                        pass
        except Exception:
            pass
        if self.main is not None:
            try:
                self.main.stop_background()
            except Exception:
                pass

    # ------------------------------------------------------------ 注销
    def request_logout(self):
        """托盘「注销」：停后台解密/刷新 → 销毁主窗口 → 回到登录页（进程不退出）

        与「退出」的区别：不弹确认、不结束进程、不隐藏托盘；
        与右上角 X 的区别：X 只藏窗口，后台继续跑，注销会把后台全部停掉。
        """
        if self._quitting:
            return
        if self.main is None:
            self.show_window()
            self._notify('当前未登录，无需注销')
            return
        self._stop_all_background()
        main, self.main = self.main, None
        main._close_mode = 'logout'
        main._skip_exit_confirm = True
        try:
            main.close()
        except Exception:
            pass
        # 主窗口是聊天/看图窗口的 parent，删掉它才能把那些子窗口一起收掉
        try:
            main.deleteLater()
        except Exception:
            pass
        self._show_login(reset=True)
        self._notify('已注销：后台解密与刷新已停止，请重新登录')

    # ------------------------------------------------------------ 退出
    def request_exit(self):
        """托盘「退出」：弹一次确认 → 停后台 → 各窗口走清理 → 结束事件循环

        ⚠️ 给窗口打 `_close_mode='exit'` + `_skip_exit_confirm`，避免
        窗口自己的 closeEvent 再弹一次确认或误判成「只隐藏」。
        """
        if self._quitting:
            return
        w = self._current_window()
        if not QApplication.closingDown() and not confirm_exit(w):
            return
        self._quitting = True
        for x in (self.main, self.login):
            if x is not None:
                x._skip_exit_confirm = True
                x._close_mode = 'exit'
        self._stop_all_background()
        # 只关「当前」那个窗口：登录成功后登录框已经 accept 过了，
        # 再去 close 它会把 dialog result 改成 Rejected，还可能多弹一次信号。
        w = self._current_window()
        if w is not None:
            try:
                w.close()
            except Exception:
                pass
        self.app.quit()
