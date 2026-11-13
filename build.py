# -*- coding: utf-8 -*-
"""一键打包脚本：生成单文件 exe

用法（依赖必须装在"跑打包"的那个 Python 里，不同环境不共享）：
    python build.py
或用项目 .venv（依赖齐全，推荐）：
    .venv\\Scripts\\python.exe build.py

产物：dist\\微信聊天记录导出工具V1.0.0.exe
      连本地库还是线上库，由 app_config.AUTH_DB_TARGET 决定（改完必须重新打包，
      因为字面量是烧进 exe 的）；打包日志第一行会打印实际连的库，打完可以用
      scripts/verify_baked_db.py 从 exe 里反查确认。

打包要点：
    --onefile    单文件：所有依赖压进一个 exe，运行时自解压到临时目录
    --windowed   GUI 程序，不带控制台窗口
    --add-data   把图标和两个 bootstrap 脚本放进包里
                 （bootstrap 脚本按数据文件放，运行时用 runpy 按脚本执行）
    --paths      本目录（get_key_v42 / scan / key_v4 / decrypt_db 都在这里，
                 不再依赖上级仓库或旧仓库的任何文件）
    --hidden-import  这些模块由 bootstrap 脚本在运行时 import，
                 PyInstaller 静态分析看不到，必须显式声明；
                 yara / pefile 藏在 try-import 和函数内 import 后面，一并声明

数据不随包发布：首次运行会在 exe 同级创建 WeChatData，
每个微信号在 accounts\\<wxid>\\ 下拥有独立配置与解密数据库。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))     # wechat_exporter/
sys.path.insert(0, HERE)
# exe 名取「程序名 + 版本号」，与 app_config 保持同一个来源，升版本不用改两处
from app_config import (APP_NAME, APP_VERSION,       # noqa: E402
                        AUTH_DB, AUTH_DB_TARGET)
# 产物名固定「程序名+版本号」，不按库区分：连哪台库由 app_config.AUTH_DB_TARGET 决定，
# 打包日志第一行会打印出来。留着两份不同名的 exe 反而容易发错包。
NAME = APP_NAME + APP_VERSION

# 打包前必须能 import 的模块：(import 名, pip 包名)
# 少了任何一个，打出来的 exe 都能"打包成功"但双击就崩，所以在开工前先挡一道
REQUIRED_MODULES = [
    ('PyQt5', 'PyQt5'),
    ('openpyxl', 'openpyxl'),
    ('zstandard', 'zstandard'),
    ('Crypto', 'pycryptodome'),
    ('pefile', 'pefile'),
    ('yara', 'yara-python'),
    ('pymysql', 'PyMySQL'),
]


def _can_import(name):
    """试着 import 一次；失败只返回 False，不抛异常"""
    try:
        __import__(name)
        return True
    except Exception:
        return False


def check_requirements():
    """打包前逐个试 import，缺一个就中止

    为什么必须显式检查：PyInstaller 对找不到的 --hidden-import 只打一条 warning
    就继续跑，照样产出 exe，直到用户双击才报 ModuleNotFoundError。
    这个坑已经踩过一次——pymysql 装在了 .venv，而打包用的是系统 Python。
    """
    missing = [(mod, pkg) for mod, pkg in REQUIRED_MODULES if not _can_import(mod)]
    if not missing:
        return True
    print('[ERROR] 当前打包环境缺依赖，硬打出来的 exe 会在启动时崩：')
    for mod, pkg in missing:
        print('        %-10s  →  pip install %s' % (mod, pkg))
    print('        当前解释器：%s' % sys.executable)
    print('        项目 .venv 里依赖是齐的，也可以直接用它打包：')
    print('              .venv\\Scripts\\python.exe build.py')
    return False


def main():
    """拼出 PyInstaller 命令行并同步执行

    不写 .spec 文件，全部参数都在这条命令里，好处是"改一行就能看出改了什么"。
    """
    # 先确认依赖齐了再开工：缺模块时 PyInstaller 不会失败，只会产出坏包
    if not check_requirements():
        return 1

    # 把「烧进去的是哪台库」打在日志里：打错版本是最难倒查的一类问题
    print('[INFO] 授权库目标 = %s  →  %s:%s/%s' % (
        AUTH_DB_TARGET, AUTH_DB['host'], AUTH_DB['port'], AUTH_DB['database']))
    print('[INFO] 产物名 = %s.exe' % NAME)

    # --add-data 的分隔符在 Windows 上是分号
    dat = lambda src, dst: ['--add-data', src + os.pathsep + dst]
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm', '--clean',                 # 覆盖旧产物、清缓存
        '--onefile', '--windowed',
        '--name', NAME,
        '--icon', os.path.join(HERE, 'assets', 'app_icon.ico'),
        # 图标也要在运行时读（设置窗口图标用），所以单独作为数据文件再带一份
        *dat(os.path.join(HERE, 'assets', 'app_icon.ico'), 'assets'),
        # 两个子进程脚本：打包后由 exe 以 --bootstrap-* 参数执行
        *dat(os.path.join(HERE, '_bootstrap_keys.py'), '.'),
        *dat(os.path.join(HERE, '_bootstrap_decrypt.py'), '.'),
        # 新解密核心全在本目录，不再引用上级/旧仓库
        '--paths', HERE,
        '--hidden-import', 'get_key_v42',
        '--hidden-import', 'scan',
        '--hidden-import', 'key_v4',
        '--hidden-import', 'decrypt_db',
        '--hidden-import', 'yara',
        '--hidden-import', 'pefile',
        # 登录校验：auth.license_db 是 main() 里函数内 import 的，
        # pymysql 内部又有大量运行时动态导入，两个都得显式声明
        '--hidden-import', 'auth.license_db',
        '--hidden-import', 'pymysql',
        # 已解密媒体库：main_window / settings_dialog 里都是函数内 import，
        # 显式声明一遍，免得静态分析漏掉后 exe 里点开图片才报 ModuleNotFoundError
        '--hidden-import', 'core.media_store',
        # 单实例用 QLocalServer/QLocalSocket（PyQt5.QtNetwork），托盘外壳是
        # main() 里函数内 import 的：显式声明，避免 exe 启动就 ModuleNotFoundError
        '--hidden-import', 'PyQt5.QtNetwork',
        '--hidden-import', 'ui.app_shell',
        # 中间产物/输出都放在本目录下，避免污染仓库根
        '--distpath', os.path.join(HERE, 'dist'),
        '--workpath', os.path.join(HERE, 'build'),
        '--specpath', os.path.join(HERE, 'build'),
        os.path.join(HERE, 'main.py'),
    ]
    print(' '.join(cmd))
    # cwd 设为本目录：PyInstaller 的临时目录默认建在当前盘，E 盘空间不足时要在此前切换
    return subprocess.call(cmd, cwd=HERE)


if __name__ == '__main__':
    sys.exit(main())
