# -*- coding: utf-8 -*-
"""登录框：程序启动后的第一道门

在 MainWindow 之前弹出，校验通过才进主界面。

为什么把登录放在 main() 里、而不是 __main__ 最前面：
    --bootstrap-keys / --bootstrap-decrypt 是 UAC 提权后重启出来的子进程，
    无界面运行、根本没人能点按钮。登录判断一旦挡在它们前面，
    「提取密钥并解密」会永久卡死在这一屏。
    main.py 的 __main__ 里先分派 bootstrap、再进 main()，所以放在 main() 里天然安全。

交互约定：
    · 顶部一行只读「设备 ID」（硬件指纹，见 auth/device.py），右侧「复制」按钮；
      采集已改成纯注册表读取（毫秒级、零子进程），仍放到窗口显示之后再做；
    · 「记住密码」默认勾选：登录成功后把账号 + DPAPI 密文写进 QSettings，
      下次启动自动回填，用户只需点一下「登 录」；取消勾选则清空历史凭据；
    · 回车 = 登录，Esc = 退出；
    · 登录过程中锁住输入，避免连点重复提交；
    · 失败文案统一来自 auth/license_db.py，这里只负责显示。
"""
import os

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (QApplication, QCheckBox, QDialog, QGridLayout,
                             QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QPushButton, QVBoxLayout)

from app_config import APP_NAME, APP_TITLE, APP_VERSION, BUNDLE_DIR, Prefs
from auth import license_db, secret
from auth.device import device_id


class LoginDialog(QDialog):
    """设备 ID + 账号 + 密码登录框（非模态，由 AppShell 持有）

    用法：
        dlg = LoginDialog()
        dlg.accepted.connect(on_login_ok)     # dlg.info['username']
        dlg.rejected.connect(on_exit)         # 取消 / Esc / X 已弹过确认
        dlg.show()

    非模态是硬要求：托盘菜单（显示/隐藏/退出）在登录页阶段就要能用，
    exec_() 的嵌套循环会把整个应用卡在登录这一屏。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # 登录成功后的授权信息：username / version / expire_at / days_left
        self.info = {}
        # 防重复提交（连点按钮、回车与按钮同时触发）
        self._busy = False
        # 当前机器的设备 ID（32 位 hex），复制按钮和 tooltip 都用它
        self._device = ''

        self.setWindowTitle(APP_TITLE)
        self.setModal(False)
        # 顶层窗口 + 最小化按钮：Windows 任务栏才会给它一个按钮。
        # 打包后若丢了 AppUserModelID，任务栏图标也会异常（见 ui/app_shell.py）
        self.setWindowFlags(self.windowFlags() | Qt.Window
                            | Qt.WindowMinimizeButtonHint)
        self.setFixedSize(430, 380)
        self._build_ui()
        self._restore_credentials()
        # 窗口先画出来再填设备 ID（纯注册表读取，<5ms；不再有 wmic 黑窗）
        QTimer.singleShot(0, self._fill_device_id)

    # ------------------------------------------------------------------
    # 主题：内联写死的几处颜色跟着主题走
    # ------------------------------------------------------------------
    def _paint_titles(self):
        from . import theme
        p = theme.palette()
        self._lb_sub.setStyleSheet(
            'font-size:15px; font-weight:600; color:%s;' % p['text'])
        self.edit_device.setStyleSheet(
            'color:%s; background:%s; border:1px solid %s; '
            'border-radius:6px; padding:5px 8px;'
            % (p['sub'], p['card'], p['line']))

    def retheme(self):
        self._paint_titles()

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build_ui(self):
        """按 图标+标题 / 设备ID / 账号 / 密码 / 记住 / 提示 / 按钮 竖排"""
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 28, 30, 22)
        root.setSpacing(0)

        # 顶部：图标 + 程序名 + 版本
        head = QHBoxLayout()
        head.setSpacing(14)
        icon_path = os.path.join(BUNDLE_DIR, 'assets', 'app_icon.ico')
        icon_label = QLabel()
        icon_label.setFixedSize(44, 44)
        if os.path.exists(icon_path):
            icon = QIcon(icon_path)
            icon_label.setPixmap(icon.pixmap(44, 44))
            self.setWindowIcon(icon)
        head.addWidget(icon_label)

        titles = QVBoxLayout()
        titles.setSpacing(3)
        # 系统标题栏已显示软件名与版本，这里只留一句引导，不再重复
        self._lb_sub = QLabel('请使用已授权的账号登录')
        self._lb_sub.setObjectName('h2')
        titles.addWidget(self._lb_sub)
        head.addLayout(titles)
        head.addStretch(1)
        root.addLayout(head)
        root.addSpacing(20)

        # 表单：设备 ID 在账号上面，只读 + 复制按钮
        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(12)
        form.setColumnStretch(1, 1)

        self.edit_device = QLineEdit()
        self.edit_device.setObjectName('loginDevice')
        self.edit_device.setReadOnly(True)
        self.edit_device.setMinimumHeight(32)
        self.edit_device.setPlaceholderText('正在读取设备信息…')
        self.btn_copy = QPushButton('复制')
        self.btn_copy.setObjectName('quiet')
        self.btn_copy.setFixedWidth(58)
        self.btn_copy.setMinimumHeight(32)
        self.btn_copy.setCursor(Qt.PointingHandCursor)
        self.btn_copy.setToolTip('复制设备 ID 到剪贴板')
        self.btn_copy.clicked.connect(self._copy_device)

        self.edit_user = QLineEdit()
        self.edit_user.setPlaceholderText('请输入账号')
        self.edit_user.setMinimumHeight(32)

        self.edit_pwd = QLineEdit()
        self.edit_pwd.setPlaceholderText('请输入密码')
        self.edit_pwd.setEchoMode(QLineEdit.Password)
        self.edit_pwd.setMinimumHeight(32)

        form.addWidget(QLabel('设备 ID'), 0, 0)
        form.addWidget(self.edit_device, 0, 1)
        form.addWidget(self.btn_copy, 0, 2)
        form.addWidget(QLabel('账号'), 1, 0)
        form.addWidget(self.edit_user, 1, 1, 1, 2)
        form.addWidget(QLabel('密码'), 2, 0)
        form.addWidget(self.edit_pwd, 2, 1, 1, 2)
        root.addLayout(form)

        root.addSpacing(10)
        self.chk_remember = QCheckBox('记住密码（下次打开只需点击登录）')
        self.chk_remember.setChecked(True)
        root.addWidget(self.chk_remember)

        # 提示区：固定高度，出错时按钮不会上下跳
        self.lbl_msg = QLabel('')
        self.lbl_msg.setWordWrap(True)
        self.lbl_msg.setFixedHeight(38)
        self.lbl_msg.setStyleSheet('font-size:12px;')
        root.addSpacing(4)
        root.addWidget(self.lbl_msg)

        # 按钮：取消 / 登录
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_cancel = QPushButton('取消')
        self.btn_cancel.setObjectName('quiet')
        self.btn_cancel.setMinimumSize(84, 34)
        self.btn_login = QPushButton('登 录')
        self.btn_login.setObjectName('primary')
        self.btn_login.setMinimumSize(104, 34)
        btns.addWidget(self.btn_cancel)
        btns.addWidget(self.btn_login)
        root.addLayout(btns)

        self.btn_login.clicked.connect(self._do_login)
        self.btn_cancel.clicked.connect(self.reject)
        # 回车：账号框里只是把焦点挪到密码框，密码框里直接提交
        self.edit_user.returnPressed.connect(self._on_user_enter)
        self.edit_pwd.returnPressed.connect(self._do_login)

        self._paint_titles()

    # ------------------------------------------------------------------
    # 设备 ID / 记住密码
    # ------------------------------------------------------------------
    def _fill_device_id(self):
        try:
            self._device = device_id() or ''
        except Exception:
            self._device = ''
        if self._device:
            self.edit_device.setText(self._device)
            self.edit_device.setToolTip(self._device)
        else:
            self.edit_device.setText('未获取到（不影响登录）')
            self.edit_device.setToolTip('当前机器未能读取硬件指纹')
            self.btn_copy.setEnabled(False)

        # 账号密码都回填好了就把焦点给登录按钮，一键登录
        if self.edit_user.text() and self.edit_pwd.text():
            self.btn_login.setDefault(True)
            self.btn_login.setFocus()
        else:
            self.edit_user.setFocus()

    def _copy_device(self):
        if not self._device:
            return
        QApplication.clipboard().setText(self._device)
        self.btn_copy.setText('已复制')
        QTimer.singleShot(1400, lambda: self.btn_copy.setText('复制'))

    # 用新键：上一版为了「不记住」写过 auth/remember=False，换键避免继承那个状态
    _REMEMBER_KEY = 'auth/remember_pwd'

    def _restore_credentials(self):
        """启动时按上次的勾选状态回填账号 / 密码（密码是 DPAPI 密文）"""
        prefs = Prefs()
        remember = prefs.get(self._REMEMBER_KEY, True)
        if isinstance(remember, str):
            remember = remember.lower() in ('true', '1', 'yes')
        self.chk_remember.setChecked(bool(remember))
        if not remember:
            return
        self.edit_user.setText(prefs.get('auth/last_user', '') or '')
        self.edit_pwd.setText(secret.unprotect(prefs.get('auth/password', '') or ''))

    def _save_credentials(self, user, pwd):
        """登录成功后才写：勾了就存密文，没勾就把历史凭据抹干净"""
        prefs = Prefs()
        if self.chk_remember.isChecked():
            prefs.set(self._REMEMBER_KEY, True)
            prefs.set('auth/last_user', user)
            prefs.set('auth/password', secret.protect(pwd))
        else:
            prefs.set(self._REMEMBER_KEY, False)
            prefs.set('auth/last_user', '')
            prefs.set('auth/password', '')

    def _on_user_enter(self):
        """账号框回车：密码还空着就先跳到密码框，已填则直接登录"""
        if self.edit_pwd.text():
            self._do_login()
        else:
            self.edit_pwd.setFocus()

    # ------------------------------------------------------------------
    # 注销复用 / 右上角 X 的关闭语义
    # ------------------------------------------------------------------
    def reset_for_relogin(self):
        """托盘「注销」后回到本页：清掉上一次授权结果与提示，允许重新登录

        记住密码的账号/密码仍保留回填（每次登录都会重新连授权库校验），
        用户点一下「登 录」就能再次进入，不必重打一遍。
        """
        self.info = {}
        self._busy = False
        self._skip_exit_confirm = False
        self._close_mode = 'hide'
        self.lbl_msg.clear()
        self._set_busy(False)
        (self.edit_pwd if not self.edit_pwd.text() else self.btn_login).setFocus()

    def closeEvent(self, e):
        """右上角 X = 只关闭窗口、程序不退出（托盘常驻，可随时重新显示）

        「取消」按钮 / Esc 走 reject()，那才是带确认的退出入口。
        托盘「退出」与 Windows 关机由 AppShell 打 `_close_mode='exit'`。
        """
        mode = getattr(self, '_close_mode', 'hide')
        if mode == 'exit' or QApplication.closingDown():
            super().closeEvent(e)
            return
        e.ignore()
        self.hide()
        shell = getattr(self, '_shell', None)
        if shell is not None:
            shell.on_window_hidden(self)

    # ------------------------------------------------------------------
    # 退出确认（「取消」按钮 / Esc）
    # ------------------------------------------------------------------
    def _confirm_exit(self):
        """弹一次退出确认，返回 True 表示可以退出"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle('退出')
        box.setText('确定要退出「%s」吗？' % APP_NAME)
        btn_cancel = box.addButton('取消', QMessageBox.NoRole)
        btn_exit = box.addButton('退出', QMessageBox.YesRole)
        box.setDefaultButton(btn_cancel)
        box.exec_()
        return box.clickedButton() is btn_exit

    def reject(self):
        """「取消」/ Esc / 右上角 X 都汇到这里：真正退出前确认一次

        系统关机/注销时不能问：确认框会拖住 Windows 的关机流程，
        closingDown() 为真就放行。托盘「退出」已经确认过一次，会给窗口
        打 `_skip_exit_confirm` 标记，这里就不再重复弹窗。
        accept() 不经过这里，登录成功不会被挡住。
        """
        if (not QApplication.closingDown()
                and not getattr(self, '_skip_exit_confirm', False)
                and not self._confirm_exit()):
            return
        super().reject()

    # ------------------------------------------------------------------
    # 交互状态
    # ------------------------------------------------------------------
    def _show(self, text, color=None):
        """在提示区显示一行文字（红色=失败，灰色=进行中/说明）"""
        from . import theme
        color = color or theme.palette()['danger']
        self.lbl_msg.setStyleSheet('font-size:12px; color:%s;' % color)
        self.lbl_msg.setText(text)

    def _set_busy(self, busy, hint=''):
        """锁/解锁输入区，并把提示区切到指定文案"""
        self._busy = busy
        for w in (self.edit_user, self.edit_pwd, self.chk_remember,
                  self.btn_copy, self.btn_login, self.btn_cancel):
            w.setEnabled(not busy)
        # 设备 ID 由 _fill_device_id 单独管，busy 期间别把复制按钮又放开
        if not busy and not self._device:
            self.btn_copy.setEnabled(False)
        self.btn_login.setText('正在登录…' if busy else '登 录')
        if hint:
            from . import theme
            self._show(hint, theme.palette()['sub'])
        elif busy:
            self.lbl_msg.clear()

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------
    def _do_login(self):
        """取输入 → 问授权库 → 通过则若干提示后 accept()，失败留在本页"""
        if self._busy:
            return

        user = self.edit_user.text().strip()
        pwd = self.edit_pwd.text()
        if not user or not pwd:
            self._show('请输入账号和密码')
            (self.edit_pwd if user else self.edit_user).setFocus()
            return

        self._set_busy(True, '正在连接授权服务器…')
        # 连库最长会卡 5 秒（数据库没启动时），先把"正在连接"画出来再阻塞
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            ok, msg, info = license_db.login(user, pwd, APP_VERSION)
        except Exception as e:
            # login() 内部已把可预期的错误转成文案，这里只兜真正意外的异常
            ok, msg, info = False, '登录过程出错：%s' % (e,), {}
        finally:
            QApplication.restoreOverrideCursor()

        if not ok:
            self._set_busy(False)
            self._show(msg)
            self.edit_pwd.selectAll()
            self.edit_pwd.setFocus()
            return

        self._set_busy(False)
        self.lbl_msg.clear()
        # 登录日志（成功/失败都有）已由 license_db.login() 内部写好，这里不补写
        self._save_credentials(user, pwd)
        self.info = info

        # 快到期时提前提醒，免得某天突然进不去
        days = info.get('days_left')
        if days is not None and days <= 7:
            QMessageBox.information(
                self, '授权提醒',
                '授权将于 %s 到期（剩余 %d 天），请联系管理员续期。'
                % (info['expire_at'].strftime('%Y-%m-%d'), days))

        self.accept()
