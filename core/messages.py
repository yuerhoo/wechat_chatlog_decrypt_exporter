# -*- coding: utf-8 -*-
"""消息层：跨分库扫描消息、解压消息正文、解析发送人、统计条数

微信 4.x（4.0 / 4.1 / 4.2 结构一致）的消息分布规则（本文件全部逻辑都建立在这几条之上）：
    1) 每个会话一张表，表名 = 'Msg_' + md5(会话 username) 的十六进制；
       微信不提供 md5() 函数，所以表名在 Python 里先算好再拼进 SQL。
    2) 表分散在多个库里：message/message_0.db、message_1.db …（公众号在 biz_message_*.db），
       同一个会话的表只存在于其中一个库，所以要逐个库查 sqlite_master 找。
    3) 时间字段 create_time 是秒级 epoch；sort_seq 是毫秒级序号（同秒内排序用）。
    4) 消息类型 local_type 是"高位带标志位"的整数，必须 & 0xFFFFFFFF 才是真实类型。
    5) 正文 message_content 有两种形态：纯文本；或 WCDB_CT_message_content=4 时
       为 zstandard 压缩后的 blob，需要先解压再按 utf-8 解码。
    6) 群消息正文常常以 "<发送人wxid>:\n" 开头，去掉这段才是真正内容；
       发送人优先用 real_sender_id → Name2Id.rowid → user_name 解析。
"""
import hashlib
import os
import re
import sqlite3

from .contacts import ro_conn

try:
    import zstandard
    _zstd = zstandard.ZstdDecompressor()
except Exception:
    _zstd = None        # 没装 zstandard 时退化为直接 utf-8 解码（压缩消息会变乱码）

# local_type → 中文类型名
TYPE_NAMES = {1: '文本', 3: '图片', 34: '语音', 43: '视频', 47: '表情',
              48: '位置', 49: '文件/链接', 10000: '系统提示'}
# 导出弹窗里的 8 个复选框（顺序即界面顺序）
EXPORT_TYPES = [('文本', 1), ('图片', 3), ('语音', 34), ('视频', 43),
                ('表情', 47), ('文件/链接', 49), ('位置', 48), ('系统提示', 10000)]

MASK = 0xFFFFFFFF                                    # 32 位掩码，清掉高位标志
SENDER_RE = re.compile(r'^([A-Za-z0-9_\-@.]{3,64}):\n')   # 群消息的"发送人:"前缀
TITLE_RE = re.compile(r'<title>(.*?)(?:</title>|$)', re.S)  # 分享卡片/链接标题

TYPE_MASK = 4294967295                               # 与 MASK 相同，SQL 里拼字面量用


def norm_wxid(w):
    """把账号目录后缀去掉：wxid_xxx_c779 / wxid_xxx → wxid_xxx

    账号文件夹名（self_wxid）常带一段 4~6 位的账号后缀，而消息表 Name2Id
    里存的是不带后缀的 wxid。判定"是不是我发的"必须先把两者归一到同一形态。
    """
    s = (w or '').strip().lower()
    if not s:
        return ''
    head, sep, tail = s.rpartition('_')
    if sep and head.startswith('wxid_') and 1 <= len(tail) <= 8 and tail.isalnum():
        return head
    return s


def is_self_wxid(sender, self_wxid):
    """发送人 wxid 是否就是本机登录账号（兼容带/不带账号目录后缀）"""
    if not sender or not self_wxid:
        return False
    a, b = norm_wxid(sender), norm_wxid(self_wxid)
    if not a or not b:
        return False
    return a == b or a.startswith(b + '_') or b.startswith(a + '_')


def msg_table(username):
    """会话对应的消息表名。SQLite 没有 md5()，所以在 Python 侧算好。"""
    return 'Msg_' + hashlib.md5(username.encode()).hexdigest()


def list_dbs(dec_dir, gh=False):
    """列出消息分库文件（已按文件名排序）

    gh=True 时找公众号的 biz_message_*.db，否则找普通消息的 message_*.db。
    只接受"前缀 + 纯数字 + .db"的文件名，避免把备份文件之类误当成分库。
    """
    d = os.path.join(dec_dir, 'message')
    if not os.path.isdir(d):
        return []
    pref = 'biz_message_' if gh else 'message_'
    out = []
    for fn in os.listdir(d):
        if fn.startswith(pref) and fn.endswith('.db') and fn[len(pref):-3].isdigit():
            out.append(os.path.join(d, fn))
    return sorted(out)


def table_dbs(dec_dir, username):
    """定位某个会话的消息表落在哪些分库里，返回 [(库路径, 表名)]

    公众号的会话 id 以 gh_ 开头，表在 biz_message_*.db 里；
    其余会话在 message_*.db 里。
    """
    t = msg_table(username)
    out = []
    for db in list_dbs(dec_dir, gh=username.startswith('gh_')):
        try:
            c = ro_conn(db)
            hit = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
            c.close()
        except Exception:
            continue
        if hit:
            out.append((db, t))
    return out


def count_msgs(dec_dir, username):
    """统计某会话的消息总数（列表里"消息数"列的来源）"""
    n = 0
    for db, t in table_dbs(dec_dir, username):
        try:
            c = ro_conn(db)
            n += c.execute('SELECT COUNT(*) FROM [%s]' % t).fetchone()[0]
            c.close()
        except Exception:
            pass
    return n


def decode_content(raw, ct):
    """把 message_content 变成可读字符串

    ct (WCDB_CT_message_content) == 4 表示正文是 zstandard 压缩的 blob：
    微信为了省空间会把长文本压缩，直接 decode 只会得到乱码。
    先试一次性 decompress，失败再试流式解压（两者对应不同的封装格式）。
    """
    if raw is None:
        return ''
    if isinstance(raw, (bytes, bytearray)):
        if ct == 4 and _zstd is not None:
            for method in ('decompress', 'stream'):
                try:
                    if method == 'decompress':
                        return _zstd.decompress(bytes(raw)).decode('utf-8', 'replace')
                    return _zstd.decompressobj().decompress(bytes(raw)).decode('utf-8', 'replace')
                except Exception:
                    continue
        try:
            return bytes(raw).decode('utf-8', 'replace')
        except Exception:
            return repr(bytes(raw)[:60])
    return str(raw)


def content_brief(type_code, content):
    """XML 类消息（分享卡片、链接、小程序…）只保留 <title>，否则表格里全是标签"""
    if content.startswith('<'):
        m = TITLE_RE.search(content)
        if m and m.group(1).strip():
            return m.group(1).strip()[:80]
        return '[%s]' % TYPE_NAMES.get(type_code, '多媒体消息')
    return content


_PACKED_KEY_RE = re.compile(rb'([0-9a-fA-F]{32})')


def media_key_of(blob):
    """从 packed_info_data 里取「本地媒体文件名」的 32 位 hash

    微信 4.x 把媒体文件落盘用的 hash 放在消息表的 packed_info_data
    （protobuf blob，形如 08 01 10 02 1a 22 22 20 <32 位 ascii hex> 58 00）。
    message_content 的 XML 里只有 CDN 侧的 md5 / aeskey，和磁盘文件名完全对不上：
    实测 12906 条媒体 XML，各属性 hash 与 attach/video 文件名的命中率均为 0，
    而 packed_info_data 的 hash 对图片命中 4136/4144（99.8%），且精确落在
    msg/attach/<md5(会话username)>/<YYYY-MM>/Img/<hash>*.dat 下。
    """
    if not isinstance(blob, (bytes, bytearray)):
        return ''
    m = _PACKED_KEY_RE.search(bytes(blob))
    return m.group(1).decode().lower() if m else ''


def scan_chat(dec_dir, sess, data, t0=None, t1=None, types=None, keyword=None,
              cancel=None, with_raw=False, self_wxid=''):
    """扫描一个会话，按时间升序返回消息行

    返回 [(sort_seq, create_time, 发送人wxid, 发送人显示名, 类型码, 类型名, 内容)]
    当 with_raw=True 时，每行末尾再追加两个元素：
        索引 7 = 解码后的原始内容（XML 类消息即未裁标题的原文）
        索引 8 = packed_info_data 里的本地媒体 hash（没有则空串）
    导出侧用 r[1]/r[3]/r[4] 等位置索引，且不用 with_raw，追加元素不影响它。

    参数：
        t0/t1     时间范围（epoch 秒，含边界）；None 表示不限
        types     消息类型集合；None 表示全部
        keyword   内容关键词（不区分大小写），命中内容或摘要即保留
        cancel    回调，返回 True 时立刻放弃并返回 None（用户取消导出）
        with_raw  为 True 时每行追加原始内容(7)与本地媒体 hash(8)
        self_wxid 本机登录账号 wxid；给了才能正确把群聊里"自己发的消息"
                  标成 (自己)。不传时保持旧行为（导出侧无需本参数）。
    """
    username = sess.username
    is_group = username.endswith('@chatroom')
    # 联系人映射表，用于把 wxid 翻成备注/昵称
    remark_of, nick_of = {}, {}
    for u, (rm, nk) in (data.contacts_map or {}).items():
        remark_of[u] = rm
        nick_of[u] = nk
    # 本群的"群昵称"表（只有设置过群昵称的成员才在里面）
    group_nick = (data.group_nicks or {}).get(username, {})
    peer_name = sess.display

    rows = []
    where = []
    args = []
    if t0 is not None:
        where.append('create_time >= ?'); args.append(int(t0))
    if t1 is not None:
        where.append('create_time <= ?'); args.append(int(t1))
    if types:
        # local_type 高位带标志，先 & 掩码再比对；参数直接内联（都是 int，无注入风险）
        where.append('(local_type & %d) IN (%s)' % (TYPE_MASK, ','.join(str(int(t)) for t in types)))
    wsql = (' WHERE ' + ' AND '.join(where)) if where else ''

    for db, t in table_dbs(dec_dir, username):
        if cancel is not None and cancel():
            return None
        try:
            c = ro_conn(db)
        except Exception:
            continue
        try:
            # Name2Id 把 real_sender_id 映射到 user_name（这个库内自增，不能跨库用）
            n2i = {}
            try:
                for rid, un in c.execute('SELECT rowid, user_name FROM Name2Id'):
                    n2i[rid] = un
            except Exception:
                pass

            # 对话框要读本地媒体文件名 hash；旧版本消息表可能没有这一列
            packed_col = False
            if with_raw:
                try:
                    packed_col = any(r[1] == 'packed_info_data'
                                     for r in c.execute('PRAGMA table_info([%s])' % t))
                except Exception:
                    packed_col = False
            sel = ('SELECT create_time, sort_seq, real_sender_id, local_type, '
                   'message_content, WCDB_CT_message_content'
                   + (', packed_info_data' if packed_col else '')
                   + ' FROM [%s]%s' % (t, wsql))
            for rec in c.execute(sel, args):
                ts, sseq, rid, lt, raw, ct = rec[:6]
                packed = rec[6] if packed_col else None
                if ts is None:
                    continue
                type_code = (lt or 0) & MASK
                content = decode_content(raw, ct)

                # 发送人：优先 real_sender_id → Name2Id.rowid；失败再解析内容前缀
                sender = n2i.get(rid) if rid else None
                m = SENDER_RE.match(content)
                if sender is None and m:
                    sender = m.group(1)
                if m:
                    content = content[m.end():]      # 去掉"发送人:\n"前缀

                # 展示名：系统消息固定"系统"；群聊按 群昵称 > 备注 > 昵称 > wxid；
                # 单聊里发送人等于对方就是对方，否则是自己。
                # ⚠️ 4.x 消息表 Name2Id 里**也有自己**：real_sender_id=2 → 自己的
                # wxid。以前只有 sender 为空才判成 '(自己)'，导致群里自己发的消息
                # 被当成别人发的、头像跑到左边。这里统一按 self_wxid 归一比对。
                mine = is_self_wxid(sender, self_wxid)
                if type_code == 10000:
                    sname = '系统'
                elif is_group:
                    if not sender or mine:
                        sname = '(自己)'
                    else:
                        sname = (group_nick.get(sender)
                                 or remark_of.get(sender)
                                 or nick_of.get(sender)
                                 or sender)
                else:
                    sname = (peer_name
                             if (sender and sender == username and not mine)
                             else '(自己)')

                brief = content_brief(type_code, content)
                if keyword and keyword not in brief and keyword not in content:
                    continue
                row = (sseq or 0, ts, sender or '', sname, type_code,
                       TYPE_NAMES.get(type_code, '类型%d' % type_code), brief)
                if with_raw:
                    row = row + (content, media_key_of(packed))
                rows.append(row)
        except Exception:
            # 单个分库异常（库损坏/正在被写）不影响其他分库的结果
            pass
        finally:
            c.close()

    # 先按时间、再按 sort_seq 排，保证同一秒内的多条消息顺序正确
    rows.sort(key=lambda r: (r[1], r[0]))
    return rows
