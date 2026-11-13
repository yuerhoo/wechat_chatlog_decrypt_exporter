# -*- coding: utf-8 -*-
"""授权数据库：登录校验与导出次数控制的唯一入口

只做六件事，刻意不扩张（不做连接池、不做 ORM、不做用户管理界面）：
    1) get_conn()                     建一次短连接，用完就关；
    2) login(...)                     验账号密码 + 版本 + 有效期 → (是否通过, 给用户看的文案, 授权信息)，
                                      并把这次尝试写进 login_log；
    3) log_login(...)                 往 login_log 插一行（账号/结果/版本/机器名/设备ID/IP），
                                      写失败只吞掉、不挡登录；
    4) hash_password / check_password 密码哈希与校验（建账号、改密码用）；
    5) check_export_limit(...)        导出前的次数校验（license.export_limit + 数 export_log 成功行数）；
    6) log_export(...)                导出完成/失败后往 export_log 插一行，写失败吞掉、不挡导出。

密码不是明文存的，格式：pbkdf2_sha256$迭代次数$盐(hex)$哈希(hex)。
校验用 hmac.compare_digest 做定长比较，不按字节提前返回。

数据库账号只有三个权限：SELECT license、INSERT login_log、SELECT export_log。
它改不了 expire_at，也改不了 password 和 export_limit——这正是把写权限收窄的意义：
就算有人解包 exe 拿到这个账号，也没法给自己续期、也没法给自己加导出次数。

授权版本的含义：version 是「允许使用的最高版本」。
    客户端版本 <= 授权版本  → 放行；填 * 表示不限版本。
    所以卖 V1.0 的授权，客户拿到 V2.0 的包会被挡住（这正是版本控制的目的）；
    反过来授权 V2.0 的人跑 V1.0 也放行（没必要拦降级，拦了只会自找麻烦）。

导出次数的语义（license.export_limit，跟 expire_at 一样客户端只读）：
    -1 不限次数 / 0 禁止导出 / 正数 = 允许成功导出的总次数。
    已用次数不单独存（存了就得让客户端能 UPDATE，防线破了），
    而是直接数 export_log 里 success=1 的行数——数日志天然准确，改不了。
"""
import datetime
import hashlib
import hmac
import ipaddress
import os
import re
import socket
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import pymysql
from pymysql.cursors import DictCursor

from app_config import AUTH_DB
from auth import device

# PBKDF2 迭代次数：26 万轮，单次约 0.1 秒，正常登录感知不到，但把离线爆破成本抬起来
PBKDF2_ITER = 260000


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------
def get_conn(use_db=True):
    """建一个短连接（每次登录新建、退出即关，不做复用）

    use_db=False 用于建库阶段：那时 wechatexport 这个库还不存在，带着 database
    参数连会直接报 1049。
    """
    kwargs = dict(
        host=AUTH_DB.get('host', '127.0.0.1'),
        port=int(AUTH_DB.get('port', 3306)),
        user=AUTH_DB.get('user', 'root'),
        password=AUTH_DB.get('password', ''),
        charset=AUTH_DB.get('charset', 'utf8mb4'),
        connect_timeout=int(AUTH_DB.get('connect_timeout', 5)),
        # 登录日志要立刻落库，自动提交省掉手工 commit
        autocommit=True,
    )
    if use_db and AUTH_DB.get('database'):
        kwargs['database'] = AUTH_DB['database']
    return pymysql.connect(**kwargs)


def _friendly(exc):
    """把 pymysql 的错误码翻译成一句用户能看懂的话

    登录框上直接显示这句话，所以不能出现 traceback、不能只有错误码。
    """
    code = exc.args[0] if getattr(exc, 'args', None) else 0
    host = AUTH_DB.get('host', '127.0.0.1')
    port = AUTH_DB.get('port', 3306)
    db = AUTH_DB.get('database', 'wechatexport')
    if code == 2003:
        return '无法连接授权服务器 %s:%s，请确认数据库已启动' % (host, port)
    if code == 2013:
        return '连接授权服务器超时（%s:%s）' % (host, port)
    if code == 1045:
        return '授权服务器拒绝登录（程序内置的数据库账号或密码不对）'
    if code == 1049:
        return '授权数据库 %s 不存在，请先运行 init_license_db.py 初始化' % db
    if code == 1044:
        return '当前数据库账号没有访问 %s 的权限' % db
    if code == 1146:
        return '授权表 license 不存在，请先运行 init_license_db.py 初始化'
    return '授权服务器异常：%s' % (exc,)


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------
def hash_password(password, iterations=PBKDF2_ITER, salt=None):
    """明文 → pbkdf2_sha256$轮数$盐$哈希（建账号、改密码都走这里）"""
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', str(password).encode('utf-8'), salt, iterations)
    return 'pbkdf2_sha256$%d$%s$%s' % (iterations, salt.hex(), dk.hex())


def check_password(password, stored):
    """比对密码。格式不对、字段缺失一律当失败，不抛异常。"""
    try:
        algo, iters, salt_hex, hash_hex = str(stored).split('$')
        if algo != 'pbkdf2_sha256':
            return False
        dk = hashlib.pbkdf2_hmac('sha256', str(password).encode('utf-8'),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 登录日志
# ---------------------------------------------------------------------------
# 查公网出口 IP 的接口。都是「访问一下就返回你自己 IP」的公开服务，不需要 key。
# 全部在国内实测过：myip.ipip.net 0.40s、ifconfig.me 0.51s、api.ip.sb 0.83s。
# 注意 api.ipify.org / 4.ipw.cn / myip.ipip.net/s / ip.cn 在国内网络不通，实测已排除。
#
# 全部用 HTTPS，**不放 http 源**：明文响应会被运营商插广告、被中间人改写，
# 返回什么 IP 全凭对方，这一列就彻底失去意义了。ip.3322.net 虽然最快（0.26s）
# 但只有 http，因此弃用——快 0.1 秒不值得拿正确性去换。
# 这几个源都走标准证书校验（urllib 默认），被劫持会直接抛异常，不会静默采信。
_IP_SOURCES = (
    'https://myip.ipip.net',
    'https://ifconfig.me/ip',
    'https://api.ip.sb/ip',
)

# 单个源的超时。公网出口 IP 只是日志里的一列，不值得为它让用户盯着登录框等
_IP_TIMEOUT = 1.5

_IPV4_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')

# 进程内缓存：一次启动只查一次（登录失败重试时不必再打一遍网络）
_public_ip_cache = None


def _is_public(ip):
    """判断是不是「有意义的公网地址」

    排除内网、回环、链路本地、保留段——劫持页、错误页里常夹着 10.x / 192.168.x
    这种地址，混进来比留空更误导人。
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def _pick_ip(text):
    """从任意响应文本里抠出第一个公网地址（纯文本、JSON、带中文说明的都能用）"""
    text = (text or '').strip()
    for m in _IPV4_RE.finditer(text):
        if _is_public(m.group(0)):
            return m.group(0)
    # 纯文本源（ip.sb 之类）在纯 IPv6 出口下会直接返回 IPv6，整体再试一次
    if _is_public(text):
        return text
    return ''


def _fetch_ip(url):
    """问一个源：请求一次，返回抠出来的 IP（失败抛异常，由调用方丢弃）"""
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0',            # 有的接口对空 UA 直接 403
    })
    with urllib.request.urlopen(req, timeout=_IP_TIMEOUT) as resp:
        return _pick_ip(resp.read(512).decode('utf-8', 'ignore'))


def _public_ip():
    """取本机的**公网出口 IP**；取不到就返回空串

    为什么不用 `socket.gethostbyname()`：那只能拿到网卡上的内网地址（172.16.x.x、
    192.168.x.x），同一个办公室几十台机器全是同一个值，等于没记。

    为什么要问外部接口：公网出口 IP 只有对端才看得见，本机无从得知，只能让
    公网上的服务把"它看到的源地址"回给我们。

    为什么并发：串行问 4 个源，断网时最坏要等 4×1.5=6 秒，登录框就僵在那了。
    并发之后总耗时封顶在 1.5 秒左右，而且**第一个成功的就收工**，
    正常情况下 0.3 秒内返回。
    """
    global _public_ip_cache
    if _public_ip_cache is not None:        # 空串也算查过了，重试登录不必再等
        return _public_ip_cache

    ip = ''
    pool = ThreadPoolExecutor(max_workers=len(_IP_SOURCES))
    try:
        futures = [pool.submit(_fetch_ip, url) for url in _IP_SOURCES]
        for fut in as_completed(futures, timeout=_IP_TIMEOUT + 0.5):
            try:
                got = fut.result()
            except Exception:
                continue                     # 这个源挂了/超时，等下一个
            if got:
                ip = got
                break
    except Exception:
        pass                             # 全部超时：TimeoutError 也走这里
    finally:
        # 不等没回来的源：它们自己会在 1.5 秒内超时结束，没必要拖住登录
        pool.shutdown(wait=False)

    _public_ip_cache = ip
    return ip


def log_login(username, ok, message='', client_version=''):
    """往 login_log 插一行：谁、什么时候、从哪台机器（机器名 + IP）、什么版本、成功没有

    失败也记是故意的——连续几条「账号或密码错误」就是有人在试密码，
    只看成功记录是发现不了的。

    纯留痕用途，写失败（断网、服务器重启、账号没拿到 INSERT 权限）
    绝不能挡住已经通过的登录，所以整段吞异常。
    各字段按列宽截断，免得超长输入直接报 1406。
    """
    user = (username or '')[:64]
    msg = (message or '')[:128]
    ver = (client_version or '')[:32]
    host = socket.gethostname()[:64]
    ok_flag = 1 if ok else 0

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'INSERT INTO `login_log` '
                    '(`username`,`success`,`message`,`version`,`host`,`device_id`,`login_ip`) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s)',
                    (user, ok_flag, msg, ver, host,
                     device.device_id()[:64], _public_ip()[:45]))
        return
    except Exception:
        pass

    # 走到这里通常是「库还没跑 sql/2026-11-09_login_ip.sql」，报 1054 未知列。
    # 退一步用不带 IP 的写法再插一次：宁可这一列空着，也不要整条流水丢掉。
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'INSERT INTO `login_log` '
                    '(`username`,`success`,`message`,`version`,`host`) '
                    'VALUES (%s,%s,%s,%s,%s)',
                    (user, ok_flag, msg, ver, host))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 版本比较
# ---------------------------------------------------------------------------
def _ver_tuple(text):
    """'V1.0.0' / 'v1.2' / '1.0.0' → (1, 0, 0)；解析不出来返回 None

    逐段取前导数字，遇到非数字就停。这样 '1.0.0-beta' 也能正确读成 (1,0,0)。
    """
    s = str(text or '').strip().lstrip('Vv')
    if not s:
        return None
    parts = []
    for seg in s.split('.'):
        num = ''
        for ch in seg:
            if ch.isdigit():
                num += ch
            else:
                break
        if not num:
            break
        parts.append(int(num))
    return tuple(parts) if parts else None


def _ver_cmp(a, b):
    """比较两个版本元组，短的一方末尾补 0（(1,0) 与 (1,0,0) 视为相等）"""
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------
def login(username, password, client_version):
    """校验一次登录，返回 (ok, msg, info)

    ok=False 时 msg 就是直接显示在登录框上的中文；
    ok=True 时 info 带 username / version / expire_at / days_left。

    判定顺序是固定好的（先验身份、再验授权）：
        账号密码 → 是否停用 → 版本 → 有效期
    这样"密码错"的人拿不到「这个账号存在且还没过期」这类信息。

    每一条拒绝/放行的结论都会顺手写进 login_log（成功、失败都有记录）。
    """
    username = (username or '').strip()
    if not username or not password:
        # 手滑空提交，没有留痕价值，也不值得为它开一次数据库连接
        return False, '请输入账号和密码', {}

    def _done(ok, msg='', info=None):
        """统一出口：记一条登录日志，再返回三元组"""
        log_login(username, ok, msg, client_version)
        return ok, msg, info or {}

    try:
        with get_conn() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    'SELECT `id`, `username`, `password`, `version`, `expire_at`, `enabled` '
                    'FROM `license` WHERE `username` = %s', (username,))
                row = cur.fetchone()
    except pymysql.MySQLError as e:
        # 连不上/没权限时这条日志也写不进去，直接返回，免得再白等一次连接超时
        return False, _friendly(e), {}

    # 账号不存在和密码错误用同一句话：不告诉对方这个账号到底存不存在
    if not row or not check_password(password, row.get('password')):
        return _done(False, '账号或密码错误')

    if not int(row.get('enabled') or 0):
        return _done(False, '该账号已被停用，请联系管理员')

    allowed = str(row.get('version') or '*').strip()
    if allowed and allowed != '*':
        cv, av = _ver_tuple(client_version), _ver_tuple(allowed)
        if cv is None or av is None:
            return _done(False, '授权版本号格式异常（%s），请联系管理员' % allowed)
        if _ver_cmp(cv, av) > 0:
            return _done(False, '当前版本 %s 超出授权范围（授权版本 %s），请联系管理员' % (
                client_version, allowed))

    expire = row.get('expire_at')
    if expire is None:
        return _done(False, '该账号没有设置有效期，请联系管理员')

    now = datetime.datetime.now()
    if now > expire:
        return _done(False, '授权已于 %s 到期，请联系管理员' % expire.strftime('%Y-%m-%d'))

    return _done(True, '', {
        'id': row.get('id'),
        'username': row.get('username'),
        'version': allowed,
        'expire_at': expire,
        'days_left': (expire.date() - now.date()).days,
    })


# ---------------------------------------------------------------------------
# 导出：次数校验 + 导出日志
# ---------------------------------------------------------------------------
def check_export_limit(username):
    """导出前的次数校验，返回 (ok, msg, left)

    ok=False 时 msg 直接弹给用户；ok=True 时 left 是剩余次数（-1 表示不限）。

    语义（license.export_limit，客户端只读）：
        -1  不限次数，直接放行
         0  禁止导出
        >0  允许成功导出这么多次

    已用次数 = export_log 里该账号 success=1 的行数（失败的导出不算）。
    为什么不把"剩余次数"存在 license 里让客户端扣减：那需要给客户端
    UPDATE 权限，解包的人一条 SQL 就能给自己加次数——数日志改不了，
    这道防线跟 expire_at 一样是只读的。

    连不上库时返回 False：次数是商业控制点，连不上就放行等于没有。
    （正常也到不了这一步——登录都过不了。）
    """
    username = (username or '').strip()
    try:
        with get_conn() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute('SELECT `export_limit` FROM `license` '
                            'WHERE `username` = %s', (username,))
                row = cur.fetchone()
                if not row:
                    return False, '账号不存在或已被删除，请重新登录', 0
                limit = int(row.get('export_limit') or 0)

                if limit < 0:
                    return True, '', -1
                if limit == 0:
                    return False, '该账号不允许导出，请联系管理员', 0

                cur.execute('SELECT COUNT(*) AS n FROM `export_log` '
                            'WHERE `username` = %s AND `success` = 1',
                            (username,))
                used = int((cur.fetchone() or {}).get('n') or 0)
                if used >= limit:
                    return False, ('导出次数已用完（限 %d 次，已成功 %d 次），'
                                   '请联系管理员增加次数' % (limit, used)), 0
                return True, '', limit - used
    except pymysql.MySQLError as e:
        return False, _friendly(e), 0


def log_export(username, ok, message='', client_version=''):
    """往 export_log 插一行：谁、哪台设备、哪个 IP、成没成、什么时候

    ok=True 时 message 写导出概要（几个会话、文件名）；
    ok=False 时 message 写失败原因——跟 login_log 一样，失败也记，
    排查「导出总报错」的用户问题全靠这些行。

    纯留痕：写失败（断网、表没建）吞掉，绝不挡导出本身。
    device_id / 公网 IP 都是进程内缓存的，这里不产生新的网络开销。
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'INSERT INTO `export_log` '
                    '(`username`,`device_id`,`export_ip`,`success`,`message`,`version`) '
                    'VALUES (%s,%s,%s,%s,%s,%s)',
                    ((username or '')[:64],
                     device.device_id()[:64],
                     _public_ip()[:45],
                     1 if ok else 0,
                     (message or '')[:255],
                     (client_version or '')[:32]))
    except Exception:
        pass
