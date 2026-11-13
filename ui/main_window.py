# -*- coding: utf-8 -*-
"""主窗口

界面自上而下四层：
    菜单栏   设置 / 关于（左对齐的两个无边框按钮）
    操作行   搜索框 + 刷新 + 导出 Excel
    四个 Tab 联系人 / 群聊 / 公众号 / 系统，Tab 行右侧是"仅显示有聊天记录 / 已选 N / 清空选择"
    列表     每个 Tab 一个 QTableView，勾选后点「导出 Excel」
数据为空时整块换成引导页（去设置）。

两条后台链路：
    Loader        启动/刷新时加载联系人、群昵称、会话时间（避免界面卡住）
    CountsWorker  逐个会话统计消息数（几万个会话，只能后台慢慢补）
另外启动时会静默跑一次增量解密（_start_sync），把新消息同步进来。
"""
import json
import os
import re
import time

from PyQt5.QtCore import QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QGuiApplication, QKeySequence
from PyQt5.QtWidgets import (QAction, QApplication, QCheckBox, QFrame,
                             QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
                             QMessageBox, QProgressBar, QPushButton, QShortcut,
                             QSizePolicy, QStackedWidget, QTabWidget, QTableView,
                             QVBoxLayout, QWidget, QToolButton)

from core import accounts, contacts, messages, runner
from core.contacts import CAT_NAMES, CATEGORIES
from app_config import APP_NAME, APP_VERSION, APP_TITLE, Prefs
from auth import license_db
from . import icons, theme
from .toast import Toast
from .delegates import CheckDelegate
from .table_model import SessionTableModel
from .settings_dialog import SettingsDialog
from .export_dialog import ExportDialog
from .export_runner import ExportProgressDialog

# 主区域三个页面（QStackedWidget 的下标，别写魔术数字）
PAGE_LIST, PAGE_EMPTY, PAGE_LOADING = 0, 1, 2

# 状态栏右下角的常驻提示（加载中 / 空数据时会临时改掉，填充完再还原）
DEFAULT_HINT = '勾选会话后点「导出 Excel」；双击任意一行打开该会话的对话框（Ctrl+F 搜索）'

# ---- 增量同步的节奏 ----
SYNC_POLL_MS = 600          # 正常轮询间隔
SYNC_SLOW_POLL_MS = 3000    # 卡住之后放慢轮询（但不能停：结束时要有人收尾）
SYNC_TIMEOUT = 25.0         # 秒；到点先放开界面，同步继续在后台跑
NOTE_LIFE_MS = 6000         # 同步结果的停留时长

# 「没能同步」的几种原因 → (状态栏短标签, 弹一次 toast 的完整说明)
# 以前这些分支全是静默 return，用户只会觉得"我刷新了怎么没有新消息"
# 状态栏那栏空间有限（右边还有常驻提示），所以短标签控制在 6 字内，
# 完整原因和该去哪儿处理留给 toast。
_SYNC_BLOCKED = {
    'no_account': ('没有选中账号', '还没有选中账号，请到「设置」里选择'),
    'no_dir': ('未绑定数据目录', '账号还没绑定微信数据目录，请到「设置」里选择'),
    'dir_missing': ('数据目录不可用', '微信数据目录不存在或不可访问，请到「设置」里确认'),
    'no_key': ('还没提取密钥', '还没提取过密钥，要同步新消息请到「设置」里点「提取密钥并解密」'),
    'key_stale': ('密钥已失效', '密钥已失效（微信重建过数据库），请到「设置」里重新提取并解密'),
    'key_error': ('密钥缓存读取失败', '读取密钥缓存失败，可以到「设置」里重新提取一次密钥'),
    'spawn_failed': ('无法启动解密进程', '无法启动解密进程，可以到「设置」里点「提取密钥并解密」'),
}


def _msg_sig(dec_dir):
    """消息库签名：变化最晚的那个消息库的 (mtime, 文件名)

    用来给消息数缓存（counts.json）做过期判断：签名变了说明有新消息，
    缓存的条数不再可信，需要重新统计。
    """
    try:
        d = os.path.join(dec_dir, 'message')
        return max((os.path.getmtime(os.path.join(d, f)), f)
                   for f in os.listdir(d) if f.endswith('.db'))
    except Exception:
        return None


class Loader(QThread):
    """后台加载联系人数据（4 万多个联系人 + 群昵称解析，约 1 秒）"""

    done = pyqtSignal(object)      # 传 contacts.Data；失败时传 None

    def __init__(self, dec_dir, counts_path):
        """dec_dir 解密库根目录；counts_path 消息数缓存文件（可不存在）"""
        super().__init__()
        self.dec_dir = dec_dir
        self.counts_path = counts_path
        self.err = ''

    def run(self):
        """线程体：load_data 是纯 SQLite 读，不碰任何界面对象

        失败不抛异常，而是把错误文本放进 self.err 并 emit(None)，
        由主线程决定是弹窗还是显示空状态页。
        """
        try:
            data = contacts.load_data(self.dec_dir)
            self._restore_counts(data)
        except Exception as e:
            self.err = str(e)
            self.done.emit(None)
            return
        self.done.emit(data)

    def _restore_counts(self, data):
        """把上次缓存的"会话消息数"贴回去，省掉重新统计的时间"""
        try:
            with open(self.counts_path, encoding='utf-8') as f:
                cache = json.load(f)
        except Exception:
            return
        if cache.get('sig') != _msg_sig(self.dec_dir):
            return                     # 库变过，缓存作废
        for u, cnt in (cache.get('counts') or {}).items():
            s = data.by_user.get(u)
            if s is not None:
                s.count = cnt


class CountsWorker(QThread):
    """后台统计消息数：一个会话一个信号，统计完立即刷新那一行"""

    one = pyqtSignal(str, int)         # username, 条数
    finishedAll = pyqtSignal()

    def __init__(self, dec_dir, sessions):
        """sessions 传全量进来，这里自己筛出"还没统计过 + 有聊天记录"的"""
        super().__init__()
        self.dec_dir = dec_dir
        # 只统计"还没统计过且确实有聊天记录"的会话
        self.sessions = [s for s in sessions if s.count is None and s.last_ts]
        self._stop = False

    def stop(self):
        """请求停止：不强制 terminate（会泄漏 sqlite 句柄），等当前会话统计完自然退出"""
        self._stop = True

    def run(self):
        """线程体：逐会话 COUNT(*)，每算完一个就发一条信号让对应行立刻刷新"""
        for s in self.sessions:
            if self._stop:             # 切 Tab / 关窗时尽快退出
                break
            try:
                n = messages.count_msgs(self.dec_dir, s.username)
            except Exception:
                continue
            s.count = n
            self.one.emit(s.username, n)
        self.finishedAll.emit()


class ClickLabel(QLabel):
    """可点击的标签（"已选 N"，点一下弹窗看清单）"""

    clicked = pyqtSignal()

    def mouseReleaseEvent(self, e):
        """用 release 而不是 press：鼠标按下后拖走再松手不应该触发"""
        self.clicked.emit()
        super().mouseReleaseEvent(e)


class MainWindow(QMainWindow):
    """主窗口：四个 Tab 的会话列表 + 顶栏账号徽章 + 底部状态栏

    数据流：Loader/CountsWorker 在后台把数据准备好 → 信号回主线程 →
    填充 table_model → QTableView 显示。主线程永远不做耗时 IO。
    """

    def __init__(self, login_user=''):
        """界面全部代码构建（不用 .ui 文件），顺序：顶栏 → 操作行 → Tab 列表 → 状态栏

        login_user：本次登录的授权账号名（main() 从登录框传进来）。
        导出前的次数校验、导出后的日志落库都用它；空串时跳过（防御，正常登录必有）。

        构造函数里不做任何数据库读取，只决定"现在该显示哪一页"，
        真正的加载在 showEvent/refresh 里发起后台线程。
        """
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1080, 700)
        self.prefs = Prefs()
        self.login_user = (login_user or '').strip()   # 登录账号：次数校验/导出日志用
        self.data = None               # contacts.Data，加载完成前为 None
        self.models = {}               # category -> SessionTableModel
        self.views = {}                # category -> QTableView
        self.open_chats = {}           # username -> ChatWindow（已打开的对话框）
        self._loader = None
        self._counts = None
        self.acc = None                # 当前账号 dict
        self._restore_geom()

        # 启动/手动刷新的增量同步（只在新消息存在且密钥有效时才跑）
        self._sync_proc = None
        self._sync_pos = 0
        self._sync_t0 = 0.0            # 本轮同步起始时间（超时兜底用）
        self._sync_manual = False      # 本轮是否由用户点刷新触发（决定要不要报"没有新消息"）
        self._sync_slow = False        # 是否已提示过"较慢、已转后台"
        self._boot_synced = False      # 启动首次加载后只自动同步一次
        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(self._poll_sync)
        # 状态栏的同步状态单独占一栏，几秒后自动清空；不会被账号/计数信息覆盖
        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.timeout.connect(self._clear_note)
        # 加载请求合并：后台读库还没结束时又来了刷新，等它完成后再补一次
        self._reload_pending = False
        self._reload_silent = False
        self._refresh_pending = False   # 排队中的一次完整刷新（含同步）
        self._silent_reload = False
        self._chats_dirty = False      # 同步到新消息后要顺手刷新已打开的对话框

        central = QWidget()
        self.setCentralWidget(central)
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # ---------- 顶栏：只留两个动作按钮 ----------
        # 软件名 / 版本 / 当前账号统一由系统标题栏显示（_update_status 里拼），
        # 这里不再重复，顶栏才能压到 44px、右侧彻底留白。
        topbar = QWidget()
        topbar.setObjectName('topbar')
        th = QHBoxLayout(topbar)
        th.setContentsMargins(18, 6, 14, 6)
        th.setSpacing(8)

        self.b_settings = QToolButton()
        self.b_settings.setObjectName('mainMenu')
        self.b_settings.setText('设置')
        self.b_settings.setIcon(icons.icon('settings'))
        self.b_settings.setIconSize(QSize(16, 16))
        self.b_settings.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.b_settings.setPopupMode(QToolButton.InstantPopup)
        self.b_settings.setCursor(Qt.PointingHandCursor)
        self.b_settings.setToolTip('主题、账号、数据路径、密钥与解密（Ctrl+,）')
        menu = QMenu(self.b_settings)
        menu.addAction('主题', self._open_theme)
        menu.addAction('账号设置', self._open_settings)
        self.b_settings.setMenu(menu)
        th.addWidget(self.b_settings)

        self.b_about = QToolButton()
        self.b_about.setObjectName('mainBtn')
        self.b_about.setText('关于')
        self.b_about.setIcon(icons.icon('info'))
        self.b_about.setIconSize(QSize(16, 16))
        self.b_about.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.b_about.setCursor(Qt.PointingHandCursor)
        self.b_about.setToolTip('版本信息与数据安全说明')
        self.b_about.clicked.connect(self._show_about)
        th.addWidget(self.b_about)
        th.addStretch(1)
        v.addWidget(topbar)

        # ---------- 操作行：搜索 / 刷新 / 导出 ----------
        act = QWidget()
        act.setObjectName('toolbar')
        ah = QHBoxLayout(act)
        ah.setContentsMargins(18, 10, 18, 10)
        ah.setSpacing(10)
        self.ed_search = QLineEdit()
        self.ed_search.setObjectName('search')
        self.ed_search.setPlaceholderText('搜索群名、备注、微信号或拼音首字母…')
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.setMinimumHeight(34)
        # 放大镜放在输入框内部左侧（addAction 的图标位置由 Qt 决定，不需要额外布局）
        self.ed_search.addAction(icons.icon('search', theme.palette()['border2']),
                                 QLineEdit.LeadingPosition)
        self.ed_search.setToolTip('即时过滤当前分类；支持拼音首字母，例如输入 hh 可匹配「虎虎」\n'
                                  '快捷键：Ctrl+F')
        self.ed_search.textChanged.connect(self._on_search)
        ah.addWidget(self.ed_search, 1)
        self.b_refresh = QPushButton('刷新')
        self.b_refresh.setIcon(icons.icon('refresh'))
        self.b_refresh.setIconSize(QSize(16, 16))
        self.b_refresh.setMinimumHeight(34)
        # 固定最小宽度：同步时文字会变「同步中…」，宽度一抖整条工具栏跟着跳
        self.b_refresh.setMinimumWidth(94)
        self.b_refresh.setCursor(Qt.PointingHandCursor)
        self.b_refresh.setToolTip(
            '同步微信里的新消息，再重读本地已解密数据（快捷键 F5）\n'
            '密钥有效时会自动增量解密变动过的库，只解变化的那几个，通常几秒\n'
            '如果提示密钥失效，去「设置」里重新提取一次即可')
        self.b_refresh.clicked.connect(self.refresh)
        ah.addWidget(self.b_refresh)
        self.b_export = QPushButton('导出 Excel')
        self.b_export.setObjectName('primary')
        self.b_export.setIcon(icons.icon('export', theme.on_accent()))
        self.b_export.setIconSize(QSize(16, 16))
        self.b_export.setMinimumHeight(34)
        self.b_export.setCursor(Qt.PointingHandCursor)
        self.b_export.setToolTip('导出勾选的会话到「微信聊天记录导出目录」\n'
                                 '小技巧：双击任意一行可打开该会话的对话框')
        self.b_export.clicked.connect(self._open_export)
        ah.addWidget(self.b_export)
        v.addWidget(act)

        # ---------- 主区：有数据(四个 Tab) / 无数据(空状态页) ----------
        self.main_stack = QStackedWidget()

        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(lambda _i: self._refill())
        for cat in CATEGORIES:
            page = QWidget()
            pv = QVBoxLayout(page)
            pv.setContentsMargins(0, 0, 0, 0)
            view = QTableView()
            view.setSelectionBehavior(QTableView.SelectRows)      # 整行选中
            view.setSelectionMode(QTableView.ExtendedSelection)   # 支持多选
            view.setEditTriggers(QTableView.NoEditTriggers)       # 只读
            view.setShowGrid(False)
            view.setAlternatingRowColors(True)
            view.setMouseTracking(True)                           # 勾选框需要 hover 反馈
            view.setWordWrap(False)
            view.verticalHeader().setVisible(False)
            view.verticalHeader().setDefaultSectionSize(36)       # 行高（勾选框留出点击余量）
            model = SessionTableModel(self)
            model.checkedChanged.connect(self._sel_changed)
            # 顺序很重要：先 setModel 再配表头，否则表头没有 model 会崩
            view.setModel(model)
            # 第 0 列改用自绘勾选框（系统默认的太小、且 QSS 管不到）
            view.setItemDelegateForColumn(0, CheckDelegate(view))
            hh = view.horizontalHeader()
            hh.setHighlightSections(False)
            # 列宽策略（用户约定）：微信号/消息数/最近消息 固定，
            # 只有名称列随窗口伸缩 —— 拉伸/缩小窗口时只有名称列变化
            hh.setStretchLastSection(False)
            hh.setSectionResizeMode(1, hh.Stretch)                # 名称列 = 唯一的动态列
            hh.resizeSection(0, 40)                               # 勾选列
            hh.resizeSection(2, 200)                              # 微信号 / 群 ID（固定）
            hh.resizeSection(3, 80)                               # 消息数（固定）
            hh.resizeSection(4, 120)                              # 最近消息（固定）
            # 点勾选列表头 = 全选/取消全选当前列表（表头本身没有文字，靠 tooltip 说明）
            hh.sectionClicked.connect(
                lambda sec, m=model: m.toggle_all() if sec == 0 else None)
            view.setContextMenuPolicy(Qt.CustomContextMenu)
            view.customContextMenuRequested.connect(lambda pos, v=view: self._menu(v, pos))
            view.doubleClicked.connect(lambda idx, v=view: self._dbl(v, idx))
            self.models[cat] = model
            self.views[cat] = view
            pv.addWidget(view)
            self.tabs.addTab(page, CAT_NAMES[cat])
        self.main_stack.addWidget(self.tabs)

        # ---------- 空状态页：一张居中卡片 + 三步引导 ----------
        empty = QWidget()
        ev = QVBoxLayout(empty)
        ev.setContentsMargins(40, 28, 40, 36)
        ev.addStretch(1)
        card = QFrame()
        card.setObjectName('card')
        card.setMaximumWidth(560)
        cv = QVBoxLayout(card)
        cv.setContentsMargins(38, 32, 38, 30)
        cv.setSpacing(10)

        ic = QLabel()
        ic.setPixmap(icons.pixmap('chat', theme.palette()['border2'], 76))
        ic.setAlignment(Qt.AlignCenter)
        cv.addWidget(ic)

        lb1 = QLabel('还没有可用的微信数据')
        lb1.setObjectName('h1')
        lb1.setAlignment(Qt.AlignCenter)
        cv.addWidget(lb1)
        lb2 = QLabel('跟着三步走，就能把指定会话导出成 Excel')
        lb2.setObjectName('sub')
        lb2.setAlignment(Qt.AlignCenter)
        cv.addWidget(lb2)
        cv.addSpacing(12)

        # 三步说明：左边圆形序号，右边标题 + 一行解释
        steps = QVBoxLayout()
        steps.setSpacing(13)
        for no, title, desc in (
                ('1', '选择微信数据目录', '点「浏览…」选到 xwechat_files 这一级，程序会自动找出里面的微信号'),
                ('2', '挑一个微信号', '在「当前账号」下拉里选择要导出的号；每个号的数据各自独立存放'),
                ('3', '提取密钥并解密', '首次会弹一次管理员授权；之后有新消息只做增量同步，很快')):
            row = QHBoxLayout()
            row.setSpacing(11)
            n = QLabel(no)
            n.setObjectName('stepNo')
            n.setAlignment(Qt.AlignCenter)
            row.addWidget(n, 0, Qt.AlignTop)
            col = QVBoxLayout()
            col.setSpacing(2)
            t = QLabel(title)
            t.setObjectName('stepTitle')
            d = QLabel(desc)
            d.setObjectName('stepDesc')
            d.setWordWrap(True)
            col.addWidget(t)
            col.addWidget(d)
            row.addLayout(col, 1)
            steps.addLayout(row)
        cv.addLayout(steps)

        cv.addSpacing(8)
        row = QHBoxLayout()
        row.addStretch(1)
        bgo = QPushButton('去设置')
        bgo.setObjectName('primary')
        bgo.setIcon(icons.icon('settings', theme.on_accent()))
        bgo.setIconSize(QSize(16, 16))
        self._bgo = bgo
        bgo.setCursor(Qt.PointingHandCursor)
        bgo.setToolTip('打开设置：选择微信数据目录 → 选账号 → 提取密钥并解密')
        bgo.clicked.connect(self._open_settings)
        row.addWidget(bgo)
        row.addStretch(1)
        cv.addLayout(row)

        ev.addWidget(card, 0, Qt.AlignHCenter)
        ev.addStretch(1)
        self.main_stack.addWidget(empty)

        # ---------- 加载页：读解密库时给个明确的"正在干活"反馈 ----------
        loading = QWidget()
        lv = QVBoxLayout(loading)
        lv.setContentsMargins(40, 40, 40, 40)
        lv.addStretch(1)
        lt = QLabel('正在读取解密数据…')
        lt.setObjectName('h2')
        lt.setAlignment(Qt.AlignCenter)
        lv.addWidget(lt)
        ls = QLabel('会话多的时候需要几秒，请稍候')
        ls.setObjectName('sub')
        ls.setAlignment(Qt.AlignCenter)
        lv.addWidget(ls)
        lv.addSpacing(10)
        brow = QHBoxLayout()
        brow.addStretch(1)
        self.bar_load = QProgressBar()
        self.bar_load.setRange(0, 0)          # 不确定进度：滚动条动画
        self.bar_load.setFixedWidth(240)
        self.bar_load.setTextVisible(False)
        brow.addWidget(self.bar_load)
        brow.addStretch(1)
        lv.addLayout(brow)
        lv.addStretch(1)
        self.main_stack.addWidget(loading)

        v.addWidget(self.main_stack, 1)

        # Tab 行右侧的控件（仅显示有聊天记录 / 已选 N / 全选 / 清空选择）
        # 注意：默认 QWidget 的尺寸策略会横向扩张、把 QTabBar 挤窄，
        #       导致 Tab 上的数字被截掉 —— 必须限制为 Maximum（最多占 sizeHint 宽）
        corner = QWidget()
        corner.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        ch = QHBoxLayout(corner)
        ch.setContentsMargins(8, 0, 12, 0)
        ch.setSpacing(6)
        self.chk_chat = QCheckBox('仅显示有聊天记录')
        self.chk_chat.setToolTip('取消勾选可看到全部联系人与群，包括从未聊过的')
        self.chk_chat.setChecked(self.prefs.get('only_with_chat', True))
        self.chk_chat.stateChanged.connect(lambda _s: self._refill())
        ch.addWidget(self.chk_chat)
        ch.addSpacing(4)
        self.lb_sel = ClickLabel('已选 0')
        self.lb_sel.setObjectName('badge')
        self.lb_sel.setCursor(Qt.PointingHandCursor)
        self.lb_sel.setToolTip('点击查看已勾选的会话清单')
        self.lb_sel.clicked.connect(self._show_selected)
        ch.addWidget(self.lb_sel)
        self.b_check_all = QPushButton('全选')
        self.b_check_all.setObjectName('quiet')
        self.b_check_all.setCursor(Qt.PointingHandCursor)
        self.b_check_all.setToolTip('勾选当前列表里所有会话（只影响搜索过滤后看到的行）\n'
                                    '也可以直接点列表最左侧的表头')
        self.b_check_all.clicked.connect(self._check_all)
        ch.addWidget(self.b_check_all)
        self.b_clear = QPushButton('清空选择')
        self.b_clear.setObjectName('quiet')
        self.b_clear.setCursor(Qt.PointingHandCursor)
        self.b_clear.setToolTip('取消勾选所有会话（不影响已经导出的文件）')
        self.b_clear.clicked.connect(self._clear_sel)
        ch.addWidget(self.b_clear)
        self.tabs.setCornerWidget(corner, Qt.TopRightCorner)

        # ---------- 快捷键：Ctrl+F 定位搜索、F5 刷新、Ctrl+, 设置 ----------
        for keys, slot in ((QKeySequence('Ctrl+F'), self._focus_search),
                           (QKeySequence('F5'), self.refresh),
                           (QKeySequence('Ctrl+,'), self._open_settings)):
            sc = QShortcut(keys, self)
            sc.activated.connect(slot)

        # ---------- 底部状态栏：常驻信息 + 操作提示 ----------
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        self.lb_status = QLabel('')
        self.lb_status.setObjectName('tip')
        sb.addWidget(self.lb_status, 1)
        # 同步状态单独一栏：_update_status() 会反复重写 lb_status，
        # 混在一起的话"正在同步…"下一秒就被计数信息盖掉了
        self.lb_sync = QLabel('')
        self.lb_sync.setObjectName('tip')
        self.lb_sync.setStyleSheet('color:%s; padding:0 12px;'
                                   % theme.palette()['sub'])
        sb.addWidget(self.lb_sync, 0)
        self.lb_hint = QLabel(DEFAULT_HINT)
        self.lb_hint.setObjectName('tip')
        sb.addPermanentWidget(self.lb_hint)

        self.reload()
        self._sel_changed(0)

    def retheme(self):
        """主题切换：重设运行时生成、QSS 改不到的图标颜色"""
        try:
            self.b_export.setIcon(icons.icon('export', theme.on_accent()))
            self.b_settings.setIcon(icons.icon('settings'))
            self.b_about.setIcon(icons.icon('info'))
            if getattr(self, '_bgo', None) is not None:
                self._bgo.setIcon(icons.icon('settings', theme.on_accent()))
        except Exception:
            pass

    # ---------- 启动/刷新 ----------
    def _dec_dir(self):
        """当前账号的解密库目录（没账号时返回空串）"""
        return (self.acc or {}).get('dec_dir') or ''

    def reload(self, silent=False):
        """重新解析账号并异步加载数据（启动、设置改动、同步完成后都会调用）

        silent=True：**原地换数据**——不切「正在加载」页、不禁用导出按钮、
        不改状态栏和提示。同步到新消息后的二次刷新走这条，列表的内容与计数
        原地更新，界面不会闪一下白屏。
        """
        self.acc = accounts.get_current()
        if self.acc and self.acc.get('name'):
            self.setWindowTitle('%s · %s' % (APP_TITLE, self.acc['name']))
        elif self.acc:
            self.setWindowTitle(APP_TITLE)
        else:
            self.setWindowTitle(APP_TITLE)
        self.counts_path = accounts.counts_file(self.acc) if self.acc else ''
        dec = self._dec_dir()
        # 用 contact.db 是否存在判断"这个账号是否已经解密过"
        ok = bool(dec) and os.path.exists(os.path.join(dec, 'contact', 'contact.db'))
        if not ok:
            if silent:                       # 后台静默刷新：别去改用户正在看的提示
                return
            self.data = None
            self.main_stack.setCurrentIndex(PAGE_EMPTY)
            self.lb_status.setText('账号：%s　·　还没有解密数据'
                                   % ((self.acc or {}).get('name') or '未选择'))
            self.lb_hint.setText('到「设置」里点「提取密钥并解密」，列表就会出现在这里')
            return
        if self._loader is not None and self._loader.isRunning():
            # 上一轮还没跑完：记下来，等它结束后补一次，别把这次刷新直接丢了
            self._reload_pending = True
            self._reload_silent = silent
            return
        self._silent_reload = silent
        if not silent:
            self.b_export.setEnabled(False)
            self.main_stack.setCurrentIndex(PAGE_LOADING)
            self.lb_status.setText('正在加载数据…')
            self.lb_hint.setText('首次读取几万个会话需要几秒，之后会走缓存')
        self._loader = Loader(dec, self.counts_path)
        self._loader.done.connect(self._loaded)
        self._loader.start()

    def _loaded(self, data):
        """Loader 回调（主线程）：data 为 None 表示加载失败"""
        silent = self._silent_reload
        self._silent_reload = False
        if not silent:
            self.b_export.setEnabled(True)
        if data is None:
            err = (self._loader.err if self._loader else '') or '未知错误'
            # 失败就别再排下一次了：立刻重试只会连着失败，还会让按钮像卡住
            self._refresh_pending = False
            self._reload_pending = False
            if silent:
                # 静默刷新失败不弹窗打断：同步刚写完库文件时最容易撞上这种临时失败
                self._note('刷新失败：%s' % err.splitlines()[0][:40], 'warn')
            else:
                self.lb_status.setText('数据加载失败')
                self.main_stack.setCurrentIndex(PAGE_EMPTY)
                QMessageBox.warning(
                    self, '加载失败',
                    '读取解密数据时出错：\n%s\n\n'
                    '常见原因：数据库还在解密中、文件被占用，或该账号的数据已损坏。\n'
                    '可以到「设置」里点「提取密钥并解密」再试一次。' % err)
            return
        self.data = data
        self._refill()
        # 同步到了新消息：已经打开的对话框也换一份内容，否则里面还是旧的
        if self._chats_dirty:
            self._chats_dirty = False
            self._reload_open_chats()
        # 只有启动后的第一次加载才自动同步；手动刷新自己会先起同步，别跑两遍
        if not self._boot_synced:
            self._boot_synced = True
            started, reason = self._start_sync(manual=False)
            if not started and reason != 'no_account':
                # 启动时同步不了，就把原因摆在状态栏（常驻）：
                # 否则密钥已失效的用户会一直以为"列表就是最新的"
                tip = _SYNC_BLOCKED.get(reason, ('暂时无法同步', ''))[0]
                self._note('未同步：%s，详见「设置」' % tip, 'warn', life=0)
        # 加载期间点过刷新的：这次点击要补做一遍完整刷新（不然等于白点）
        if self._refresh_pending:
            self._refresh_pending = False
            QTimer.singleShot(0, self.refresh)   # 回到事件循环再触发，避免回调里层层套
        elif self._reload_pending:
            self._reload_pending = False
            again = self._reload_silent
            self._reload_silent = False
            self.reload(silent=again)

    def _cat(self):
        """当前 Tab 对应的分类 key"""
        return CATEGORIES[self.tabs.currentIndex()]

    def _refill(self):
        """按当前 Tab + 搜索词 + "仅显示有聊天记录" 重新灌数据到表格"""
        if self.data is None:
            self.main_stack.setCurrentIndex(PAGE_EMPTY)
            return
        self.main_stack.setCurrentIndex(PAGE_LIST)
        # Tab 标题带上数量。
        # 文本末尾故意多留一个空格：Qt 计算 Tab 宽度 = 文本度量宽 + 内边距，分毫不差；
        # 高分屏缩放（125%/150%）下字形实际渲染会比度量宽出 1~2 设备像素，
        # 没有这个空格缓冲，最后一位数字会被切掉半个（真机 125% 缩放实测出现过）
        for i, c in enumerate(CATEGORIES):
            sess = self.data.of(c)
            if self.chk_chat.isChecked():
                sess = [s for s in sess if s.last_ts]
            self.tabs.setTabText(i, '%s %s ' % (CAT_NAMES[c], format(len(sess), ',')))
        cat = self._cat()
        sess = self.data.of(cat)
        if self.chk_chat.isChecked():
            sess = [s for s in sess if s.last_ts]
        sess.sort(key=lambda s: s.last_ts or 0, reverse=True)   # 最近聊的排前面
        self.models[cat].set_sessions(sess)
        self.models[cat].set_filter(self.ed_search.text())
        self.b_check_all.setEnabled(bool(sess))
        self.lb_hint.setText(DEFAULT_HINT)
        self._update_status()
        self._start_counts()

    def _update_status(self):
        """刷新底部状态栏：账号 · 显示数/总数 · 已选数（搜索过滤后能一眼看出差别）"""
        if self.data is None:
            return
        cat = self._cat()
        m = self.models.get(cat)
        total = len(m._all) if m is not None else 0
        shown = m.rowCount() if m is not None else 0
        who = (self.acc or {}).get('name') or (self.acc or {}).get('id') or '未选择账号'
        if shown == 0 and self.ed_search.text().strip():
            # 搜不到东西时，说清楚是"没匹配"而不是"没有数据"
            self.lb_status.setText(
                '%s　·　%s 没有匹配「%s」的会话（共 %s 个）'
                % (who, CAT_NAMES.get(cat, cat), self.ed_search.text().strip(),
                   format(total, ',')))
            return
        self.lb_status.setText(
            '%s　·　%s 显示 %s / 共 %s　·　已选 %d 个'
            % (who, CAT_NAMES.get(cat, cat), format(shown, ','),
               format(total, ','), len(self._checked())))

    def _start_counts(self):
        """启动（或重启）消息数统计线程"""
        if self._counts is not None:
            self._counts.stop()
            self._counts.wait(2000)     # 等上一个线程收尾，避免两个线程写同一批对象
        model = self.models[self._cat()]
        self._counts = CountsWorker(self._dec_dir(), model._all)
        self._counts.one.connect(model.update_count)
        self._counts.finishedAll.connect(self._save_counts)
        self._counts.start()

    def _save_counts(self):
        """统计结束后把消息数写进缓存（带消息库签名，库变了缓存自动作废）"""
        if self.data is None or not self.counts_path:
            return
        try:
            os.makedirs(os.path.dirname(self.counts_path), exist_ok=True)
            with open(self.counts_path, 'w', encoding='utf-8') as f:
                json.dump({'sig': _msg_sig(self._dec_dir()),
                           'counts': {u: s.count for u, s in self.data.by_user.items()
                                      if s.count is not None}}, f, ensure_ascii=False)
        except Exception:
            pass

    # ---------- 刷新 / 增量同步 ----------
    def refresh(self):
        """刷新按钮与 F5 的唯一入口：同步微信新消息 + 重读本地数据

        顺序是「先重读（秒级，马上看到当前数据），同时后台增量解密」，
        解出新库后再静默换一次数据——全程不切「正在加载」页，列表不闪。
        """
        # 先取最新账号：_start_sync() 用的是 self.acc，而它可能还是上一次加载时的快照。
        # 在「设置」里刚换过账号/目录时，不刷新就会拿旧账号去解密。
        self.acc = accounts.get_current()
        if self._loader is not None and self._loader.isRunning():
            # 正在读库：把这次点击排到读完之后，走一遍**完整**刷新（含同步）。
            # 只记一个"重读"是不够的——用户点的是刷新，同步也得补上。
            self._refresh_pending = True
            self._note('正在加载数据，完成后会立即刷新', 'busy', life=0)
            return
        self._refresh_pending = False
        started, reason = self._start_sync(manual=True)
        if not started:
            # 说清楚为什么没同步，否则用户只会觉得"我刷新了但没有新消息"
            self._sync_blocked(reason)
        self.reload(silent=True)

    def _start_sync(self, manual=False):
        """启动一次增量解密，返回 (是否已启动, 没启动的原因)

        原因只用于给用户提示：以前这些分支全是静默 return —— 密钥失效时
        点刷新永远同步不到新消息，界面却一切正常。
        """
        if self._sync_proc is not None and self._sync_proc.poll() is None:
            # 已经在跑：借这次点击要一份结果（结束后要提示"已是最新"）
            self._sync_manual = self._sync_manual or manual
            if manual:
                # 点击总要有回应，否则用户分不清"在同步"和"按钮坏了"
                self._note('同步仍在进行中，完成后会自动刷新列表', 'busy', life=0)
            return True, ''
        acc = self.acc
        if not acc or not acc.get('id'):
            return False, 'no_account'
        db, dec = acc.get('db_dir', ''), self._dec_dir()
        if not db or not dec:
            return False, 'no_dir'
        if not os.path.isdir(db):
            return False, 'dir_missing'
        keys = accounts.keys_file(acc['id'])
        try:
            valid, stale, _total = contacts.key_status(db, keys)
        except Exception:
            return False, 'key_error'
        if stale > 0:                   # 微信重建过库，salt 变了，旧密钥解不开
            return False, 'key_stale'
        if valid == 0:                  # 从来没提取过密钥
            return False, 'no_key'
        try:
            self._sync_proc, _f = runner.run_plain(
                runner.BOOT_DECRYPT,
                [db, dec, keys, accounts.manifest_file(acc['id'])],
                accounts.sync_log(acc['id']))
        except Exception:
            return False, 'spawn_failed'
        self._sync_pos = 0
        self._sync_t0 = time.time()
        self._sync_slow = False
        self._sync_manual = manual
        self._set_refresh_busy(True)
        self._note('正在同步微信新消息…', 'busy', life=0)
        self._sync_timer.start(SYNC_POLL_MS)
        return True, ''

    def _poll_sync(self):
        """轮询同步日志：真的解出了新库才刷新列表，没有就别白刷一遍"""
        if not self.acc:
            self._sync_timer.stop()
            self._set_refresh_busy(False)
            return
        log = accounts.sync_log(self.acc['id'])
        lines, self._sync_pos, rc = runner.read_new_lines(log, self._sync_pos)
        if rc is None:
            # 还在跑。微信占着库时子进程会卡住：到点先把界面放开，但**不能停轮询**，
            # 否则它跑完之后没人收尾，新消息永远不会出现
            if not self._sync_slow and time.time() - self._sync_t0 > SYNC_TIMEOUT:
                self._sync_slow = True
                self._set_refresh_busy(False)
                self._note('同步还在后台进行，可先继续使用', 'warn', life=0)
                self._sync_timer.setInterval(SYNC_SLOW_POLL_MS)
            return
        self._sync_timer.stop()
        self._sync_timer.setInterval(SYNC_POLL_MS)
        self._set_refresh_busy(False)
        manual = self._sync_manual
        self._sync_manual = False
        self._sync_slow = False
        text = '\n'.join(lines)
        m = re.search(r'完成: (\d+) 解密', text)
        n = int(m.group(1)) if m else 0
        if rc != 0:
            self._note('同步失败：微信可能正在写数据库，稍后再试一次', 'warn')
            if manual:
                self.reload(silent=True)
            return
        if n > 0:
            self._note('已同步 %d 个库的新消息' % n, 'ok')
            self._chats_dirty = True         # 已经打开着的对话框也跟着换内容
            self.reload(silent=True)
            return
        # 没有新库：手动刷新也要重读一遍，否则"点了刷新什么都没发生"
        if manual:
            self._note('已是最新，没有新消息', 'ok')
            self.reload(silent=True)

    def _reload_open_chats(self):
        """已打开的对话框换一份内容（同时换成这次新加载的 data 对象）"""
        for win in list(self.open_chats.values()):
            try:
                win.data = self.data
                win.reload()
            except Exception:
                pass

    # ---------- 状态提示 ----------
    def _note(self, text, level='busy', life=NOTE_LIFE_MS):
        """状态栏里的同步状态：独立一栏，不会被账号/计数信息覆盖

        life=0 表示常驻，等下一次 _note 来替换。
        """
        label = getattr(self, 'lb_sync', None)
        if label is None:
            return
        p = theme.palette()
        color = {'busy': p['sub'], 'ok': p['ok'],
                 'warn': p['warn']}.get(level, p['sub'])
        # 左右各留 12px：不然会和左边的账号信息、右边的常驻提示贴成一团
        label.setStyleSheet('color:%s; padding:0 12px;' % color)
        label.setText(text)
        if life > 0:
            self._note_timer.start(life)
        else:
            self._note_timer.stop()

    def _clear_note(self):
        label = getattr(self, 'lb_sync', None)
        if label is not None:
            label.setText('')

    def _set_refresh_busy(self, on):
        """同步中：按钮禁用 + 文字变化（宽度固定过，不会把工具栏顶得乱跳）"""
        b = getattr(self, 'b_refresh', None)
        if b is None:
            return
        b.setEnabled(not on)
        b.setText('同步中…' if on else '刷新')

    def _sync_blocked(self, reason):
        """没能同步：状态栏说一句 + toast 说清怎么办（用户刚点了按钮，必须给回应）"""
        text, tip = _SYNC_BLOCKED.get(
            reason, ('暂时无法同步', '暂时无法同步新消息，可以到「设置」里检查账号与密钥'))
        self._note('未同步：%s' % text, 'warn')
        Toast.show_for(self, tip)

    # ---------- 搜索/选择 ----------
    def _on_search(self, text):
        """搜索框实时过滤当前 Tab（只过滤显示，不改勾选状态）"""
        self.models[self._cat()].set_filter(text)

    def _focus_search(self):
        """Ctrl+F：把光标送进搜索框并选中已有内容（直接输入即可覆盖）"""
        self.ed_search.setFocus()
        self.ed_search.selectAll()

    def _check_all(self):
        """「全选」按钮：勾上当前列表里所有会话"""
        self.models[self._cat()].toggle_all(True)

    def _sel_changed(self, n=0):
        """勾选数量变化 → 更新"已选 N"徽章、清空按钮与底部状态栏

        这里统计的是四个 Tab 的合计：跨分类勾选时，用户期望的是"一共选了多少"，
        而不是"当前 Tab 选了多少"。
        """
        try:
            total = len(self._checked())
        except Exception:
            total = n
        self.lb_sel.setText('已选 %d' % total)
        # 有勾选时徽章换成主色，一眼能看出"当前有选择"
        self.lb_sel.setObjectName('badgeActive' if total else 'badge')
        self.lb_sel.style().unpolish(self.lb_sel)
        self.lb_sel.style().polish(self.lb_sel)
        self.b_clear.setEnabled(total > 0)
        if self.data is not None:
            self._update_status()

    def _checked(self):
        """四个 Tab 里所有已勾选的会话（跨 Tab 导出）"""
        return [s for m in self.models.values() for s in m.checked_sessions()]

    def _clear_sel(self):
        """清空四个 Tab 的全部勾选（不只是当前 Tab），并刷新"已选 N"徽章"""
        for m in self.models.values():
            m.clear_checked()

    def _show_selected(self):
        """点"已选 N"：弹窗列出已勾选的会话（最多显示 30 个）"""
        sel = self._checked()
        if not sel:
            QMessageBox.information(
                self, '已选会话',
                '当前没有勾选任何会话。\n\n'
                '在列表最左侧勾选就能加入导出清单；勾选数量会显示在标签页右上角。')
            return
        lines = ['%d. %s  （%s）' % (i, s.display, CAT_NAMES.get(s.category, s.category))
                 for i, s in enumerate(sel[:30], 1)]
        more = '\n… 还有 %d 个未列出' % (len(sel) - 30) if len(sel) > 30 else ''
        QMessageBox.information(
            self, '已选 %d 个会话' % len(sel),
            '\n'.join(lines) + more + '\n\n如需调整，直接回列表取消/重新勾选即可。')

    # ---------- 右键/双击 ----------
    def _menu(self, view, pos):
        """右键菜单：只导出此会话 / 复制会话 ID"""
        idx = view.indexAt(pos)
        if not idx.isValid():
            return
        view.selectRow(idx.row())
        s = self.models[self._cat()].session_at(idx.row())
        m = QMenu(self)
        a1 = QAction('只导出此会话', self)
        a1.triggered.connect(lambda _c, ss=s: self._do_export([ss]))
        a2 = QAction('复制会话 ID', self)
        a2.triggered.connect(lambda _c, ss=s: QGuiApplication.clipboard().setText(ss.username))
        m.addAction(a1)
        m.addAction(a2)
        m.exec_(view.viewport().mapToGlobal(pos))

    def open_chat(self, sess):
        """双击会话列表行 → 打开（或聚焦）对应的微信对话框

        同一会话已打开则聚焦已有窗口；否则新建一个独立顶层 ChatWindow 并登记，
        窗口销毁时自动从登记表移除（destroyed 信号）。

        ⚠️ 关闭对话框走的是 QWidget.close() → 只是 hide，窗口并没销毁，
        open_chats 里还留着引用。以前只调 raise_()，对隐藏窗口完全无效，
        于是"关掉再双击就打不开了"。这里必须先 show() 再 raise_()；
        同时用 sip 判断 C++ 对象是否已被销毁，避免对野指针调用直接崩溃。
        """
        win = self.open_chats.get(sess.username)
        if win is not None:
            dead = False
            try:
                try:
                    from PyQt5 import sip as _sip
                except ImportError:
                    import sip as _sip
                dead = _sip.isdeleted(win)
            except Exception:
                pass
            if dead:
                self.open_chats.pop(sess.username, None)
            else:
                if win.isMinimized():
                    win.showNormal()
                win.show()               # 关键：close() 之后窗口是隐藏的
                win.raise_()
                win.activateWindow()
                # 上次没读到消息（空结果 / 加载失败）就重试一次：
                # 否则用户关掉再双击，看到的还是那片空白。
                if getattr(win, 'needs_reload', lambda: False)():
                    win.reload()
                return
        from .chat_window import ChatWindow
        from core import media_store
        acc = self.acc or {}
        db_dir = acc.get('db_dir', '')
        # 微信原始账号目录（db_dir 的上一级）里有 msg/attach、msg/video、msg/file，
        # 解密目录里只有数据库，媒体必须从这里读（**只读**）
        media_root = os.path.dirname(db_dir) if db_dir else ''
        # 已解密媒体库：图片/视频/收发文件归档到程序自己的目录，
        # 对话框里的打开、右键定位、复制路径一律指向这里。
        store = media_store.get_store(
            accounts.media_dir_of(acc), source_root=media_root,
            key_file=accounts.media_key_file(acc))
        win = ChatWindow(sess, self._dec_dir(), self.data, self,
                         media_root=media_root,
                         self_wxid=acc.get('wxid') or acc.get('id', ''),
                         media_store=store)
        win.destroyed.connect(
            lambda *_a, u=sess.username: self.open_chats.pop(u, None))
        self.open_chats[sess.username] = win
        win.show()

    def _dbl(self, view, idx):
        """双击非勾选列 = 打开这个会话的对话框（导出仍走右键菜单 / 顶部按钮）"""
        if idx.column() != 0:
            s = self.models[self._cat()].session_at(idx.row())
            self.open_chat(s)

    # ---------- 弹窗 ----------
    def _show_about(self):
        """关于：版本、能力边界与数据安全说明"""
        from PyQt5.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setWindowTitle('关于')
        box.setText('%s\n版本 %s' % (APP_NAME, APP_VERSION))
        box.setInformativeText(
            '从本机微信客户端解密聊天数据库，按会话与时间范围导出为 Excel。\n\n'
            '· 支持多个微信号：每个账号的密钥与解密数据各自独立\n'
            '· 支持微信同时登录多个号：自动定位到当前账号所在的微信进程\n'
            '· 增量同步：只解密有变化的库，日常几十秒完成\n'
            '· 导出集中保存到「微信聊天记录导出目录」，同名文件自动覆盖\n\n'
            '所有数据只在本机处理，程序不联网、不上传。\n\n'
            'Python 3.12 · PyQt5 5.15 · openpyxl\n'
            '解密引擎：SQLCipher v3 / v4')
        box.setIcon(QMessageBox.Information)
        box.addButton('关闭', QMessageBox.YesRole)
        box.exec_()

    def _open_theme(self):
        """设置 ▾ → 主题：打开独立主题设置页"""
        from .theme_dialog import ThemeDialog
        ThemeDialog(self).exec_()

    def _open_settings(self):
        """打开设置窗（模态）。里面的保存/重新检查/解密都会触发 dataChanged → reload"""
        dlg = SettingsDialog(self.prefs, self)
        # 账号/目录/密钥变化 → 走 refresh（先同步新消息再重读，语义与刷新按钮一致）
        dlg.dataChanged.connect(self.refresh)
        dlg.exec_()

    def _open_export(self):
        """顶部「导出 Excel」：必须有勾选"""
        sel = self._checked()
        if not sel:
            QMessageBox.information(
                self, '还没有选择会话',
                '请先在列表里勾选要导出的会话，再点「导出 Excel」。\n\n'
                '小技巧：双击任意一行可打开该会话的对话框；'
                '右键某一行也能「只导出此会话」。')
            return
        self._do_export(sel)

    def _do_export(self, sessions):
        """先弹选项窗，确认后再开进度窗（导出目录固定，无需再选路径）"""
        if self.data is None:
            return
        # 导出次数校验：没通过连选项窗都不弹，别让用户填完选项才被拒
        if self.login_user:
            # 查库最长可能卡几秒（连不上时），先把界面刷新出来，学登录框的做法
            QApplication.setOverrideCursor(Qt.WaitCursor)
            QApplication.processEvents()
            try:
                ok, msg, _left = license_db.check_export_limit(self.login_user)
            except Exception as e:
                ok, msg, _left = False, '校验导出次数时出错：%s' % (e,), 0
            finally:
                QApplication.restoreOverrideCursor()
            if not ok:
                QMessageBox.warning(self, '无法导出', msg)
                return
        dlg = ExportDialog(sessions, self.data, self._dec_dir(), self.prefs, self)
        self._pending = sessions
        dlg.startExport.connect(self._run_export)
        dlg.exec_()

    def _run_export(self, opts):
        """选项确认后的实际执行：进度窗内部起线程跑导出

        进度窗是模态的且关闭前必等导出线程收尾，所以 exec_() 返回时
        worker.result 一定已就绪——在这里记导出日志（成功/失败/取消都记）。
        """
        dlg = ExportProgressDialog(self._pending, opts, self.data,
                                   self._dec_dir(), self)
        dlg.exec_()
        if not self.login_user:
            return
        r = (getattr(dlg, 'worker', None) and dlg.worker.result) or {}
        ok = bool(r.get('ok', 0)) and not r.get('failed') and not r.get('cancelled')
        if r.get('cancelled'):
            summary = '用户取消：成功 %d · 跳过 %d · 失败 %d' % (
                r.get('ok', 0), r.get('skipped', 0), r.get('failed', 0))
        else:
            summary = '成功 %d · 跳过 %d · 失败 %d · 共 %s 条 · %.1f 秒' % (
                r.get('ok', 0), r.get('skipped', 0), r.get('failed', 0),
                format(r.get('total', 0), ','), r.get('elapsed', 0))
        # success 只认「产出文件且无失败」：全跳过（时间范围内没消息）没产出文件，
        # 不该扣次数；log_export 写失败只吞掉，绝不影响导出结果本身
        license_db.log_export(self.login_user, ok, summary, APP_VERSION)

    # ---------- 窗口状态记忆 ----------
    def _restore_geom(self):
        """恢复上次的窗口位置大小；没有记录就居中偏左上、占屏幕 3/4"""
        g = self.prefs.get('win/geometry', None)
        if g:
            try:
                self.restoreGeometry(bytes.fromhex(str(g)))
                return
            except Exception:
                pass
        qr = QGuiApplication.primaryScreen().availableGeometry()
        self.setGeometry(qr.width() // 8, qr.height() // 8,
                         qr.width() * 3 // 4, qr.height() * 3 // 4)

    # ------------------------------------------------------------------
    # 关窗 / 后台任务收尾
    # ------------------------------------------------------------------
    def stop_background(self):
        """停掉主窗口全部后台：增量解密子进程、同步轮询、统计与加载线程

        只有托盘「注销」和「退出」会调它。点右上角 X 只是把窗口藏起来，
        后台同步照常进行，托盘还能随时把窗口叫回来。
        """
        self._sync_timer.stop()
        self._sync_timer.setInterval(SYNC_POLL_MS)
        self._note_timer.stop()
        self._refresh_pending = False
        self._reload_pending = False
        self._set_refresh_busy(False)

        proc, self._sync_proc = self._sync_proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self._counts is not None:
            try:
                self._counts.stop()
                self._counts.wait(1500)
            except Exception:
                pass
            self._counts = None
        if self._loader is not None and self._loader.isRunning():
            # Loader 正在同步读 sqlite，别 terminate（会泄漏句柄），等它自己收尾
            try:
                self._loader.wait(1500)
            except Exception:
                pass

    def _cleanup_before_close(self):
        """真正销毁窗口前：停后台 + 存缓存 + 记住窗口状态与勾选项"""
        self.stop_background()
        self._save_counts()
        self.prefs.set('win/geometry', bytes(self.saveGeometry()).hex())
        self.prefs.set('only_with_chat', self.chk_chat.isChecked())

    def closeEvent(self, e):
        """右上角 X = 只关闭窗口、程序不退出（托盘常驻，后台继续）

        AppShell 用 `_close_mode` 区分三种来源：
            'hide'（默认，用户点 X）→ 隐藏窗口，什么都不停，也不弹确认；
            'logout'（托盘注销）     → 停后台、销毁窗口、回登录页，进程还在；
            'exit'（托盘退出）       → 停后台、销毁窗口、结束进程。
        Windows 关机/注销时 closingDown() 为真，按 'exit' 处理，绝不弹确认
        （确认框会拖住系统关机流程）。
        """
        mode = getattr(self, '_close_mode', 'hide')
        if mode == 'hide' and not QApplication.closingDown():
            e.ignore()
            self.hide()
            shell = getattr(self, '_shell', None)
            if shell is not None:
                shell.on_window_hidden(self)
            return
        self._cleanup_before_close()
        super().closeEvent(e)
        if mode == 'exit' or QApplication.closingDown():
            QApplication.instance().quit()
