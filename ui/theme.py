# -*- coding: utf-8 -*-
"""全局样式表（整个程序的视觉基调）

单独成模块的原因：
    1) 勾选框的「勾」必须用运行时生成的 png（QSS 画不出对勾），
       路径依赖用户机器的缓存目录，只能等 QApplication 建好后再拼；
    2) 样式集中在文件里，各窗口只管 setObjectName，避免同一控件在多处写内联样式。

设计约定（改样式前先看这里，避免各处风格打架）：
    · 底色 #F2F5F9 浅灰蓝，卡片纯白 + 1px #E6EAF2 细边 + 圆角 12px
      —— 用「卡片分层」代替老式灰色 GroupBox 描边，界面更透气
    · 主色 #185FA5（蓝），只用在「主操作按钮 / 选中态 / 进度条 / 关键数字」
    · 语义色：成功 #17805B、警示 #B26A00、危险 #C0392B
    · 控件对象名约定（各窗口里通过 setObjectName 引用）：
        primary     主操作按钮（蓝底白字）
        quiet       无边框文字按钮（次要操作）
        danger      危险操作（删除类，红字浅边）
        chip        可选中的胶囊按钮（时间快捷块）
        menubtn     顶栏文字按钮（设置 / 关于）
        card        白色圆角卡片容器
        stepNo      空状态里的步骤序号圆点
        h1 / sub / tip / tipGood / tipWarn / badge / badgeActive / badgeGood / badgeBad
    · 输入框统一：白底、#D5DBE5 边框、圆角 8px、聚焦变主色
"""
from . import icons

# ===========================================================================
# 主题系统
# ---------------------------------------------------------------------------
# 9 套主题（红黄蓝绿橙紫青白黑），共用一份 QSS_TEMPLATE，靠"蓝色系字面量 →
# 主题派生色"的整体替换实现全局换色；黑色主题额外追加一层深色覆盖样式。
# 切换时 apply() 会：换图标主色 → 重建 app 级样式表 → 广播 BUS.changed，
# 让自定义样式表的窗口（对话框）同步刷新。所有窗口都吃 app 级样式表，
# 因此换色是全局即时生效的，不需要逐窗口 setStyleSheet。
# ===========================================================================
import re as _re
from collections import OrderedDict
from PyQt5.QtCore import QObject, pyqtSignal


class _ThemeBus(QObject):
    """主题切换广播：自定义样式表的窗口连它来同步刷新"""
    changed = pyqtSignal(str)


BUS = _ThemeBus()

# 有序字典保证设置界面里色块顺序稳定
THEMES = OrderedDict([
    ('蓝', '#185FA5'), ('红', '#C0392B'), ('橙', '#E07A2F'), ('黄', '#D4A017'),
    ('绿', '#1E9E62'), ('青', '#159AA8'), ('紫', '#7C5CC4'),
    ('白', '#7A8494'), ('黑', '#3B424E'),
])
THEME_KEY = 'ui/theme'
DEFAULT_THEME = '蓝'

# 自己气泡：默认蓝主题保持微信原生绿，其余主题给一套协调的浅色
_OWN_BUBBLE = {
    '蓝': '#95EC69', '红': '#F8B9B2', '橙': '#F9CE9E', '黄': '#F7E39A',
    '绿': '#95EC69', '青': '#A9E7E6', '紫': '#DBC4F5',
    '白': '#E7EAF0', '黑': '#3E6349',
}


def _rgb(h):
    h = (h or '#000000').lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hex(rgb):
    return '#%02X%02X%02X' % tuple(max(0, min(255, int(v))) for v in rgb)


def _mix(a, b, t):
    """a→b 线性混合：t=0 取 a，t=1 取 b"""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(ra[i] + (rb[i] - ra[i]) * t for i in range(3)))


def theme_names():
    return list(THEMES.keys())


def current():
    """当前主题名（读 QSettings，任何异常都退回默认蓝）"""
    try:
        from app_config import Prefs
        n = Prefs().get(THEME_KEY, DEFAULT_THEME)
        return n if n in THEMES else DEFAULT_THEME
    except Exception:
        return DEFAULT_THEME


def set_current(name):
    if name not in THEMES:
        return
    try:
        from app_config import Prefs
        Prefs().set(THEME_KEY, name)
    except Exception:
        pass


def palette(name=None):
    """返回某主题的全部配色令牌（颜色都由主色派生，避免手写 9 套色值）"""
    n = name if name in THEMES else current()
    pri = THEMES[n]
    dark = (n == '黑')
    white = (n == '白')
    on = '#3A2E00' if n == '黄' else '#FFFFFF'
    soft = _mix(pri, '#FFFFFF', 0.88) if not dark else _mix(pri, '#181B21', 0.80)
    return {
        'name': n, 'dark': dark,
        'primary': pri,
        'hover': _mix(pri, '#FFFFFF', 0.12),
        'pressed': _mix(pri, '#000000', 0.20),
        'disabled': _mix(pri, '#FFFFFF', 0.55),
        'soft': soft,
        'border': _mix(pri, '#FFFFFF', 0.60),
        'border2': _mix(pri, '#FFFFFF', 0.72),
        'on_primary': on,
        'primary_text': _mix(pri, '#000000', 0.35),
        # 明暗基调
        'bg': '#181B21' if dark else '#F2F5F9',
        'card': '#22262E' if dark else '#FFFFFF',
        'text': '#DDE3EC' if dark else '#1F2430',
        'sub': '#98A2B0' if dark else '#6B7280',
        'line': '#333A45' if dark else '#E6EAF2',
        'hover_bg': '#2F3742' if dark else _mix(pri, '#FFFFFF', 0.94),
        # 对话框
        'chat_bg': '#1B1F26' if dark else '#F5F5F5',
        'chat_title': '#22262E' if dark else ('#FFFFFF' if white else '#F2F5F9'),
        'peer_bubble': '#2B3039' if dark else '#FFFFFF',
        'own_bubble': _OWN_BUBBLE.get(n, '#95EC69'),
        'chat_text': '#E6EAF2' if dark else '#1B2230',
        'nick': '#98A2B0' if dark else '#8A8A8A',
        'time_pill': '#3A424E' if dark else '#D6D6D6',
        'time_text': '#E6EAF2' if dark else '#FFFFFF',
        # 语义色：状态栏 / 提示 / 错误文案统一从这里取，深色下自动提亮
        'ok': '#4FD1A1' if dark else '#17805B',
        'warn': '#E8B45B' if dark else '#B26A00',
        'danger': '#F28074' if dark else '#C0392B',
        # 中性图标色：按钮上的齿轮/放大镜/刷新等，深色底上要有足够对比
        'neutral': '#A8B2C0' if dark else '#4B5563',
    }


def accent():
    return palette()['primary']


def on_accent():
    return palette()['on_primary']


# 模板里所有"蓝色系"字面量 → 主题派生色（语义色 成功绿/危险红 保持不动）
_COLOR_MAP = {
    '#185FA5': 'primary', '#1B70C2': 'hover', '#12497F': 'pressed',
    '#B7CCE2': 'disabled', '#E8F1FB': 'soft', '#AFCCEA': 'border',
    '#C6D9EE': 'border2', '#EAF1F9': 'soft', '#F1F5FA': 'hover_bg',
    '#F4F7FB': 'hover_bg', '#E9EFF7': 'soft', '#F7FBFF': 'hover_bg',
    '#FBFCFE': 'card', '#EEF2F8': 'hover_bg', '#E7EEF7': 'soft',
    '#123A5E': 'primary_text', '#12324F': 'primary_text',
}


def _recolor(qss, p):
    def sub(m):
        return p[_COLOR_MAP[m.group(0).upper()]]
    return _re.sub('|'.join(_COLOR_MAP.keys()), sub, qss, flags=_re.I)


# 深色主题：追加一层覆盖，把底色/文字/输入/表格整体反转（QSS 后定义优先）
_DARK_OVERRIDE = """
QMainWindow, QDialog, QStackedWidget, QWidget#page { background: %(bg)s; }
QWidget { color: %(text)s; }
QGroupBox, QFrame#card, QWidget#card {
    background: %(card)s; border-color: %(line)s;
}
QGroupBox::title, QLabel#h1, QLabel#h2, QLabel#stepTitle { color: %(text)s; }
QLabel#sub, QLabel#tip, QLabel#stepDesc { color: %(sub)s; }
QPushButton { background: #2A3039; color: %(text)s; border-color: #3A424F; }
QPushButton:hover { background: #323A45; border-color: #46505E; }
QPushButton:pressed { background: #242B34; }
QPushButton#primary {
    background: %(primary)s; color: %(on_primary)s; border-color: %(primary)s;
}
QPushButton#quiet { color: %(primary)s; background: transparent; }
QPushButton#menubtn { color: %(text)s; background: transparent; }
QPushButton#chip { background: #292F38; color: %(sub)s; border-color: #3A424F; }
QLineEdit, QComboBox, QDateTimeEdit, QSpinBox, QPlainTextEdit {
    background: #22262E; color: %(text)s; border-color: #3A424F;
}
QLineEdit[readOnly="true"] { background: #262B33; color: %(sub)s; }
QComboBox QAbstractItemView { background: #22262E; border-color: #3A424F; color: %(text)s; }
QTableView {
    background: #1E2229; alternate-background-color: #20252C;
    selection-background-color: %(soft)s; color: %(text)s;
}
QHeaderView::section { background: #22262E; color: %(sub)s; border-color: %(line)s; }
QTableCornerButton::section { background: #22262E; }
QListWidget { background: #1E2229; border-color: #3A424F; }
QListWidget::item { color: %(text)s; }
QTabWidget::pane { background: #1E2229; }
QProgressBar { background: #2A3039; color: %(text)s; }
QWidget#topbar, QWidget#toolbar, QStatusBar {
    background: #22262E; border-color: %(line)s; color: %(sub)s;
}
QMenu { background: #22262E; border-color: #3A424F; }
QMenu::item:selected { background: #2F3742; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal { background: #454E5C; }
QLabel#badge, QLabel#badgeGray { background: #2A3039; color: %(sub)s; }
QMessageBox { background: #22262E; }
QMessageBox QLabel { color: %(text)s; }
QToolTip { background: #2A3039; color: %(text)s; border: 1px solid #3A424F; }
"""

# 追加在最后的一层「动态主题」：这些控件的颜色必须直接取当前 palette 令牌，
# 不能靠 QSS_TEMPLATE 里的蓝色系字面量替换（例如日志框要完全跟随卡片主题）。
# 放在 _DARK_OVERRIDE 之后，深浅两套主题都由同一份公式算出，优先级也最高。
_DYNAMIC_QSS = """
QToolButton {
    background: transparent; border: 1px solid transparent;
    border-radius: 8px; padding: 6px 12px; color: %(text)s;
}
QToolButton:hover { background: %(hover_bg)s; border-color: %(border)s; color: %(primary)s; }
QToolButton:pressed { background: %(soft)s; }
QToolButton:focus { border-color: %(primary)s; outline: none; }
QToolButton:disabled { color: %(sub)s; background: transparent; }

QListWidget#settingsNav {
    background: %(bg)s; border: none; border-right: 1px solid %(line)s;
    border-radius: 0; padding: 12px 8px; font-size: 13px;
}
QListWidget#settingsNav::item {
    padding: 10px 14px; border-radius: 8px; color: %(sub)s; margin: 1px 4px;
}
QListWidget#settingsNav::item:hover { background: %(hover_bg)s; color: %(text)s; }
QListWidget#settingsNav::item:selected {
    background: %(soft)s; color: %(primary)s; font-weight: 600;
    border-left: 3px solid %(primary)s;
}

QScrollArea#settingsScroll { background: transparent; border: none; }
QWidget#settingsPage { background: transparent; }

QGroupBox { background: %(card)s; border-color: %(line)s; }
QGroupBox::title { color: %(text)s; }
QPlainTextEdit {
    background: %(card)s; color: %(text)s;
    border: 1px solid %(line)s; border-radius: 9px;
    selection-background-color: %(soft)s; selection-color: %(text)s;
}
"""


def apply(app, name=None):
    """应用主题到整个程序：换图标主色 + 重建全局样式表 + 广播给所有窗口"""
    if name:
        set_current(name)
    n = current()
    p = palette(n)
    icons.set_accent(p['primary'])
    icons.set_neutral(p['neutral'])
    icons.invalidate_qss_assets()
    qss = _recolor(stylesheet(n), p)
    if p['dark']:
        qss += _DARK_OVERRIDE % p
    qss += _DYNAMIC_QSS % p          # 最后一层：全部由当前主题令牌算出的控件色
    app.setStyleSheet(qss)
    # 全局样式表已经覆盖所有窗口的控件外观；这里再给各窗口一次机会刷新
    # 「运行时生成、写在代码里」的图标/富文本颜色（QSS 改不到它们）
    for w in app.topLevelWidgets():
        fn = getattr(w, 'retheme', None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass
    BUS.changed.emit(n)
    return n


# ---------------------------------------------------------------------------
# 对话框专用样式（ChatWindow 有自己的无边框窗口，需要单独一套 QSS）
# ---------------------------------------------------------------------------
_CHAT_QSS = """
QWidget#chatRoot { background: %(chat_bg)s; border: 1px solid %(line)s; }
QWidget#chatContent { background: %(chat_bg)s; }
QWidget#chatTitle { background: %(chat_title)s; border-bottom: 1px solid %(line)s; }
QLabel#chatTitleName { font-size: 15px; font-weight: 600; color: %(chat_text)s; }
QLabel#chatTitleSub { font-size: 11px; color: %(sub)s; }
QPushButton#chatTopBtn, QPushButton#chatTopClose {
    background: transparent; border: none; border-radius: 6px;
}
QPushButton#chatTopBtn:hover { background: %(hover_bg)s; }
QPushButton#chatTopClose:hover { background: #E84C3D; }
QScrollArea#chatScroll { background: %(chat_bg)s; border: none; }
QLabel#chatNick { font-size: 12px; color: %(nick)s; padding-left: 2px; }
QLabel#chatTime {
    font-size: 11px; color: %(time_text)s; background: %(time_pill)s;
    border-radius: 4px; padding: 2px 8px; margin: 6px 0; max-width: 160px;
}
QLabel#chatSys { font-size: 12px; color: %(sub)s; }
QPushButton#chatMore {
    font-size: 12px; color: %(primary)s; padding: 8px 0;
    background: transparent; border: none;
}
QPushButton#chatMore:hover { color: %(hover)s; }
QLabel#chatOwn {
    background: %(own_bubble)s; color: %(chat_text)s; border-radius: 8px;
    padding: 8px 11px; font-size: 14px; border: 1px solid %(border)s;
}
QLabel#chatPeer {
    background: %(peer_bubble)s; color: %(chat_text)s; border-radius: 8px;
    padding: 8px 11px; font-size: 14px; border: 1px solid %(line)s;
}
QLabel#chatImg { border-radius: 6px; }
QFrame#chatMediaCard {
    background: %(peer_bubble)s; border: 1px solid %(line)s; border-radius: 8px;
}
QLabel#chatCardTitle { font-size: 13px; color: %(chat_text)s; font-weight: 600; }
QLabel#chatCardDesc { font-size: 12px; color: %(sub)s; }
QLabel#chatCardTag { font-size: 11px; color: %(primary)s; }
QLabel#chatVoiceText {
    font-size: 12px; color: %(sub)s; background: %(peer_bubble)s;
    border-radius: 6px; padding: 6px 8px;
}
QFrame#chatBottom { background: %(chat_title)s; border-top: 1px solid %(line)s; }
QPushButton#chatExport {
    background: %(primary)s; color: %(on_primary)s; border: none;
    border-radius: 8px; padding: 9px 30px; font-size: 14px; font-weight: 600;
}
QPushButton#chatExport:hover { background: %(hover)s; }
QPushButton#chatExport:pressed { background: %(pressed)s; }
QFrame#chatSearchBar { background: %(chat_title)s; border-bottom: 1px solid %(line)s; }
QLineEdit#chatSearchEdit {
    background: %(card)s; border: 1px solid %(line)s; border-radius: 6px;
    padding: 5px 9px; font-size: 13px; color: %(text)s;
    selection-background-color: %(primary)s; selection-color: %(on_primary)s;
}
QPushButton#chatSearchBtn {
    background: transparent; border: none; border-radius: 5px;
    color: %(primary)s; font-size: 12px; padding: 3px 8px;
}
QPushButton#chatSearchBtn:hover { background: %(hover_bg)s; }
QLabel#chatSearchInfo { font-size: 12px; color: %(sub)s; }
QWidget#chatHit { background: %(soft)s; border-radius: 8px; }
QWidget[hit="true"] { background: %(soft)s; border-radius: 8px; }
"""


def chat_qss(name=None):
    """对话框样式表：始终跟随当前主题"""
    return _CHAT_QSS % palette(name)


QSS_TEMPLATE = """
/* ================= 基础 ================= */
QWidget {
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif;
    font-size: 13px; color: #1F2430;
}
QMainWindow, QDialog, QStackedWidget { background: #F2F5F9; }
QWidget#page { background: #F2F5F9; }

/* ================= 卡片 ================= */
QGroupBox {
    background: #FFFFFF;
    border: 1px solid #E6EAF2;
    border-radius: 12px;
    margin-top: 16px;
    padding: 18px 16px 16px 16px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 14px; padding: 0 5px;
    color: #1B2230; font-weight: 600; background: transparent;
}
QFrame#card, QWidget#card {
    background: #FFFFFF; border: 1px solid #E6EAF2; border-radius: 12px;
}

/* ================= 按钮 ================= */
QPushButton {
    background: #FFFFFF; border: 1px solid #D5DBE5; border-radius: 8px;
    padding: 7px 15px; color: #26303F; min-height: 18px;
}
QPushButton:hover { background: #F4F7FB; border-color: #BCC7D6; }
QPushButton:pressed { background: #E9EFF7; }
QPushButton:focus { border-color: #185FA5; outline: none; }
QPushButton:disabled {
    background: #F6F8FB; border-color: #ECF0F5; color: #B3BAC5;
}

QPushButton#primary {
    background: #185FA5; border-color: #185FA5; color: #FFFFFF; font-weight: 600;
    padding: 7px 18px;
}
QPushButton#primary:hover { background: #1B70C2; border-color: #1B70C2; }
QPushButton#primary:pressed { background: #12497F; border-color: #12497F; }
QPushButton#primary:disabled {
    background: #B7CCE2; border-color: #B7CCE2; color: #F4F8FC;
}

QPushButton#quiet {
    background: transparent; border: none; color: #185FA5; padding: 5px 9px;
}
QPushButton#quiet:hover { background: #EAF1F9; }
QPushButton#quiet:disabled { color: #B3BAC5; background: transparent; }

QPushButton#danger { color: #C0392B; border-color: #EFCFC9; }
QPushButton#danger:hover { background: #FDF3F1; border-color: #E4B2A9; }
QPushButton#danger:disabled { color: #C9CDD4; border-color: #EFEFEF; background: #FAFAFB; }

QPushButton#menubtn {
    background: transparent; border: none; padding: 7px 12px; color: #4B5563;
}
QPushButton#menubtn:hover { background: #F1F5FA; color: #1B2230; }
QPushButton#menubtn:pressed { background: #E7EEF7; }

QPushButton#chip {
    background: #F4F6FA; border: 1px solid #E6EAF2; border-radius: 15px;
    padding: 5px 17px; color: #5B6472;
}
QPushButton#chip:hover { background: #EAF1F9; border-color: #C6D9EE; color: #185FA5; }
QPushButton#chip:checked {
    background: #E8F1FB; border-color: #AFCCEA; color: #185FA5; font-weight: 600;
}

/* ================= 输入类控件 ================= */
QLineEdit, QComboBox, QDateTimeEdit, QSpinBox {
    background: #FFFFFF; border: 1px solid #D5DBE5; border-radius: 8px;
    padding: 7px 10px; selection-background-color: #185FA5;
    selection-color: #FFFFFF; min-height: 18px;
}
QLineEdit:focus, QComboBox:focus, QDateTimeEdit:focus, QSpinBox:focus {
    border-color: #185FA5;
}
QLineEdit:disabled, QComboBox:disabled, QDateTimeEdit:disabled {
    background: #F6F8FB; color: #A8B0BC;
}
QLineEdit[readOnly="true"] { background: #F6F8FB; color: #62707F; }
QLineEdit#search {
    padding-left: 8px; background: #FFFFFF;
}
QComboBox::drop-down { border: none; width: 24px; }
QComboBox::down-arrow { image: url("%(arrow)s"); width: 12px; height: 12px; }
QComboBox::down-arrow:disabled { image: url("%(arrow_disabled)s"); }
QComboBox QAbstractItemView {
    background: #FFFFFF; border: 1px solid #E6EAF2; border-radius: 8px;
    padding: 4px; outline: none;
    selection-background-color: #E8F1FB; selection-color: #1F2430;
}
QDateTimeEdit::drop-down { border: none; width: 20px; }

/* ================= 勾选框 / 单选框 ================= */
QCheckBox, QRadioButton { spacing: 8px; }
QCheckBox:disabled, QRadioButton:disabled { color: #A8B0BC; }

QCheckBox::indicator, QRadioButton::indicator {
    width: 16px; height: 16px;
    border: 1px solid #C6CEDB; background: #FFFFFF;
}
QCheckBox::indicator { border-radius: 4px; }
QRadioButton::indicator { border-radius: 9px; }

QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: #185FA5; }
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {
    border-color: #E3E7EE; background: #F7F8FA;
}

/* 勾选态：主色底 + 白勾（白勾是运行时生成的 png，QSS 画不出对勾） */
QCheckBox::indicator:checked {
    background: #185FA5; border-color: #185FA5;
    image: url("%(tick)s");
}
QCheckBox::indicator:checked:hover { background: #1B70C2; border-color: #1B70C2; }
QCheckBox::indicator:checked:disabled {
    background: #B7CCE2; border-color: #B7CCE2;
    image: url("%(tick_disabled)s");
}

/* 单选：白底圆框 + 主色实心圆点（圆点是运行时生成的 png，
   纯 border 技巧在不同缩放比例下容易糊），一眼能看出选中在哪 */
QRadioButton::indicator:checked {
    border: 1px solid #185FA5; background: #FFFFFF;
    image: url("%(dot)s");
}
QRadioButton::indicator:checked:disabled {
    border-color: #B7CCE2;
    image: url("%(dot_disabled)s");
}

/* 输出方式的三张「选项卡」：整行可点，选中时浅蓝底 + 主色边 */
QRadioButton[modeCard="true"] {
    background: #FBFCFE; border: 1px solid #E9EDF3; border-radius: 9px;
    padding: 9px 11px; color: #26303F;
}
QRadioButton[modeCard="true"]:hover { border-color: #C6D9EE; background: #F7FBFF; }
QRadioButton[modeCard="true"]:checked {
    border-color: #AFCCEA; background: #F1F8FE; color: #123A5E; font-weight: 600;
}

/* ================= Tab ================= */
QTabWidget::pane { border: none; background: #FFFFFF; }
QTabWidget::tab-bar { left: 4px; }
QTabBar::tab {
    background: transparent; padding: 10px 22px; margin-right: 2px;
    border: none; border-bottom: 2px solid transparent;
    color: #6B7280; font-weight: 500;
    /* 说明：内边距故意比视觉需要多留几像素 —— Windows 高分屏缩放
       （125%%/150%%）下字形实际渲染宽度会比字体度量多出 1~3px，
       余量不足时 Tab 数字（尤其最后一位）会被切掉 */
}
QTabBar::tab:hover:!selected { color: #1F2430; background: #F4F7FB; }
QTabBar::tab:selected {
    color: #185FA5; border-bottom: 2px solid #185FA5; font-weight: 600;
}

/* ================= 表格 ================= */
QTableView {
    background: #FFFFFF; border: none; alternate-background-color: #FAFBFD;
    selection-background-color: #E8F1FB; selection-color: #12324F;
    gridline-color: transparent; outline: none;
}
QTableView::item { padding: 4px 10px; border: none; }
QTableView::item:selected { background: #E8F1FB; color: #12324F; }
QHeaderView { background: transparent; }
QHeaderView::section {
    background: #F7F9FC; color: #6B7280; border: none;
    border-bottom: 1px solid #E9EDF3; padding: 9px 10px; font-weight: 600;
}
QHeaderView::section:hover { color: #1F2430; background: #F1F5FA; }
QTableCornerButton::section { background: #F7F9FC; border: none; }

/* ================= 列表 ================= */
QListWidget {
    background: #FBFCFE; border: 1px solid #E9EDF3; border-radius: 9px;
    padding: 4px; outline: none;
}
QListWidget::item {
    padding: 7px 9px; border-radius: 6px; color: #26303F;
}
QListWidget::item:hover { background: #F1F5FA; }
QListWidget::item:selected { background: #E8F1FB; color: #12324F; }

/* ================= 滚动条 ================= */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical {
    background: #D6DCE6; border-radius: 5px; min-height: 32px;
}
QScrollBar::handle:vertical:hover { background: #BFC9D6; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal {
    background: #D6DCE6; border-radius: 5px; min-width: 32px;
}
QScrollBar::handle:horizontal:hover { background: #BFC9D6; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* ================= 日志框 / 进度条 ================= */
QPlainTextEdit {
    background: #FBFCFE; border: 1px solid #E9EDF3; border-radius: 9px;
    padding: 9px; color: #3C4655;
    font-family: Consolas, "Cascadia Mono", "Courier New", monospace; font-size: 12px;
}
QProgressBar {
    background: #EBF0F7; border: none; border-radius: 8px;
    height: 16px; text-align: center; color: #3E4A5A; font-size: 11px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 #1B70C2, stop:1 #185FA5);
    border-radius: 8px;
}
/* 导出结果：进度条整条变成结果色（代码里用 setProperty('state', ...) 触发） */
QProgressBar[state="done"]::chunk { background: #17805B; }
QProgressBar[state="cancel"]::chunk { background: #B26A00; }
QProgressBar[state="fail"]::chunk { background: #C0392B; }

/* ================= 顶栏 / 状态栏 ================= */
QWidget#topbar { background: #FFFFFF; border-bottom: 1px solid #E9EDF3; }
QWidget#toolbar { background: #FFFFFF; border-bottom: 1px solid #EDF0F5; }
QStatusBar { background: #FFFFFF; border-top: 1px solid #E9EDF3; color: #6B7280; }
QStatusBar::item { border: none; }
QStatusBar QLabel { padding: 0 2px; }

/* ================= 文字标签 ================= */
QLabel#h1 { font-size: 18px; font-weight: 600; color: #161C28; }
QLabel#h2 { font-size: 15px; font-weight: 600; color: #1B2230; }
QLabel#sub { font-size: 12px; color: #6B7280; }
QLabel#tip { font-size: 12px; color: #7A8494; }
QLabel#tipGood { font-size: 12px; color: #17805B; }
QLabel#tipWarn { font-size: 12px; color: #B26A00; }

QLabel#badge {
    background: #EEF2F8; color: #5B6472; border-radius: 10px;
    padding: 3px 11px; font-size: 12px;
}
QLabel#badgeActive {
    background: #E8F1FB; color: #185FA5; border-radius: 10px;
    padding: 3px 11px; font-size: 12px; font-weight: 600;
}
QLabel#badgeGood {
    background: #E7F5EF; color: #17805B; border-radius: 10px;
    padding: 2px 10px; font-size: 12px;
}
QLabel#badgeBad {
    background: #FDECEA; color: #C0392B; border-radius: 10px;
    padding: 2px 10px; font-size: 12px;
}
QLabel#badgeGray {
    background: #F1F3F6; color: #6B7280; border-radius: 10px;
    padding: 2px 10px; font-size: 12px;
}

/* 空状态里的步骤序号 */
QLabel#stepNo {
    background: #E8F1FB; color: #185FA5; border-radius: 11px;
    min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px;
    font-weight: 600; font-size: 12px;
}
QLabel#stepTitle { font-size: 13px; font-weight: 600; color: #26303F; }
QLabel#stepDesc { font-size: 12px; color: #7A8494; }

/* ================= 菜单 / 提示气泡 ================= */
QMenu { background: #FFFFFF; border: 1px solid #E6EAF2; border-radius: 9px; padding: 6px; }
QMenu::item { padding: 7px 26px 7px 14px; border-radius: 6px; }
QMenu::item:selected { background: #F1F5FA; }
QMenu::separator { height: 1px; background: #EEF1F6; margin: 4px 8px; }
QToolTip {
    background: #1F2430; color: #FFFFFF; border: none;
    padding: 6px 9px; border-radius: 6px;
}

/* ================= 消息框 ================= */
QMessageBox { background: #FFFFFF; }
QMessageBox QLabel { color: #26303F; }
QMessageBox QPushButton { min-width: 76px; }
"""


def stylesheet(name=None):
    """生成最终样式表：把运行时生成的勾/箭头贴图路径填进模板

    必须在 QApplication 创建之后调用（生成贴图要用 QPixmap，需要 GUI 环境）。
    name 仅为兼容 apply() 的调用形式；换色在 _recolor 里统一处理。
    """
    a = icons.qss_assets()
    return QSS_TEMPLATE % {
        'tick': a.get('tick', ''),
        'tick_disabled': a.get('tick_disabled', ''),
        'arrow': a.get('arrow', ''),
        'arrow_disabled': a.get('arrow_disabled', ''),
        'dot': a.get('dot', ''),
        'dot_disabled': a.get('dot_disabled', ''),
    }
