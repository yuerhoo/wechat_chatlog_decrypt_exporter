# -*- coding: utf-8 -*-
"""wxgf（微信 4.x HEVC 原图）解码

为什么需要它：
    微信 4.x 的原图里有相当比例是 wxgf 容器——magic 'wxgf' + 一段变长头 +
    标准 HEVC Annex-B 裸流（VPS/SPS/PPS + IDR）。Qt 只认 jpg/png，解不开，
    所以过去只能退到 `_t.dat` 缩略图（实测只有 150×210 左右），画框一放大就糊。

做法：
    用 PyAV（wheel 自带 ffmpeg，不依赖系统 ffmpeg）打开这段裸流，取第一帧，
    在 av 内部 swscale 成 rgb24，然后直接从 VideoPlane 的 buffer 指针读字节。
    这样不需要 numpy、不需要 Pillow，也不落临时文件。

对外只有一个函数 decode(data) -> (rgb_bytes, w, h, stride) 或 None。
PyAV 没装（或解不出来）时安静返回 None，调用方照旧退回缩略图。
"""
import ctypes
import io

WXGF_MAGIC = b'wxgf'
# HEVC NAL 起始码 + VPS（nal_unit_type=32 → 头字节 0x40 0x01）
_NAL_VPS4 = b'\x00\x00\x00\x01\x40\x01'
_NAL_VPS3 = b'\x00\x00\x01\x40\x01'
_NAL_START4 = b'\x00\x00\x00\x01'

_AV = None
_AV_TRIED = False


def _av():
    """懒加载 PyAV：没装时整个模块仍可 import，只是 decode() 返回 None"""
    global _AV, _AV_TRIED
    if not _AV_TRIED:
        _AV_TRIED = True
        try:
            import av                       # noqa: F401
            _AV = av
        except Exception:
            _AV = None
    return _AV


def available():
    """当前环境能不能解 HEVC（打包版恒为 True）"""
    return _av() is not None


def is_wxgf(data):
    return bool(data) and data[:4] == WXGF_MAGIC


def _stream_offset(data):
    """找 HEVC 裸流起点：优先 VPS，退而求其次任意 4 字节起始码"""
    i = data.find(_NAL_VPS4)
    if i < 0:
        i = data.find(_NAL_VPS3)
    if i < 0:
        i = data.find(_NAL_START4)
    return i


def decode(data):
    """wxgf 字节 -> (rgb, w, h, stride)；不是 wxgf / 解不出来返回 None

    rgb 是 RGB888 的紧密字节串（stride 可能大于 w*3，转 QImage 时传进去）。
    """
    av = _av()
    if av is None or not is_wxgf(data):
        return None
    off = _stream_offset(data)
    if off < 0:
        return None
    try:
        with av.open(io.BytesIO(data[off:]), format='hevc') as box:
            for frame in box.decode(video=0):
                rgb = frame.reformat(format='rgb24')
                plane = rgb.planes[0]
                raw = ctypes.string_at(plane.buffer_ptr, plane.buffer_size)
                return raw, rgb.width, rgb.height, plane.line_size
    except Exception:
        return None
    return None


def probe_size(data):
    """只解头，拿宽高（给占位画框算比例用）；失败返回 (0, 0)"""
    av = _av()
    if av is None or not is_wxgf(data):
        return 0, 0
    off = _stream_offset(data)
    if off < 0:
        return 0, 0
    try:
        with av.open(io.BytesIO(data[off:]), format='hevc') as box:
            for frame in box.decode(video=0):
                return frame.width, frame.height
    except Exception:
        return 0, 0
    return 0, 0
