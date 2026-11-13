# -*- coding: utf-8 -*-
"""用系统程序打开本地文件 · 在资源管理器里定位 · 列出本机可用的播放器

为什么单独一层：
    ⚠️ **绝对不要用 subprocess.Popen 去调 explorer / 播放器。**
    打包成 --windowed 的 exe 之后 sys.stdout / sys.stderr 都是 None，
    子进程继承到的是无效标准句柄，Popen 直接抛 WinError 6（句柄无效）。
    异常被 except 吞掉，用户看到的就是"点了没反应 / 打开文件夹失败"。
    ShellExecuteW 走 shell 关联，不依赖子进程句柄，也会自动补 .exe——但
    ⚠️ 目标写成 explorer.exe 时，--windowed 打包后会被系统拒绝（返回 ≤32）。
    定位并选中文件必须走 SHOpenFolderAndSelectItems，见 reveal_in_explorer()。

4.2 的视频 mp4 本身是明文真实文件，不需要解密，因此播放完全交给系统：
    单击  → 默认关联程序
    右键  → 我们自己枚举出来的播放器列表（见 list_video_players）

⚠️ 为什么不用系统自带的「打开方式」对话框（实测于 Win11 build 26200）：
    - `ShellExecuteW(..., 'openas', ...)` 返回 **31**（<=32 即失败），什么也不弹；
    - `SHOpenWithDialog()` 直接**挂死**，进程活着但一个窗口都不创建。
    根因是注册表里 `HKCR\\.mp4` 的默认 ProgId 指向 `KMPlayer.mp4`，
    而 KMPlayer 已经卸载 —— 残留关联把 shell 的选择程序链路弄坏了。
    所以这里改成自己读注册表枚举处理器，选程序这一步也不依赖 shell 对话框。
"""
import os
import sys

_HAS_WIN = sys.platform == 'win32'

# 常见播放器的安装位置（环境变量会展开，逐个探测，存在即列出）
_KNOWN_PLAYERS = (
    ('PotPlayer', (
        r'%ProgramFiles%\DAUM\PotPlayer\PotPlayerMini64.exe',
        r'%ProgramFiles(x86)%\DAUM\PotPlayer\PotPlayerMini.exe',
        r'%ProgramFiles%\PotPlayer\PotPlayerMini64.exe',
    )),
    ('VLC media player', (
        r'%ProgramFiles%\VideoLAN\VLC\vlc.exe',
        r'%ProgramFiles(x86)%\VideoLAN\VLC\vlc.exe',
    )),
    ('MPC-HC', (
        r'%ProgramFiles%\MPC-HC\mpc-hc64.exe',
        r'%ProgramFiles(x86)%\MPC-HC\mpc-hc.exe',
    )),
    ('MPC-BE', (
        r'%ProgramFiles%\MPC-BE x64\mpc-be64.exe',
        r'%ProgramFiles(x86)%\MPC-BE\mpc-be.exe',
    )),
    ('KMPlayer', (
        r'%ProgramFiles%\KMPlayer 64X\KMPlayer64.exe',
        r'%ProgramFiles(x86)%\The KMPlayer\KMPlayer.exe',
    )),
    ('mpv', (
        r'%ProgramFiles%\mpv\mpv.exe',
    )),
)

# App Paths 里按 exe 名认播放器（比路径白名单稳，装哪儿都能找到）
_PLAYER_HINTS = ('potplayer', 'vlc', 'mpc-hc', 'mpc-be', 'kmplayer',
                 'qqplayer', '5kplayer', 'mpv', 'gomplayer', 'wmplayer')

# shell 自己/系统外壳，绝不列进"播放器"
_NOT_PLAYERS = ('explorer.exe', 'rundll32.exe', 'dllhost.exe',
                'shell32.dll', 'cmd.exe', 'powershell.exe')


def _expand(p):
    return os.path.expandvars(os.path.expanduser(p or ''))


# ------------------------------------------------------------------ shell
LAST_REVEAL_ERROR = ''          # 最近一次定位失败的原因（界面会带给用户看）


def last_reveal_error():
    return LAST_REVEAL_ERROR


def path_exists(path):
    """文件/目录是否存在（兼容 Windows 超长路径）

    普通 os.path.exists 在 >260 字符且系统没开 LongPathsEnabled 时会误报
    False；路径一深（或用户把媒体库放到很深的目录）就容易踩到。长路径前缀只用在检查上，
    交给 explorer 的仍然是普通绝对路径。
    """
    if not path:
        return False
    try:
        if os.path.exists(path):
            return True
    except (OSError, ValueError):
        pass
    if os.name == 'nt' and not path.startswith('\\\\?\\'):
        try:
            return os.path.exists('\\\\?\\' + os.path.abspath(path))
        except (OSError, ValueError):
            pass
    return False


def _shell_execute(verb, file, params=None):
    """ShellExecuteW 包装：返回 True 表示 shell 接受了这次调用

    ShellExecuteW 的返回值是 HINSTANCE，**>32 才算成功**，其余是错误码。
    这里必须显式设 argtypes / restype：不设的话 Win64 下指针会被截成 int，
    偶尔表现为随机失败。
    """
    if not _HAS_WIN:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        sh = ctypes.windll.shell32.ShellExecuteW
        sh.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                       wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
        sh.restype = ctypes.c_void_p
        rc = sh(None, verb, file, params, None, 1)   # 1 = SW_SHOWNORMAL
        return int(rc or 0) > 32
    except Exception:
        return False


def _reveal_shell_api(path):
    """用 SHOpenFolderAndSelectItems 定位并选中（Windows 官方 API）

    比 explorer.exe /select 可靠：不拼命令行、不受引号 / 空格 / 长路径影响，
    也不依赖 explorer.exe 的 PATH 或 .exe 关联。hr=0 就是已经选中了。
    """
    import ctypes
    from ctypes import (POINTER, byref, c_int, c_uint32, c_void_p,
                        c_wchar_p)
    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32
    ole32.CoInitializeEx.argtypes = [c_void_p, c_uint32]
    ole32.CoInitializeEx.restype = c_int
    ole32.CoUninitialize.argtypes = []
    ole32.CoTaskMemFree.argtypes = [c_void_p]
    shell32.SHParseDisplayName.argtypes = [
        c_wchar_p, c_void_p, POINTER(c_void_p), c_uint32, c_void_p]
    shell32.SHParseDisplayName.restype = c_int
    shell32.SHOpenFolderAndSelectItems.argtypes = [
        c_void_p, c_uint32, c_void_p, c_uint32]
    shell32.SHOpenFolderAndSelectItems.restype = c_int
    # Qt 主线程通常已初始化过 COM：S_OK / S_FALSE 都要配对一次 CoUninitialize；
    # RPC_E_CHANGED_MODE（已按 MTA 初始化）则不能动别人的计数。
    inited = ole32.CoInitializeEx(None, 2) >= 0   # 2 = COINIT_APARTMENTTHREADED
    pidl = c_void_p()
    try:
        if (shell32.SHParseDisplayName(path, None, byref(pidl), 0, None) < 0
                or not pidl):
            return False
        return shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) >= 0
    finally:
        if pidl:
            ole32.CoTaskMemFree(pidl)
        if inited:
            ole32.CoUninitialize()


def open_path(path):
    """用系统默认关联程序打开（视频 / 文档 / 文件夹都行）"""
    if not path:
        return False
    target = os.path.normpath(path)
    if not path_exists(target):
        return False
    if not _HAS_WIN:
        try:
            os.startfile(target)
            return True
        except Exception:
            return False
    return _shell_execute('open', target)


def open_with_exe(exe, path):
    """用指定播放器（exe 完整路径）打开文件"""
    if not exe or not path:
        return False
    exe = _expand(exe)
    if not path_exists(exe):
        return False
    return _shell_execute('open', os.path.normpath(exe),
                          '"%s"' % os.path.normpath(path))


def open_entry(target, path):
    """按 list_video_players() 给的目标打开（就是 open_with_exe 的别名）"""
    return open_with_exe(target, path)


def reveal_in_explorer(path):
    """在资源管理器里**定位并选中** path 这一个文件，返回是否成功

    用户明确要求「定位文件位置，而不是只打开文件夹」，所以选中是硬要求：
    ① 系统 Shell API（最可靠）→ ② explorer.exe /select（三种写法）。
    全失败时返回 False，绝不退化成「只打开目录算成功」。失败原因写进
    LAST_REVEAL_ERROR，界面提示会带上，不再只显示一个光秃秃的文件名。
    """
    global LAST_REVEAL_ERROR
    LAST_REVEAL_ERROR = ''
    if not path:
        return False
    # 强制绝对 + 规范化：上游万一给的是相对路径，explorer 会按自己的工作
    # 目录去解析，结果就是「目录开了但什么都没选中」。
    target = os.path.normpath(os.path.abspath(path))
    if not path_exists(target):
        LAST_REVEAL_ERROR = '文件不存在'
        return False
    if not _HAS_WIN:
        try:
            os.startfile(os.path.dirname(target))
            return True
        except Exception:
            return False
    try:
        if _reveal_shell_api(target):
            return True
        LAST_REVEAL_ERROR = '系统定位 API 未能选中'
    except Exception as e:
        LAST_REVEAL_ERROR = '系统定位 API 异常：%s' % e
    # 兜底：--windowed 打包后实测这条会失败（系统拒绝启动 explorer.exe），
    # 但源码运行 / 部分机器仍可能可用，保留不删。
    exe = os.path.join(os.environ.get('WINDIR') or r'C:\Windows',
                       'explorer.exe')
    if not os.path.isfile(exe):
        exe = 'explorer.exe'
    for params in ('/select,"%s"' % target,
                   '/select,%s' % target,
                   '/e,/select,"%s"' % target):
        if _shell_execute('open', exe, params):
            return True
    LAST_REVEAL_ERROR += '；explorer /select 也被拒绝（返回 ≤32）'
    return False


# ------------------------------------------------------------ 注册表解析
def _reg(path, name=''):
    """读注册表值，失败返回 None。path 相对 HKEY_CLASSES_ROOT"""
    try:
        import winreg
    except Exception:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except Exception:
        return None


def _reg_subkeys(path):
    try:
        import winreg
    except Exception:
        return []
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path) as k:
            i = 0
            while True:
                try:
                    out.append(winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
    except Exception:
        pass
    return out


def _reg_values(path):
    """读某个键下的全部 (名, 值)，失败返回 []"""
    try:
        import winreg
    except Exception:
        return []
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path) as k:
            i = 0
            while True:
                try:
                    n, v, _t = winreg.EnumValue(k, i)
                except OSError:
                    break
                i += 1
                out.append((n, v))
    except Exception:
        pass
    return out


def _reg_hkcu(path, name=''):
    """读 HKEY_CURRENT_USER 下的值"""
    try:
        import winreg
    except Exception:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except Exception:
        return None


def _values_hkcu(path):
    """读 HKCU 某个键下的全部 (名, 值)

    ⚠️ FileExts\\.mp4\\OpenWithList 在 **HKCU**，不在 HKCR。
    早先误用 HKCR 读，永远空 —— 表现为"播放器列表只有默认那一个"。
    """
    try:
        import winreg
    except Exception:
        return []
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            i = 0
            while True:
                try:
                    n, v, _t = winreg.EnumValue(k, i)
                except OSError:
                    break
                i += 1
                out.append((n, v))
    except Exception:
        pass
    return out


def _user_choice_progid(ext='.mp4'):
    """资源管理器里「始终用这个程序打开」实际选中的 ProgId（最权威）"""
    return _reg_hkcu(r'Software\Microsoft\Windows\CurrentVersion\Explorer'
                     r'\FileExts\%s\UserChoice' % ext, 'ProgId')


def _cmd_exe(cmd):
    """从 `"C:\\x\\y.exe" /arg "%L"` 这种 command 串里抠出 exe 完整路径"""
    if not cmd:
        return ''
    s = _expand(cmd).strip()
    if not s:
        return ''
    if s.startswith('"'):
        end = s.find('"', 1)
        cand = s[1:end] if end > 0 else s.strip('"')
    else:
        cand = s
        # 未加引号但路径里带空格：从最长前缀往回退，找真实存在的文件
        for cut in range(len(cand), 0, -1):
            p = cand[:cut].rstrip()
            if p.lower().endswith('.exe') and os.path.isfile(p):
                return os.path.normpath(p)
        cand = s.split(' ')[0]
    cand = os.path.normpath(cand)
    return cand if os.path.isfile(cand) else ''


def _progid_exe(progid):
    """ProgId → exe（读 HKCR\\<progid>\\shell\\open\\command）"""
    if not progid:
        return ''
    return _cmd_exe(_reg('%s\\shell\\open\\command' % progid))


def _args_compat(exe_name):
    """exe 名 → exe 路径：先 Applications，再 App Paths"""
    if not exe_name:
        return ''
    p = _cmd_exe(_reg(r'Applications\%s\shell\open\command' % exe_name))
    if p:
        return p
    return _app_paths().get(exe_name.lower(), '')


def _disp_name(key, fallback):
    """取 ProgId / Applications 项的描述文字做显示名"""
    v = _reg(key)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return fallback


def _file_desc(exe):
    """exe 的「文件说明」（Google Chrome / Windows Media Player 这种友好名）

    读版本资源 FileDescription。拿不到就退回文件名去扩展名 ——
    绝不能把 "Applications\\chrome.exe" 这种注册表键名直接摆给用户看。
    """
    if not exe or not _HAS_WIN:
        return ''
    try:
        import ctypes
        from ctypes import wintypes
        ver = ctypes.windll.version
        path = os.path.normpath(exe)
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ''
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return ''
        ptr = ctypes.c_void_p()
        ln = wintypes.UINT()
        sub = '\\StringFileInfo\\%04x%04x\\FileDescription'
        # 语言/代码页各机器不同，直接用 Translation 里查到的第一组
        trans = ctypes.c_void_p()
        tlen = wintypes.UINT()
        lang = 0x0409
        cp = 0x04B0
        if ver.VerQueryValueW(buf, '\\VarFileInfo\\Translation',
                              ctypes.byref(trans), ctypes.byref(tlen)):
            if tlen.value >= 4:
                vals = ctypes.cast(trans, ctypes.POINTER(wintypes.WORD))
                lang, cp = vals[0], vals[1]
        if ver.VerQueryValueW(buf, sub % (lang, cp), ctypes.byref(ptr),
                              ctypes.byref(ln)):
            # VerQueryValue 给的长度**包含结尾 NUL**，不裁掉的话名字尾部会
            # 多一个 \x00，摆进菜单就是「Google Chrome 播放」这种莫名空格
            return ctypes.wstring_at(ptr.value).strip().strip('\x00')
        if ver.VerQueryValueW(buf, sub % (0x0804, 0x04B0),
                              ctypes.byref(ptr), ctypes.byref(ln)):
            return ctypes.wstring_at(ptr.value).strip().strip('\x00')
    except Exception:
        pass
    return ''


def _pretty_name(target):
    """exe 路径 → 给人看的名字"""
    if not target:
        return ''
    desc = _file_desc(target)
    if desc:
        return desc
    stem = os.path.splitext(os.path.basename(target))[0]
    return stem


def default_entry(ext='.mp4'):
    """当前默认打开方式：('名字', 'exe 或 uwp:AUMID')，拿不到返回 ('', '')"""
    if not _HAS_WIN:
        return '', ''
    progid = _user_choice_progid(ext)
    if progid:
        name = _disp_name(progid, progid)
        if progid.lower().startswith('applications\\'):
            exe_name = progid.split('\\', 1)[1]
            exe = _args_compat(exe_name) or _progid_exe(progid)
            if exe:
                return name, exe
        exe = _progid_exe(progid)
        if exe:
            return name, exe
    exe = _assoc_exe(ext)
    if exe:
        return os.path.splitext(os.path.basename(exe))[0], exe
    return '', ''


def default_player(ext='.mp4'):
    """当前 .mp4 默认关联程序 exe 完整路径（拿不到返回 ''）"""
    name, target = default_entry(ext)
    return '' if target.startswith('uwp:') else target


def _assoc_exe(ext='.mp4'):
    """AssocQueryStringW 取默认程序（比手工拼注册表更兼容，但不管 UserChoice）"""
    if not _HAS_WIN:
        return ''
    try:
        import ctypes
        from ctypes import wintypes
        shlwapi = ctypes.windll.shlwapi
        shlwapi.AssocQueryStringW.argtypes = [
            ctypes.c_ulong, ctypes.c_int, wintypes.LPCWSTR,
            wintypes.LPCWSTR, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        shlwapi.AssocQueryStringW.restype = ctypes.c_long
        n = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        # 0x2 = ASSOCSTR_EXECUTABLE
        if shlwapi.AssocQueryStringW(0, 2, ext, 'open', buf,
                                     ctypes.byref(n)) == 0:
            return buf.value or ''
    except Exception:
        pass
    return ''


def _app_paths():
    """注册表 App Paths 里的可执行程序 → {小写 exe 名: 完整路径}"""
    try:
        import winreg
    except Exception:
        return {}
    out = {}
    key = r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths'
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in views:
            try:
                base = winreg.OpenKey(root, key, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(base, i)
                except OSError:
                    break
                i += 1
                try:
                    k = winreg.OpenKey(base, name)
                    val = winreg.QueryValueEx(k, '')[0]
                    k.Close()
                except OSError:
                    continue
                if val and os.path.isfile(val):
                    out.setdefault(name.lower(), val)
            try:
                base.Close()
            except Exception:
                pass
    return out


def _uwp_name(aumid):
    """UWP AUMID → 家族名（仅用于日志/兜底显示）"""
    return aumid.split('!')[0].split('_')[0]


# --------------------------------------------------------------- 播放器探测
def list_video_players(ext='.mp4'):
    """本机可用的播放器：[(显示名, exe 完整路径), ...]

    四条来源合并去重（前两条覆盖绝大多数机器，后两条补漏）：
      1. 资源管理器里的默认关联（UserChoice）—— 排第一，名字带（默认）
      2. 「打开过」列表 OpenWithList —— 用户实际用过的程序
      3. HKCR\\<ext> 的 ProgId / OpenWithProgids —— 注册过的处理器
      4. 常见播放器安装路径白名单 + App Paths

    ⚠️ 不信 HKCR\\.mp4 的**默认值**：卸载不干净的软件会留下幽灵 ProgId
    （本机就是 `KMPlayer.mp4`，程序早没了），照着它找必然落空。

    ⚠️ 不列 UWP 应用（媒体播放器 / 电影和电视）：实测本机用
    `explorer shell:AppsFolder\\<AUMID>`、`os.startfile`、COM
    IApplicationActivationManager 三种方式激活，**都返回成功但进程从未出现**，
    列进菜单等于又制造一个"点了没反应"。只列真实 exe。
    """
    if not _HAS_WIN:
        return []
    out = []
    seen = set()

    def add(name, target):
        if not target:
            return
        target = _expand(target)
        key = os.path.normcase(os.path.normpath(target))
        if key in seen or not os.path.isfile(target):
            return
        if os.path.basename(target).lower() in _NOT_PLAYERS:
            return
        # 名字拿不到或者是 "xxx.exe" / "Applications\\xxx.exe" 这种键名，
        # 一律用 exe 的 FileDescription 兜住，别把注册表内部串暴露给用户
        if (not name) or name.lower().endswith('.exe') or '\\' in name:
            name = _pretty_name(target) or name
        seen.add(key)
        out.append((name, target))

    # 1. 默认关联排第一
    dname, dtarget = default_entry(ext)
    if dtarget:
        add(dname, dtarget)
        if out:
            out[0] = (out[0][0] + '（默认）', out[0][1])

    # 2. OpenWithList（值为 exe 名）—— 注意在 HKCU，不在 HKCR
    for _n, val in _values_hkcu(r'Software\Microsoft\Windows\CurrentVersion'
                                r'\Explorer\FileExts\%s\OpenWithList' % ext):
        if not isinstance(val, str) or not val or '!' in val:
            continue                         # '!' 是 UWP，见上面的说明
        add('', _args_compat(val) or '')

    # 3. ProgId / OpenWithProgids
    progids = []
    p0 = _reg(ext)
    if isinstance(p0, str) and p0:
        progids.append(p0)
    for other in _reg_subkeys(ext + r'\OpenWithProgids'):
        progids.append(other)
    for progid in progids:
        cmd = _reg('%s\\shell\\open\\command' % progid)
        if not cmd or 'rundll32' in cmd.lower():
            continue                         # 占位处理器，点了没反应
        exe = _cmd_exe(cmd)
        add('', exe)

    # 4. 已知安装路径 + App Paths
    for name, cands in _KNOWN_PLAYERS:
        for c in cands:
            p = _expand(c)
            if os.path.isfile(p):
                add(name, p)
                break
    for exe_name, exe in _app_paths().items():
        stem = exe_name[:-4] if exe_name.endswith('.exe') else exe_name
        if any(h in stem for h in _PLAYER_HINTS):
            add('', exe)

    return out
