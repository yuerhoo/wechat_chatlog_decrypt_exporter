# -*- coding: utf-8 -*-
"""静态扫描 Weixin.dll，提取 4.1.10.31+ / 4.2 的 XOR 混淆常量

在整条密钥链路里的位置（第 1 步，见 get_key_v42.run()）：
    1) **本模块**        读 Weixin.dll 的 .text 段，找"4 条 mov rdx, imm64"指令序列
    2) key_v4.py         扫微信进程内存，找 32 字节候选
    3) get_key_v42       候选 XOR 常量 = 账号级 passphrase，用 db 首页 HMAC 验真

找的是什么：微信把账号级密钥按 4 个 8 字节切片，以立即数形式编进指令里
（x86-64 的 `mov rdx, imm64` 每条正好带 8 字节操作数），4 × 8 = 32 字节，
正好是 AES-256 密钥的长度。这段指令在 DLL 文件里是静态的，
所以不需要运行微信就能先把候选全部提取出来 —— 这才是"版本无关"的关键：
微信升级换了密钥，指令里的立即数跟着变，重新扫一遍就有新常量。

为什么用多进程 + 正则而不是反汇编：
    Weixin.dll 有几十 MB，.text 段占大头，纯 Python 逐字节匹配要几十秒。
    这里按 2MB 分块扔进进程池，配合预编译的字节正则，整段扫完在秒级。

产出：keys/<版本号>.jsonl，每行一个 {"va": "0x...", "key": "AA BB ..."}。
      va 是虚拟地址，只用于人工去 IDA 里核对；程序实际只用 key 字段。
"""
import json
import sys
import re
import time
import os
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed

try:
    import pefile
except ImportError:
    print("[!] 请先安装 pefile 库: pip install pefile")
    sys.exit()

# 结果统一落在脚本同目录的 keys/ 下，不依赖当前工作目录
# （get_key_v42 运行时会把这个模块级变量改成自己的 keys/ 目录）
KEYS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys")

# 编译好的特征码正则
# 逐段解释（整段匹配的就是"连续 4 次给 rdx 赋 64 位立即数，最后 test rax,rax"）：
#   b"^\x48\xBA(.{8})"   mov rdx, imm64 → 48 BA + 8 字节立即数（第 1 片）
#   b".{3,8}?"           中间夹的存储类指令（mov [reg+disp], rdx 等），长度不固定
#                        —— 用最小的惰性通配 3~8 字节，既能跨过去又不会吃太多
#   ... 重复 4 次 ...     共取出 4 片，拼成 32 字节密钥
#   b"\x48\x85\xC0"      test rax, rax，作为序列收尾的锚点（防止把无关的 4 条凑一起）
# re.DOTALL 让 "." 也能匹配到 0x0A 之类的字节（二进制里到处都是，默认会被当换行跳过）
PATTERN = re.compile(
    b"^\x48\xBA(.{8})"       # mov rdx, <8字节>
    b".{3,8}?"               # 中间的 mov [xxx], rdx
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\x85\xC0",         # test rax, rax
    re.DOTALL
)

def worker_search(task):
    """
    子进程执行函数：读取自己负责的文件片段，进行极速搜索

    task = (文件路径, 起始偏移, 本块大小, 重叠字节数, 本块的虚拟地址基准)

    为什么要传"文件路径 + 偏移"而不是直接传字节：几十 MB 的数据经进程池
    序列化传递，光 IPC 拷贝就比扫描本身还慢；让每个子进程自己 seek 读盘更快。
    """
    file_path, file_offset, chunk_size, overlap, base_va = task
    results = []

    # 让子进程独立读取自己的分块，避免 Python 多进程之间传递巨量数据的 IPC 序列化卡顿
    with open(file_path, 'rb') as f:
        f.seek(file_offset)
        # 读取指定大小 + 冗余重叠部分(防止特征码被从中切断)
        # 重叠区不参与"输出结果"的判定（见下面 idx >= chunk_size 的判断），
        # 只用来让跨边界的特征码能在这一块里被完整匹配到。
        chunk_data = f.read(chunk_size + overlap)

    offset = 0
    while True:
        # 先用 find 快速定位候选起点（比正则全文跑快得多），再对候选位置做严格匹配
        idx = chunk_data.find(b'\x48\xBA', offset)

        # 如果找不到，或者找到了但已经进入了重叠区(交由下一个分块处理防止重复)
        if idx == -1 or idx >= chunk_size:
            break

        # 截取 85 字节进行严格正则匹配
        # 85 = 4 段×(2 字节指令 + 8 字节立即数) + 3 段×最长 8 字节间隔 + 3 字节收尾
        match = PATTERN.match(chunk_data[idx : idx + 85])
        if match:
            # 提取小端序 32 字节 Key：把 4 个 8 字节立即数按顺序首尾相接
            key_bytes = match.group(1) + match.group(2) + match.group(3) + match.group(4)
            key_hex = key_bytes.hex().upper()
            formatted_key = " ".join(key_hex[i:i+2] for i in range(0, len(key_hex), 2))

            # 保存结果：VA 地址、物理偏移、格式化好的 Key
            # va = 本块在内存里的起始地址 + 块内偏移（IDA 里可以直接按这个地址跳过去看）
            results.append({
                'va': base_va + idx,
                'file_offset': file_offset + idx,
                'key': formatted_key
            })
            # 命中后整段跳过（连指令带立即数），避免在同一个序列里重复匹配
            offset = idx + len(match.group(0))
        else:
            offset = idx + 1

    return results

def extract_xor_keys_multiprocess(dll_path, version="unknown"):
    """扫描整个 DLL 的代码段，把所有 32 字节 XOR 常量写进 keys/<版本号>.jsonl

    参数：
        dll_path   Weixin.dll 的完整路径（<InstallPath>/<版本号>/Weixin.dll）
        version    版本号字符串，只用来给输出文件命名，例如 "4.2.0.13"；
                   不传则写 unknown.jsonl（会被下一次同版本扫描覆盖）

    步骤：
        1) pefile 解析 PE 头，取 ImageBase 和每个节的属性
        2) 只扫 Characteristics 带 0x20000000（IMAGE_SCN_CNT_CODE）的节，即 .text
        3) 按 2MB 切块 + 100 字节重叠，交给进程池并行搜索
        4) 结果按 VA 排序后逐条写入 jsonl（同一次扫描追加写，先删旧文件由调用方负责）

    找不到任何命中时不报错，只打印 0 处 —— 这通常意味着微信又改了编译产物，
    需要按新的指令序列重新适配正则。
    """
    print(f"[*] 正在分析目标: {dll_path}")
    print(f"[*] 正在启动多进程并行引擎 (CPU 核心数: {multiprocessing.cpu_count()})...")
    start_time = time.time()
    os.makedirs(KEYS_DIR, exist_ok=True)

    try:
        pe = pefile.PE(dll_path, fast_load=True) # fast_load 更快解析头
    except Exception as e:
        print(f"[!] PE 解析失败: {e}")
        return

    image_base = pe.OPTIONAL_HEADER.ImageBase
    tasks = []

    # 每个核心分配的数据块大小：2MB
    # 太小则任务数暴涨（调度开销），太大则并行度不够
    CHUNK_SIZE = 2 * 1024 * 1024
    # 块与块之间的重叠大小，确保特征码跨越边界时不会被漏掉
    # 100 字节 > 单条特征序列的最大长度（85），足够覆盖任何一种跨边界情况
    OVERLAP_SIZE = 100

    # 1. 扫描 PE 段，生成切片任务
    for section in pe.sections:
        if section.Characteristics & 0x20000000: # 仅处理代码段
            sec_size = section.SizeOfRawData
            sec_file_offset = section.PointerToRawData
            sec_va = image_base + section.VirtualAddress

            # 将巨大的代码段切成若干个 2MB 的小块
            # 最后一块用 min() 收窄，否则会读到下一个节的数据里
            for i in range(0, sec_size, CHUNK_SIZE):
                current_chunk_size = min(CHUNK_SIZE, sec_size - i)
                tasks.append((
                    dll_path,
                    sec_file_offset + i,
                    current_chunk_size,
                    OVERLAP_SIZE,
                    sec_va + i
                ))

    # 2. 扔进进程池火力全开并行运算
    # 这里开满 cpu_count()（纯内存/文件读取，不像 key_v4 那样受内存带宽拖累）
    found_matches = []
    with ProcessPoolExecutor(max_workers=multiprocessing.cpu_count()) as executor:
        # 提交所有任务
        futures = {executor.submit(worker_search, task): task for task in tasks}

        # 实时收集结果
        for future in as_completed(futures):
            found_matches.extend(future.result())

    # 3. 按地址排序并输出结果
    # 排序是为了输出稳定：同一份 DLL 每次扫出来的顺序一致，便于 diff 对比
    found_matches.sort(key=lambda x: x['va'])

    pe.close()  # 及时关闭 PE 文件释放资源

    for i, res in enumerate(found_matches):
        print(f"\n[+] 发现第 {i+1} 处加密逻辑:")
        print(f"    -> 提取的 32 Byte Key: {res['key']}")
        print(f"    -> IDA 虚拟地址 (VA):  0x{res['va']:X}  <-- 请去 IDA 验证")
        print(f"    -> 文件物理偏移量:     0x{res['file_offset']:X}")

        json_dict = {
            "va": f"0x{res['va']:X}",
            "key": res['key']
        }

        # 逐条追加写：即使中途被打断，已找到的常量也已经落盘，不必从头再扫
        with open(os.path.join(KEYS_DIR, f"{version}.jsonl"), "a") as f:
            json.dump(json_dict, f)
            f.write("\n")

    print(f"\n[*] 并行扫描完毕，共发现 {len(found_matches)} 处，总耗时: {time.time() - start_time:.3f} 秒")

if __name__ == "__main__":
    # Windows 下多进程必须保护入口点
    # （打包成 exe 后，子进程会带着 --multiprocessing-fork 参数重新启动，靠这一行挡住）
    multiprocessing.freeze_support()

    # 用法: python scan.py <Weixin.dll路径> [版本号]
    if len(sys.argv) < 2:
        print("用法: python scan.py <Weixin.dll路径> [版本号]")
        sys.exit(2)
    path = sys.argv[1]
    # 不给版本号时从路径猜：.../<版本号>/Weixin.dll 的上一级目录名就是版本号
    version = sys.argv[2] if len(sys.argv) > 2 else path.replace("\\", "/").split("/")[-2]
    extract_xor_keys_multiprocess(path, version=version)
