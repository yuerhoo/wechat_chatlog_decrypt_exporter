# -*- coding: utf-8 -*-
"""微信本地媒体（图片 / 视频 / 语音 / 文件 / 表情）解密库

== 为什么要单独做这一层 ==
解密数据库（decrypt_db.py）只能拿到消息"记录"，真实的多媒体文件并不在数据库里。
微信把图片 / 视频 / 语音 / 文件按原始字节加密后存成 .dat 文件，数据库里只留一条
XML（缩略图 md5、aeskey、cdn 地址等元信息）。本模块负责把 .dat 还原成真实文件。

== 两套加密体系（务必分清）==
    · 3.x 经典版（数据在 <wxid>/FileStorage/Image|Video|File|...，Msg/*.db）
      图片 .dat 是"单字节 XOR"加密，XOR key 由文件头/尾对照已知 magic 反推，不需要任何密钥。
      语音是 silk/amr，文件一般直接存（不加密或仅 XOR）。
    · 4.x（数据在 <wxid>/db_storage 与 <wxid>/msg/attach/<md5(username)>/<YYYY-MM>/...）
      图片 .dat 是 V2 格式：AES-128-ECB + XOR 混合（见下方 V2 结构）。
      V2 的 AES key 是一个 16 字节 ASCII 字符串，只在"你正在看这张图"的瞬间短暂存在于
      微信进程内存——所以要提取它，必须微信在运行 + 本进程有管理员权限去读进程内存
      （复用我们取库密钥那套 VirtualQueryEx/ReadProcessMemory 思路）。
      不要求微信运行、也不需要密钥的情形：3.x XOR 图片、4.x V1（固定 key）、4.x 已缓存了 key。

== V2 .dat 文件结构（4.x 图片 / 视频 / 表情 / 文件通用同一个外壳）==
    [6B 签名: 07 08 V2 08 07] [4B aes_size LE] [4B xor_size LE] [1B padding]
    [aligned_aes_size 字节 AES-ECB 密文] [raw_data 明文] [xor_size 字节 XOR 密文]
    解密 = AES-ECB(key, 去 PKCS7 填充) 拼上 raw_data 再拼上 XOR 段；最后按文件头 magic 判定真实格式。
    V1 签名是 07 08 V1 08 07，AES key 固定为 md5("0")[:16] = 'cfcd208495d565ef'，其余与 V2 相同。

    ⚠️ 4.2 实测更正：头 8 字节 07 08 56 32 08 07 00 04 并不是"新的 8 字节签名"，
    而是上述经典头里 aes_size 恰好 = 1024（字节 6~9 = 00 04 00 00）、
    字节 10~13 = xor_size。判据：15 + aligned(aes_size) + xor_size == len(data)，
    且 aes_size + xor_size == 图片 XML 的 length。首块 AES 密文在偏移 15（不是 31）。
    decode_dat_bytes() 按这份自洽布局走 decrypt_v2；decrypt_v2_4x() 只作兜底。

== 真实格式判定 ==
解密后的字节按 magic 识别：jpg / png / gif / webp / hevc(微信视频裸流 wxgf) /
tif / bmp / mp4 / mov / silk(#!SILK / SILK_V3) / amr / zip / rar / 7z / pdf …
语音（silk）要能播放还需 silk→pcm→wav 转码，本模块只负责解到 .silk/.amr，**不转码**。

== 与消息的关联（4.x）==
    message_*.db 的某条消息 → local_id → message_resource.db 的 MessageResourceInfo.packed_info
    （protobuf，抠出 32 字符 hex MD5）→ attach/<md5(username)>/<YYYY-MM>/<类型>/<md5>[_t|_h].dat
    本模块提供 MediaResolver4x 封装这条链；3.x 走 MediaResolver3x（路径直查）。

本模块刻意不依赖 PyQt / 不弹界面，纯函数 + 一个可选的内存扫描函数（仅 Windows 用 ctypes）。
"""
import hashlib
import os
import re
import struct

try:
    from Crypto.Cipher import AES
    from Crypto.Util import Padding
    _HAS_CRYPTO = True
except Exception:  # pragma: no cover - 依赖缺失时降级为"只支持 3.x XOR"
    _HAS_CRYPTO = False


# ---------------------------------------------------------------------------
# 1. 文件签名 / 魔数
# ---------------------------------------------------------------------------
V2_MAGIC_FULL = b'\x07\x08V2\x08\x07'      # 旧版 4.x V2 完整 6 字节签名
V1_MAGIC_FULL = b'\x07\x08V1\x08\x07'      # 旧版 4.x V1 完整 6 字节签名（固定 AES key）
V2_MAGIC_FULL_4X = b'\x07\x08V2\x08\x07\x00\x04'   # 微信 4.2 实测：8 字节签名（尾巴是 00 04）
V2_MAGIC_HEAD = b'\x07\x08\x56\x32'        # 前 4 字节，快速判定
V1_MAGIC_HEAD = b'\x07\x08\x56\x31'
# 任一 V2 签名都算"需要按账号 AES key"的加密容器
V2_SIGNATURES = (V2_MAGIC_FULL, V2_MAGIC_FULL_4X)
# V2 尾巴 XOR 段的单字节 key 候选（按账号一个值）：4.x 实测 0x4A，3.x 经典 0x88，
# 4.2 内存里的 _W 图是 0x0E。decrypt_v2 会按"文件结尾是否正确"自动挑。
V2_XOR_KEYS = (0x4A, 0x88, 0x0E)
# 4.x V1 固定 AES-128 key = md5("0") 的前 16 字节
V1_AES_KEY = hashlib.md5(b'0').hexdigest()[:16].encode('ascii')

# 常见图片 magic（按长度降序排，短 magic 容易假阳性，放后面再兜底）
IMAGE_MAGIC = {
    'tif':  [0x49, 0x49, 0x2A, 0x00],      # little-endian TIFF
    'png':  [0x89, 0x50, 0x4E, 0x47],
    'gif':  [0x47, 0x49, 0x46, 0x38],
    'webp': [0x52, 0x49, 0x46, 0x46],      # RIFF
    'jpg':  [0xFF, 0xD8, 0xFF],
    # BMP 只有 2 字节 magic，需要额外校验（见 detect_format）
}

# 解密后字节 → 扩展名 的"宽"判定（覆盖所有媒体类型，不只是图片）
def detect_format(head):
    """根据解密后的前若干字节判断真实格式，返回扩展名（小写，不含点）

    返回 'bin' 表示认不出（多半是 key 不对 / 文件损坏 / 不是微信 .dat）。
    """
    if len(head) < 16:
        return 'bin'
    # —— 图片 ——
    if head[:3] == b'\xFF\xD8\xFF':
        return 'jpg'
    if head[:4] == b'\x89PNG':
        return 'png'
    if head[:3] == b'GIF':
        return 'gif'
    if head[:2] == b'BM':
        return 'bmp'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    if head[:4] in (b'II*\x00', b'MM\x00*'):      # TIFF
        return 'tif'
    if head[:4] == b'wxgf':                         # 微信 HEVC 图片裸流（Qt 解不了）
        return 'hevc'
    # —— 视频 ——
    if b'ftyp' in head[:16]:                         # MP4 / MOV 的 ftyp box
        # 进一步区分 mp4 / mov / m4a，扩展名都用 mp4 也能播
        return 'mp4'
    # —— 语音 ——
    if head[:6] in (b'SILK_V', b'#!SILK'):           # 微信语音 silk 编码
        return 'silk'
    if head[:4] in (b'AMR\x01', b'#!AM') or head[:3] == b'AMR':   # AMR
        return 'amr'
    # —— 文件（压缩包 / 文档）——
    if head[:4] == b'PK\x03\x04':                    # ZIP / DOCX / XLSX / APK …
        return 'zip'                                  # 解到 zip 后由真实后缀决定，先用 zip
    if head[:4] == b'Rar!':
        return 'rar'
    if head[:6] == b'7z\xBC\xAF\x27\x1C':
        return '7z'
    if head[:5] == b'%PDF-':
        return 'pdf'
    if head[:4] == b'\x25PDF' or (head[:2] == b'\x25\x25'):  # 某些变体
        return 'pdf'
    # BMP 兜底：文件头是 'BM' 时上面已返回；这里留给更短的情况
    return 'bin'


# ---------------------------------------------------------------------------
# 2. XOR key 检测（3.x 图片 / 4.x V2 XOR 段）
# ---------------------------------------------------------------------------
def detect_xor_key(data):
    """从一段 .dat 原始字节反推单字节 XOR key（3.x 图片用）

    原理：明文首字节必然命中某个已知图片 magic，XOR key = 密文首字节 ^ magic[0]，
    再逐个字节校验，全部命中即确认。返回 int 或 None。
    V2 文件（头部是 07 08 V2）无法用这个办法，返回 None（V2 的 XOR key 见 find_xor_key_4x）。
    """
    head = data[:16]
    if len(head) < 4 or head[:4] == V2_MAGIC_HEAD or head[:4] == V1_MAGIC_HEAD:
        return None
    for fmt, magic in IMAGE_MAGIC.items():
        if len(head) < len(magic):
            continue
        key = head[0] ^ magic[0]
        if all((head[i] ^ key) == magic[i] for i in range(1, len(magic))):
            return key
    return None


def find_xor_key_4x(attach_dir):
    """从 4.x 一堆 V2 缩略图尾部推导出 XOR key

    V2 图片的明文尾是 JPEG 的 FF D9（缩略图几乎都是 jpg），所以
    XOR key = 尾字节 ^ 0xFF（且次尾字节 ^ 0xD9 应一致）。扫若干张取多数票。
    非图片媒体（视频/语音/文件）V2 段多半不是 jpg 结尾，这个函数会失效——
    那时用默认 0x88（微信常用值）或直接传 xor_key=None 让调用方处理。
    """
    pattern = os.path.join(attach_dir, '*', '*', 'Img', '*_t.dat')
    files = sorted(_glob(pattern), key=_mtime, reverse=True)
    counts = {}
    for f in files[:32]:
        try:
            sz = os.path.getsize(f)
            with open(f, 'rb') as fp:
                if fp.read(6) != V2_MAGIC_FULL or sz < 2:
                    continue
                fp.seek(sz - 2)
                tail = fp.read(2)
            if len(tail) == 2:
                counts[(tail[0] ^ 0xFF)] = counts.get(tail[0] ^ 0xFF, 0) + 1
        except OSError:
            continue
    if not counts:
        return None
    return max(counts, key=counts.get)


# ---------------------------------------------------------------------------
# 3. 三种外套的解密
# ---------------------------------------------------------------------------
_XOR_TABLES = {}


def _xor_table(key):
    """key -> 256 字节翻译表（translate 比逐字节 Python 循环快 20 倍以上）"""
    t = _XOR_TABLES.get(key)
    if t is None:
        t = bytes(i ^ key for i in range(256))
        _XOR_TABLES[key] = t
    return t


def _xor(data, key):
    """对整个字节串做单字节 XOR（key 为 int）"""
    if not key:
        return data
    return data.translate(_xor_table(key))


def _tail_closed(data, fmt):
    """尾巴是不是该格式应有的结束标记——用来反证 XOR key 猜对了

    AES 段解开后文件头必然是对 magic（跟 XOR key 无关），所以只能靠文件
    「结尾」来判断尾巴的 key：JPEG 必是 FF D9、PNG 必是 IEND 的 CRC。
    """
    if fmt == 'jpg':
        return data.endswith(b'\xff\xd9')
    if fmt == 'png':
        return data.endswith(b'\xaeB`\x82')
    if fmt == 'gif':
        return data.endswith(b';')
    return True      # hevc / mp4 / silk 等没有统一结尾，不做判断


def decrypt_xor(data):
    """3.x 图片 .dat：单字节 XOR 解密，自动探测 key

    返回 (明文 bytes, 扩展名) 或 (None, None)。XOR 加密不用密钥，所以 3.x 图片离线即可解。
    """
    key = detect_xor_key(data)
    if key is None:
        return None, None
    plain = _xor(data, key)
    return plain, detect_format(plain[:16])


def decrypt_v2(data, aes_key, xor_key=None):
    """4.x V1/V2 .dat 外壳解密（AES-ECB + XOR 混合），返回 (明文 bytes, 扩展名)

    aes_key：16 字节 ASCII（V2 从内存提；V1 固定为 V1_AES_KEY）。
    xor_key：单字节，默认 0x88；图片可先用 find_xor_key_4x 推出。
    返回 (None, None) 表示签名不符或 key 不对 / 缺 pycryptodome。
    """
    if not _HAS_CRYPTO or len(data) < 15:
        return None, None
    sig = data[:6]
    if sig == V1_MAGIC_FULL:
        aes_key = V1_AES_KEY
    elif sig != V2_MAGIC_FULL:
        return None, None
    # AES key 统一成 16 字节：调用方可能传 str（内存扫出来的）或 bytes。
    # 用 latin-1 而不是 ascii：内存里抓到的 key 可能是任意字节（0x80~0xFF），
    # 走 ascii 会直接 UnicodeEncodeError；latin-1 是字节保真的。
    if isinstance(aes_key, str):
        aes_key = aes_key.encode('latin-1', 'ignore')[:16]
    elif isinstance(aes_key, (bytes, bytearray)):
        aes_key = bytes(aes_key)[:16]
    else:
        return None, None

    aes_size, xor_size = struct.unpack_from('<LL', data, 6)
    # AES 密文段长度 = PKCS7 填充后的长度。明文长 aes_size 时，密文长恒为
    # (aes_size // 16 + 1) * 16（PKCS7 哪怕明文已满块也会再补一个整块）。
    # 注意：旧版 decode_image.py 用 "aes_size - ~(aes_size%16)"（向下取整到 16），
    # 在 aes_size 恰为 16 整数倍时会少算 16 字节导致解密错位——这里改成向上取整，覆盖全部情形。
    aligned = (aes_size // 16 + 1) * 16

    offset = 15
    if offset + aligned > len(data):
        return None, None
    try:
        cipher = AES.new(aes_key[:16], AES.MODE_ECB)
        dec_aes = Padding.unpad(cipher.decrypt(data[offset:offset + aligned]), AES.block_size)
    except (ValueError, KeyError):
        return None, None
    offset += aligned

    raw_end = len(data) - xor_size
    if raw_end < offset:
        raw_end = offset          # xor_size 写得比实际尾长时不越界
    raw = data[offset:raw_end]
    xor_part = data[raw_end:]
    fmt = detect_format(dec_aes[:16])

    # XOR 段的单字节 key 是**按账号**的一个值，实测 4.x 是 0x4A（3.x 才是 0x88，
    # 内存图的 _W 又是 0x0E）。写死任何一个都会让另一批图整屏空白，所以这里
    # 用「文件应有结尾」反证：先用末 16 字节试，命中再整段 XOR（大图省一次全量拷贝）。
    cands = ([xor_key] if xor_key is not None else []) + \
            [k for k in V2_XOR_KEYS if k != xor_key]
    pick = cands[0]
    tail16 = xor_part[-16:]
    for k in cands:
        if _tail_closed(_xor(tail16, k), fmt):
            pick = k
            break
    dec = dec_aes + raw + _xor(xor_part, pick)

    return dec, fmt


def decrypt_v2_4x(raw, aes_key, iv=None):
    """微信 4.2 V2 容器解密（8 字节签名 07 08 56 32 08 07 00 04）

    实测布局（来自对真实 4.2 样本的逐字节分析）：
        sig(8) + 2字节 + L(uint16 LE @10) + 3字节 + field16(16 @15) + payload(@31)
    payload 用"按账号"的 16 字节 AES key + **流密码**（CTR/CFB/OFB/CBC）整段加密，
    field16 极可能是 IV；与旧 4.x 的"AES-ECB 块 + raw + XOR"混合外壳完全不同。
    key 只在微信进程内存里，磁盘无落地，需要从内存提取（find_v2_key_4x.py）。

    返回 (明文 bytes, 扩展名) 或 (None, None)。多个模式/IV 试解，命中已知 magic 即返回。
    L 视为明文长度，若 0 < L <= payload 长度则截断（流密码无填充）。
    """
    if not _HAS_CRYPTO or len(raw) < 31:
        return None, None
    if isinstance(aes_key, str):
        aes_key = aes_key.encode('latin-1', 'ignore')[:16]
    elif isinstance(aes_key, (bytes, bytearray)):
        aes_key = bytes(aes_key)[:16]
    else:
        return None, None

    L = struct.unpack('<H', raw[10:12])[0]
    field16 = raw[15:31]
    payload = raw[31:]
    if 0 < L <= len(payload):
        payload = payload[:L]

    from Crypto.Cipher import AES
    head_magics = (b'\xff\xd8\xff', b'\x89PNG', b'GIF8', b'RIFF', b'\x00\x00\x00\x18ftyp')
    def ok(b):
        return any(b[:len(m)] == m for m in head_magics)

    # 候选 (mode, cipher)：IV 依次试 field16 / 全 0 / payload 前 16；再加 ECB（首块）
    cands = []
    for candidate_iv in (iv or field16, b'\x00' * 16, payload[:16] if payload else b'\x00' * 16):
        try:
            cands.append(('CBC', AES.new(aes_key, AES.MODE_CBC, candidate_iv)))
            cands.append(('CFB', AES.new(aes_key, AES.MODE_CFB, candidate_iv)))
            cands.append(('OFB', AES.new(aes_key, AES.MODE_OFB, candidate_iv)))
        except Exception:
            pass
        try:
            cands.append(('CTR', AES.new(aes_key, AES.MODE_CTR, nonce=candidate_iv[:8],
                                         initial_value=int.from_bytes(candidate_iv[8:16], 'big'))))
        except Exception:
            pass
    if len(payload) % 16 == 0:
        try:
            cands.append(('ECB', AES.new(aes_key, AES.MODE_ECB)))
        except Exception:
            pass

    for name, cipher in cands:
        try:
            dec = cipher.decrypt(payload)
        except Exception:
            continue
        if ok(dec):
            return dec, detect_format(dec[:16])
    return None, None


# 微信 4.x / 3.x 中"本来就以真实扩展名明文落盘"的媒体类型（无需解密，直接归档）
KNOWN_CLEAR_EXT = {
    '.mp4', '.m4v', '.mov', '.mp3', '.wav', '.wma', '.ogg', '.m4a', '.amr', '.silk',
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tif', '.tiff', '.heic',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.txt', '.md', '.csv', '.json', '.xml', '.log',
    '.zip', '.rar', '.7z', '.tar', '.gz', '.apk', '.ipa', '.exe', '.dll',
    '.slx', '.torrent',
}


def classify_media_path(path):
    """判断一个媒体文件是否"本来就是明文"（按扩展名）

    返回 True 表示无需解密（视频/文件/语音等真实扩展名文件），False 表示需要进一步判定
    （.dat 或无扩展名，可能是加密图片）。
    """
    return os.path.splitext(path)[1].lower() in KNOWN_CLEAR_EXT


def decode_dat_bytes(data, aes_key=None, xor_key=None):
    """统一入口：自动判定 V2 / V1 / 3.x XOR，返回 (明文 bytes, 扩展名)

    aes_key：4.x V2 的 16 字节 key（V1 不需要）；传 None 时 V2 直接失败返回 (None,None)。
    xor_key：4.x V2 尾巴 XOR 段的单字节 key；None（推荐）时由 decrypt_v2 按文件
             结尾自动选（4.x 0x4A / 3.x 0x88 / 内存图 0x0E）。

    判定顺序：
        1) 头部命中任一 V2/V1 签名 → AES 路径（V2 没 key 直接失败）。
        2) 头部本身就是已知图片/压缩包 magic → 明文（免密钥，如 4.2 的 _t_NW）。
        3) 否则 → 3.x 风格单字节 XOR（免密钥，如 4.2 的 _W / _t_W）。
    注意：微信 4.2 的文件"后缀（_W/_t_W/_t_NW）不可信"，必须按内容魔数判定。
    """
    if len(data) >= 8 and data[:8] in V2_SIGNATURES:
        # ⚠️ 4.2 那个「8 字节签名 07 08 56 32 08 07 00 04」其实不是新格式：
        # 它恰好是经典 V2 头 = sig6 + aes_size(u32 LE) + xor_size(u32 LE)，
        # 其中 aes_size 恒为 1024（字节 6~9 = 00 04 00 00），bytes10~13 是
        # XOR 尾长度。实测：15 + aligned(1024) + xor_size == 文件长度，且
        # 1024 + xor_size 正好等于 XML 里的 length/hdlength。
        # 所以首块 AES 密文在偏移 15，不是 31；旧版从 31 起试 key 全部落空。
        if data[:8] == V2_MAGIC_FULL_4X and len(data) >= 15:
            sz_aes, sz_xor = struct.unpack_from('<LL', data, 6)
            aligned = (sz_aes // 16 + 1) * 16
            classic = (0 < sz_aes <= len(data) and 0 < sz_xor <= len(data)
                       and 15 + aligned + sz_xor <= len(data))
            if classic:
                if not aes_key:
                    return None, None
                return decrypt_v2(data, aes_key,
                                  xor_key)
        # 4.2 的 V2 容器需要按账号 key；没有就明确失败（不要误当成 3.x XOR）
        if not aes_key:
            return None, None
        if data[:8] == V2_MAGIC_FULL_4X:
            return decrypt_v2_4x(data, aes_key, iv=None)
        return decrypt_v2(data, aes_key, xor_key)
    if len(data) >= 6 and data[:6] in (V2_MAGIC_FULL, V1_MAGIC_FULL):
        if data[:6] == V2_MAGIC_FULL and not aes_key:
            return None, None
        return decrypt_v2(data, aes_key, xor_key)
    # 头部已经是合法图片 / 文档 magic → 明文（4.2 _t_NW 等）
    if detect_format(data[:16]) != 'bin':
        return data, detect_format(data[:16])
    # 其余按单字节 XOR 处理（4.2 的 _W/_t_W，以及 3.x 图片）
    return decrypt_xor(data)


def _strip_suffix(base):
    """去掉缩略图 / 高清后缀，便于落盘时统一文件名"""
    for sfx in ('_t', '_h'):
        if base.endswith(sfx):
            return base[:-len(sfx)]
    return base


def decrypt_dat_file(dat_path, out_path=None, aes_key=None, xor_key=None):
    """解密单个 .dat 文件到磁盘，返回 (输出路径, 扩展名) 或 (None, None)

    out_path 为 None 时：把 .dat 同目录同名文件改成正确扩展名（去掉 _t/_h 后缀）。
    out_path 传一个"已存在目录"或以分隔符结尾时：在该目录下自动拼文件名落盘
    （方便批量解密时统一指定输出根目录，而不是污染原始 attach 目录）。
    """
    try:
        with open(dat_path, 'rb') as f:
            data = f.read()
    except OSError:
        return None, None
    plain, ext = decode_dat_bytes(data, aes_key, xor_key)
    if plain is None:
        return None, None

    if out_path is None:
        base = _strip_suffix(os.path.splitext(dat_path)[0])
        out_path = '%s.%s' % (base, ext)
    else:
        # 允许 --out 传目录：自动拼文件名
        if (os.path.isdir(out_path) or out_path.endswith(os.sep)
                or out_path.endswith('/')):
            base = _strip_suffix(os.path.splitext(os.path.basename(dat_path))[0])
            out_path = os.path.join(out_path, '%s.%s' % (base, ext))

    try:
        os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
        with open(out_path, 'wb') as f:
            f.write(plain)
    except OSError:
        return None, None
    return out_path, ext


# ---------------------------------------------------------------------------
# 4. 4.x AES key 内存提取（仅 Windows + 运行中的微信 + 管理员权限）
# ---------------------------------------------------------------------------
def get_wechat_pids():
    """返回正在运行的微信进程 PID 列表（需要 Windows）

    兼容三套进程名：3.x 的 Weixin.exe、4.x 主程序 WeChat.exe / WeChatAppEx.exe。
    """
    if os.name != 'nt':
        return []
    names = ('Weixin.exe', 'WeChat.exe', 'WeChatAppEx.exe')
    try:
        import subprocess
        # --windowed 的 GUI 程序里起子进程必须带 CREATE_NO_WINDOW，
        # 否则会闪一个黑色 cmd 窗口（和 wmic 那两次是同一类问题）
        out = subprocess.run(['tasklist.exe', '/FO', 'CSV', '/NH'],
                             capture_output=True, text=True,
                             creationflags=0x08000000).stdout
        pids = []
        for line in out.strip().split('\n'):
            for nm in names:
                if nm in line:
                    parts = line.strip('"').split('","')
                    if len(parts) >= 2:
                        try:
                            pids.append(int(parts[1]))
                        except ValueError:
                            pass
                    break
        return pids
    except Exception:
        return []


def find_v2_ciphertext(attach_dir):
    """从一批 V2 缩略图里取第一个 AES 密文块（16 字节），当作"试 key 的靶子"

    v2_decrypt 已知：V2 .dat 偏移 15 起、按 aes_size 对齐后是 AES 密文；取前 16 字节
    就能用 try_key 验证某个候选字符串是不是真 AES key（能解出图片 magic 即命中）。
    """
    files = sorted(_glob(os.path.join(attach_dir, '*', '*', 'Img', '*_t.dat')),
                   key=_mtime, reverse=True)
    for f in files[:100]:
        try:
            with open(f, 'rb') as fp:
                head = fp.read(31)
            if head[:6] == V2_MAGIC_FULL and len(head) >= 31:
                return head[15:31], os.path.basename(f)
        except OSError:
            continue
    return None, None


def try_key(aes_key_bytes, ciphertext):
    """用候选 key 试解 ciphertext（16 字节），能解出已知 magic 才说明 key 对"""
    if not _HAS_CRYPTO or len(ciphertext) < 16:
        return None
    try:
        dec = AES.new(aes_key_bytes[:16], AES.MODE_ECB).decrypt(ciphertext)
    except Exception:
        return None
    if dec[:3] == b'\xFF\xD8\xFF': return 'JPEG'
    if dec[:4] == b'\x89PNG':      return 'PNG'
    if dec[:4] == b'RIFF':         return 'WEBP'
    if dec[:4] == b'wxgf':         return 'WXGF'
    if dec[:3] == b'GIF':          return 'GIF'
    return None


def scan_memory_for_aes_key(pid, ciphertext, timeout=20.0):
    """扫描微信进程内存，返回 16 字符的 AES key 字符串，找不到返回 None

    只在 Windows 生效。逻辑：遍历可读写(RW)内存区 → 正则抠 16/32 位 alphanum 候选
    → 逐个用 try_key 验证能否解出图片 magic。V2 的 key 只在"看图瞬间"存在，
    所以要先在微信里点开 2~3 张大图，立刻跑；找不到就返回 None（让调用方提示用户）。
    这是重活（要读几百 MB 内存），用 timeout 兜底防止卡死太久。
    需要管理员权限，否则 OpenProcess 会失败返回 None。
    """
    if os.name != 'nt' or not _HAS_CRYPTO:
        return None
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    PAGE_RW = (0x04 | 0x08 | 0x40 | 0x80)     # RW / WRITECOPY / EXEC-RW / EXEC-WRITECOPY
    access = 0x0010 | 0x0400                    # VM_READ | QUERY_INFORMATION
    h = k32.OpenProcess(access, False, pid)
    if not h:
        return None

    REGEX32 = re.compile(rb'(?<![a-zA-Z0-9])[a-zA-Z0-9]{32}(?![a-zA-Z0-9])')
    REGEX16 = re.compile(rb'(?<![a-zA-Z0-9])[a-zA-Z0-9]{16}(?![a-zA-Z0-9])')
    class MBI(ctypes.Structure):
        _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p),
                    ('AllocationProtect', wintypes.DWORD), ('RegionSize', ctypes.c_size_t),
                    ('State', wintypes.DWORD), ('Protect', wintypes.DWORD),
                    ('Type', wintypes.DWORD)]
    try:
        addr = 0
        mbi = MBI()
        regions = []
        while addr < 0x7FFFFFFFFFFF:
            if k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                  ctypes.sizeof(mbi)) == 0:
                break
            if (mbi.State == 0x1000 and mbi.Protect != 0x01 and (mbi.Protect & 0x100) == 0
                    and mbi.RegionSize <= 50 * 1024 * 1024 and (mbi.Protect & PAGE_RW)):
                regions.append((mbi.BaseAddress, mbi.RegionSize))
            nxt = addr + mbi.RegionSize
            if nxt <= addr:
                break
            addr = nxt
        buf = ctypes.create_string_buffer
        for base, size in regions:
            b = buf(size)
            rd = ctypes.c_size_t(0)
            if not k32.ReadProcessMemory(h, ctypes.c_void_p(base), b, size, ctypes.byref(rd)):
                continue
            d = b.raw[:rd.value]
            for m in REGEX32.finditer(d):
                kb = m.group()
                for cand in (kb[:16], kb):        # 前 16 字符作 AES-128，或整 32 作 AES-256
                    if try_key(cand, ciphertext):
                        return kb.decode('ascii')[:16]
            for m in REGEX16.finditer(d):
                if try_key(m.group(), ciphertext):
                    return m.group().decode('ascii')
        return None
    except Exception:
        return None
    finally:
        k32.CloseHandle(h)


# ---------------------------------------------------------------------------
# 5. 4.x 消息 → 媒体文件 解析链
# ---------------------------------------------------------------------------
def extract_md5_from_packed_info(blob):
    """从 message_resource.db 的 packed_info(protobuf blob) 里抠出 32 字符 hex 文件 MD5

    典型结构：... \x12\x22\x0a\x20 + 32 字节 ASCII hex MD5 ...
    用固定标记先定位；找不到再暴力扫连续 32 位 hex 字符兜底。
    返回 md5 字符串或 None。
    """
    if not blob or not isinstance(blob, (bytes, bytearray)):
        return None
    marker = b'\x12\x22\x0a\x20'
    idx = blob.find(marker)
    if idx >= 0 and idx + len(marker) + 32 <= len(blob):
        chunk = blob[idx + len(marker): idx + len(marker) + 32]
        try:
            s = chunk.decode('ascii')
            int(s, 16)
            return s
        except (UnicodeDecodeError, ValueError):
            pass
    hexc = set(b'0123456789abcdef')
    i = 0
    while i <= len(blob) - 32:
        if blob[i] in hexc:
            c = blob[i:i + 32]
            if all(x in hexc for x in c):
                try:
                    return c.decode('ascii')
                except UnicodeDecodeError:
                    pass
            i += 32
        else:
            i += 1
    return None


def resolve_4x(attach_dir, username, file_md5, media_sub='Img'):
    """在 4.x attach 目录里找某个 MD5 对应的 .dat 文件

    路径形态：attach/<md5(username)>/<YYYY-MM>/<media_sub>/<file_md5>[_t|_h].dat
    media_sub 视类型而定：Img（图片/表情）/ Video / Voice / File …
    优先选标准版 → 高清 _h → 缩略图 _t。返回命中路径列表（已排序）。
    """
    uh = hashlib.md5(username.encode()).hexdigest()
    base = os.path.join(attach_dir, uh)
    if not os.path.isdir(base):
        return []
    found = []
    for sub in (media_sub, '*'):                    # 先精确子目录，再兜底全扫
        pat = os.path.join(base, '*', sub, '%s*.dat' % file_md5)
        found += _glob(pat)
    return sorted(set(found))


def get_resource_md5(db_path, local_id):
    """查 message_resource.db 的 MessageResourceInfo，用 local_id 取图片/媒体 MD5"""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                'SELECT packed_info FROM MessageResourceInfo WHERE local_id = ?',
                (local_id,)).fetchone()
            if row and row[0]:
                return extract_md5_from_packed_info(row[0])
        finally:
            conn.close()
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# 6. 3.x / 4.x 解析器封装
# ---------------------------------------------------------------------------
class MediaResolver4x(object):
    """4.x：local_id → message_resource.db → MD5 → attach 目录 .dat → 解密

    用法：
        r = MediaResolver4x(attach_dir, resource_db, decrypted_out_dir)
        r.decode_by_local_id(username, local_id, media_sub='Img')
    """

    def __init__(self, attach_dir, resource_db, out_dir):
        self.attach_dir = attach_dir
        self.resource_db = resource_db
        self.out_dir = out_dir

    def decode_by_local_id(self, username, local_id, media_sub='Img',
                           aes_key=None, xor_key=None):
        md5 = get_resource_md5(self.resource_db, local_id)
        if not md5:
            return {'ok': False, 'error': 'message_resource.db 找不到 local_id=%s' % local_id}
        dats = resolve_4x(self.attach_dir, username, md5, media_sub)
        if not dats:
            return {'ok': False, 'error': '找不到 .dat (md5=%s)' % md5, 'md5': md5}
        # 选文件优先级：标准 > _h > _t
        sel = dats[0]
        for f in dats:
            if not os.path.basename(f).startswith(md5 + '_'):
                sel = f
                break
        for f in dats:
            if f.endswith('_h.dat'):
                sel = f
                break
        out = os.path.join(self.out_dir, '%s.%s' % (md5, 'tmp'))
        path, ext = decrypt_dat_file(sel, out, aes_key, xor_key)
        if not path:
            return {'ok': False, 'error': '解密失败 (key 不对？文件:%s)' % os.path.basename(sel)}
        final = os.path.join(self.out_dir, '%s.%s' % (md5, ext))
        if final != path:
            if os.path.exists(final):
                os.unlink(final)
            os.rename(path, final)
        return {'ok': True, 'path': final, 'format': ext, 'md5': md5, 'source': sel,
                'size': os.path.getsize(final)}


class MediaResolver3x(object):
    """3.x：媒体直接按 FileStorage 路径存，图片 .dat 是单字节 XOR（免密钥）

    注意 3.x 的消息表里图片/语音的路径信息在 XML 的 <img>/<voicemsg> 标签或
    HardLinkImage.db / Media.db 里，本类只负责"给一个 .dat 文件就解出来"，
    不解析消息→文件映射（3.x 的映射比 4.x 散，且本工具主线是 4.x）。
    """

    def __init__(self, out_dir):
        self.out_dir = out_dir

    def decrypt_file(self, dat_path, xor_key=None):
        """解一个 3.x .dat（图片）；xor_key 为 None 时自动反推"""
        out = os.path.join(self.out_dir, os.path.splitext(os.path.basename(dat_path))[0] + '.tmp')
        path, ext = decrypt_dat_file(dat_path, out, aes_key=None, xor_key=xor_key)
        if not path:
            return {'ok': False, 'error': 'XOR key 探测失败（可能不是 3.x 图片 .dat）'}
        final = os.path.join(self.out_dir, '%s.%s' % (os.path.splitext(os.path.basename(dat_path))[0], ext))
        if final != path:
            if os.path.exists(final):
                os.unlink(final)
            os.rename(path, final)
        return {'ok': True, 'path': final, 'format': ext}


# ---------------------------------------------------------------------------
# 小工具（避免 Windows glob 在超大目录过慢 + 兼容）
# ---------------------------------------------------------------------------
def _glob(pattern):
    import glob
    return glob.glob(pattern)


def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# CLI：python -m core.media <dat_file> [--aes-key K] [--xor-key K] [--out DIR]
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import sys
    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print('用法: python -m core.media <dat文件> [--aes-key 16字符] [--xor-key 十六进制] [--out 目录]')
        print('  解密单个微信 .dat（自动识别 3.x XOR / 4.x V1 / 4.x V2）')
        sys.exit(0)
    dat_file = args[0]
    aes = None
    xor = None
    out = None
    i = 1
    while i < len(args):
        a = args[i]
        if a == '--aes-key' and i + 1 < len(args):
            aes = args[i + 1]; i += 2
        elif a == '--xor-key' and i + 1 < len(args):
            xor = int(args[i + 1], 16); i += 2
        elif a == '--out' and i + 1 < len(args):
            out = args[i + 1]; i += 2
        else:
            i += 1
    if not os.path.exists(dat_file):
        print('文件不存在: %s' % dat_file)
        sys.exit(1)
    path, ext = decrypt_dat_file(dat_file, out, aes_key=aes, xor_key=xor)
    if path:
        print('解密成功: %s' % path)
        print('格式: %s, 大小: %d 字节' % (ext, os.path.getsize(path)))
    else:
        print('解密失败：文件可能不是微信 .dat，或 4.x V2 缺 --aes-key / key 不对')
        sys.exit(1)
