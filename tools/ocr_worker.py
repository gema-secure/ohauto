"""OCR worker —— 在**装了 OCR 后端的解释器**里执行一次识别。

为什么拆成独立进程
------------------
本项目的 Python 包隔离约定是「包只装进 venv」（不污染托管运行时），
但项目脚本平时用托管解释器跑。为了不破坏这个约定，OCR 走**子进程**：
融合器发现当前解释器没有后端时，用 venv 的解释器跑这个 worker。

    用法（通常不手敲，由 `ohauto.fusion.source_ocr` 调用）：
        <venv>/Scripts/python.exe tools/ocr_worker.py <图片路径>

输出：**每行一条识别到的文本**，识别失败则往 stderr 写原因并以非 0 退出。
（刻意不用 JSON —— 子进程通信越简单越好，调用方按行读即可。）
"""
from __future__ import annotations

import asyncio
import os
import sys

# stdout / stderr **必须固定成 UTF-8** —— 这是被调用方正确解码的前提。
# Windows 上 Python 默认按 locale（cp936）写 stdout，而 fusion.py 那头按 utf-8 解码
# → 中文识别结果到调用方手里就是 UnicodeDecodeError。
# ⚠️ 而且它不表现为「报错」：异常发生在 subprocess 的 _readerthread 里，
# 调用方拿到的是 returncode=0 + stdout=None，看起来跟「没读到文字」一样。
# 这里是**进程内自钉**，谁调用、用不用环境变量都不影响，比依赖调用方传 env 更稳。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')      # type: ignore[union-attr]
    except (AttributeError, ValueError):           # Python <3.7 或已被重定向成非 TextIO
        pass
del _stream


async def _winsdk_ocr(path: str) -> list:
    """Windows 自带 OCR 引擎 —— 不需要下载模型，中文支持取决于系统语言包。"""
    import winsdk.windows.globalization as glob
    import winsdk.windows.graphics.imaging as imaging
    import winsdk.windows.media.ocr as ocr
    import winsdk.windows.storage as storage

    file = await storage.StorageFile.get_file_from_path_async(os.path.abspath(path))
    stream = await file.open_async(storage.FileAccessMode.READ)
    decoder = await imaging.BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()

    # 优先中文，再退到系统已安装语言 —— 两个都拿不到才算失败
    engine = ocr.OcrEngine.try_create_from_language(glob.Language('zh-Hans-CN'))
    if engine is None:
        engine = ocr.OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError('系统没有可用的 OCR 语言包（需在 Windows 设置里装中文语言）')

    result = await engine.recognize_async(bitmap)
    return [line.text for line in result.lines]


def _rapidocr(path: str) -> list:
    """rapidocr-onnxruntime —— 纯 Python + ONNX，自带中英文模型，无需外部程序。

    之所以两个后端都留着：`winsdk` 用系统引擎（不下载模型）但装得慢、
    平台绑定；`rapidocr` 是纯 Python 但首次会下模型。**哪个先能用就用哪个**，
    调用方不必关心。
    """
    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR()
    result, _elapse = engine(path)
    return [item[1] for item in (result or [])]


def main() -> int:
    if len(sys.argv) < 2:
        print('用法: python ocr_worker.py <图片路径>', file=sys.stderr)
        return 2
    path = sys.argv[1]
    if not os.path.isfile(path):
        print('图片不存在: %s' % path, file=sys.stderr)
        return 2

    tried = []
    # 后端 1：Windows 系统 OCR（不下载模型）
    try:
        lines = asyncio.run(_winsdk_ocr(path))
        # 后端名走 stderr —— 调用方要能如实标注「这次是谁读的」，
        # 标注错了就是假信息（stdout 只放识别结果，保持干净）
        print('backend=winsdk', file=sys.stderr)
        for t in lines:
            t = (t or '').strip()
            if t:
                print(t)
        return 0
    except ImportError as e:
        tried.append('winsdk 未安装(%s)' % e)
    except Exception as e:
        tried.append('winsdk 失败(%s: %s)' % (type(e).__name__, e))

    # 后端 2：rapidocr（纯 Python）
    try:
        lines = _rapidocr(path)
        print('backend=rapidocr', file=sys.stderr)
        for t in lines:
            t = (t or '').strip()
            if t:
                print(t)
        return 0
    except ImportError as e:
        tried.append('rapidocr 未安装(%s)' % e)
    except Exception as e:
        tried.append('rapidocr 失败(%s: %s)' % (type(e).__name__, e))

    print('本解释器没有可用的 OCR 后端 —— %s' % '；'.join(tried), file=sys.stderr)
    return 3


if __name__ == '__main__':
    raise SystemExit(main())
