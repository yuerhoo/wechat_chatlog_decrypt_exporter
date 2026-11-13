# -*- coding: utf-8 -*-
"""密码的本地加密存放（Windows DPAPI）

登录框勾了「记住密码」时，密码不是明文写进注册表，而是交给 Windows 的
CryptProtectData 加密一次，再把 base64 密文存进 QSettings。

为什么这么做：
    DPAPI 的密钥由**当前 Windows 用户的登录凭据**派生，加密和解密都要求
    "同一个用户、同一台机器"。所以：
      · 注册表里躺的是密文，打开 regedit 抄不出密码；
      · 把密文拷到别的机器 / 换个 Windows 用户，解不开（unprotect 返回空串，
        界面当"没记住"处理，不弹错、不挡登录）；
      · 用户改了 Windows 登录密码，旧密文失效，效果同上。

边界（必须清楚）：
    这是"防止密码明文落地"，不是"防止本机其他程序读"。同一用户下跑的任何
    程序都能调 CryptUnprotectData 解开它。真要做到"本机也读不到"，只能不记住。

两个函数都不抛异常：加密失败返回空串（调用方存个空值，等于没记住），
解密失败也返回空串。宁可"这次没记住密码"，绝不让登录框崩在启动路径上。
"""
import base64
import ctypes
import ctypes.wintypes as wt

# 禁止 DPAPI 弹任何 UI：解密失败时系统可能弹对话框要求输入凭据，
# 而这段代码跑在登录框的构造里，弹窗会卡住界面
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DATA_BLOB(ctypes.Structure):
    """CRYPTOAPI_BLOB：一段带长度的字节缓冲，DPAPI 的输入输出都用它

    字段顺序必须与 Win32 定义一致；64 位下 cbData 后面有 4 字节对齐填充，
    ctypes 会自动补，不要手动加 _pad。
    """
    _fields_ = [('cbData', wt.DWORD),
                ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _to_blob(data):
    """bytes → (DATA_BLOB, 持有内存的 buffer)

    第二个返回值必须被调用方持有到 API 调用结束：它一旦被 GC 回收，
    DATA_BLOB.pbData 就成了野指针。
    """
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buf


# 显式声明 argtypes/restype：不声明的话 ctypes 按 C int 处理返回值与指针，
# 64 位的 DATA_BLOB* 会被截断成 32 位，表现为必然失败或进程崩溃
_crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

_crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB),      # pDataIn
    ctypes.c_void_p,                 # szDataDescr（描述文字，不用）
    ctypes.c_void_p,                 # pOptionalEntropy（附加熵，不用）
    ctypes.c_void_p,                 # pvReserved
    ctypes.c_void_p,                 # pPromptStruct
    wt.DWORD,                        # dwFlags
    ctypes.POINTER(_DATA_BLOB),      # pDataOut
]
_crypt32.CryptProtectData.restype = wt.BOOL

_crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB),      # pDataIn
    ctypes.c_void_p,                 # ppszDataDescr（输出描述，不用）
    ctypes.c_void_p,                 # pOptionalEntropy
    ctypes.c_void_p,                 # pvReserved
    ctypes.c_void_p,                 # pPromptStruct
    wt.DWORD,                        # dwFlags
    ctypes.POINTER(_DATA_BLOB),      # pDataOut
]
_crypt32.CryptUnprotectData.restype = wt.BOOL

# out.pbData 由 API 用 LocalAlloc 分配，必须 LocalFree 归还
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p


def protect(text):
    """明文 → base64 密文；空输入或失败返回空串"""
    if not text:
        return ''
    try:
        src, _keep = _to_blob(text.encode('utf-8'))
        out = _DATA_BLOB()
        ok = _crypt32.CryptProtectData(
            ctypes.byref(src), None, None, None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
        if not ok:
            return ''
        try:
            raw = ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))
        return base64.b64encode(raw).decode('ascii')
    except Exception:
        return ''


def unprotect(token):
    """base64 密文 → 明文；空输入、格式不对、换用户/换机器解不开，一律返回空串"""
    if not token:
        return ''
    try:
        src, _keep = _to_blob(base64.b64decode(token))
        out = _DATA_BLOB()
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(src), None, None, None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
        if not ok:
            return ''
        try:
            return ctypes.string_at(out.pbData, out.cbData).decode('utf-8')
        finally:
            _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))
    except Exception:
        return ''
