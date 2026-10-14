# -*- coding: utf-8 -*-
"""生成 README 分层架构图（PNG）。

输出：docs/architecture.png
运行：python tools/arch_diagram.py

中文渲染依赖系统的 CJK 字体：脚本按平台候选表自动定位，
也可用环境变量 OHAUTO_DIAGRAM_FONT 指定字体文件；都找不到时报错退出。
"""

from __future__ import annotations

import os
from PIL import Image, ImageDraw, ImageFont

#: 中文字形候选：按平台列出常见路径，命中第一个存在的即用。
#: 找不到时**直接报错退出**，不回落 Pillow 默认位图字体 —— 默认字体没有
#: 中文字形，会静默画出满屏方框，比直接报错更坏。
#: 可用环境变量 OHAUTO_DIAGRAM_FONT 显式指定字体文件。
_FONT_ENV = 'OHAUTO_DIAGRAM_FONT'

_FONT_REG_CANDIDATES = (
    'C:/Windows/Fonts/msyh.ttc',                                  # Windows 微软雅黑
    'C:/Windows/Fonts/msyh.ttf',
    'C:/Windows/Fonts/simhei.ttf',                                # Windows 黑体
    'C:/Windows/Fonts/simsun.ttc',                                # Windows 宋体
    '/System/Library/Fonts/PingFang.ttc',                         # macOS
    '/System/Library/Fonts/Hiragino Sans GB.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',     # Linux
    '/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf',
    '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
    '/usr/share/fonts/opentype/source-han-sans/SourceHanSans-Regular.otf',
)

_FONT_BOLD_CANDIDATES = (
    'C:/Windows/Fonts/msyhbd.ttc',
    'C:/Windows/Fonts/simhei.ttf',
    '/System/Library/Fonts/PingFang.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
    '/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc',
    '/usr/share/fonts/opentype/source-han-sans/SourceHanSans-Bold.otf',
)


def _resolve_font(bold: bool) -> str:
    """定位一个可渲染中文的字体文件。找不到就报错，不静默降级。"""
    env = os.environ.get(_FONT_ENV)
    if env:
        if os.path.isfile(env):
            return env
        raise SystemExit(f'{_FONT_ENV} 指向的字体文件不存在：{env}')
    for path in (_FONT_BOLD_CANDIDATES if bold else _FONT_REG_CANDIDATES):
        if os.path.isfile(path):
            return path
    if bold:                       # 粗体缺席时退用常规字重：字形正确，只是不加粗
        for path in _FONT_REG_CANDIDATES:
            if os.path.isfile(path):
                return path
    raise SystemExit(
        '找不到可渲染中文的字体。请安装任一 CJK 字体（如 Noto Sans CJK），'
        f'或用环境变量 {_FONT_ENV} 指定字体文件路径。\n'
        '这里不回落 Pillow 默认字体：它没有中文字形，会画出满屏方框。')

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "architecture.png")

S = 2  # 超采样倍数

BG = (255, 255, 255)
FRAME = (110, 120, 135)
LABEL_BG = (255, 255, 255)
TITLE = (40, 46, 58)
BODY = (46, 52, 64)

# 每层：(左侧层名, 层内主色系, [内容块])
# 块 = (标题, 副标题, 是否强调)
LAYERS = [
    (
        "L5",
        "应用与工装层",
        (206, 224, 240),   # 冷蓝灰
        (108, 140, 176),
        (234, 243, 250),
        [
            ("tools/", "质量门禁 · 静态检查 · 离线评测 · 长稳编排（51 个脚本）", False),
            ("examples/", "冒烟 · 套件执行 · 探索 · 用例生成示例（7 个）", False),
            ("tests/", "单元与集成测试（56 个文件 / 1529 项，离线可跑）", False),
        ],
    ),
    (
        "L4",
        "编排层",
        (198, 232, 250),   # 浅蓝
        (120, 180, 226),   # 块边框
        (232, 245, 253),   # 块填充
        [
            ("explorer.py", "自动探索 · 页面状态图 · 回归用例", False),
            ("generator.py", "自然语言 → 用例（两阶段）", False),
            ("diagnose.py", "失败归因四分类 · 信号交叉印证", False),
            ("signals.py", "崩溃 / 白屏 / 无响应 / 无窗口", False),
            ("runner.py", "套件编排 · 重试 · 设备保活", False),
            ("crossform_report.py", "跨形态差异报告", False),
        ],
    ),
    (
        "L3",
        "能力层",
        (216, 212, 246),   # 浅紫
        (152, 144, 214),
        (241, 239, 252),
        [
            ("action.py", "Action DSL · YAML 中间表示与执行", False),
            ("locator.py", "定位器健康账本 · 五档线索 · 自愈换代", True),
            ("fusion.py", "N 源契约融合 · Claim 契约裁决", True),
            ("crossform.py", "跨形态差异比对（四类判据）", False),
            ("sim.py", "模拟设备 · 崩溃与故障注入", False),
        ],
    ),
    (
        "L2",
        "设备通道层",
        (208, 240, 222),   # 浅绿
        (108, 186, 138),
        (236, 249, 241),
        [
            ("hdc.py", "hdc 命令封装 · 三档输入通路", False),
            ("vision.py", "多模态定位 · 可插拔 Provider", True),
            ("driver.py", "Driver 门面：定位→操作→断言→留痕", False),
            ("devices.py", "设备形态档案（68 台 / 11 类型）", False),
            ("doctor.py", "环境自检", False),
        ],
    ),
    (
        "L1",
        "基元层",
        (253, 238, 205),   # 浅黄
        (226, 186, 106),
        (254, 248, 232),
        [
            ("layout.py", "控件树解析（字段别名宽进）", False),
            ("matcher.py", "多属性匹配器（对齐 UiTest ON）", False),
            ("identity.py", "控件身份指纹 · 两级稳定标识", False),
            ("treesum.py", "控件树摘要（紧凑表示）", False),
            ("permission.py", "系统权限弹窗识别与策略", False),
            ("perf.py", "性能 / 资源采样原语", False),
            ("report.py", "JSON / Markdown / HTML 报告", False),
            ("llm_transport.py", "LLM 传输与解析共用底座", False),
            ("static_arkts/", "静态源码桥接（Node 子进程调用）", False),
        ],
    ),
    (
        "L0",
        "设备层",
        (253, 222, 210),   # 浅橙红
        (226, 140, 108),
        (253, 238, 232),
        [
            ("hdc 通道 —— 设备侧零部署", "DAYU200 真机（OpenHarmony 5.0.x）· DevEco 模拟器实例", True),
        ],
    ),
]


def build():
    # 版本 A：左标签 + 右内容块（2 列内容排布）
    pad = 24
    gap_layer = 14
    label_w = 150
    inner_pad = 16
    block_gap_x = 14
    block_gap_y = 12
    content_w = 1460
    img_w = pad + label_w + inner_pad + content_w + inner_pad + pad

    # 预计算每层高度
    layouts = []
    y = pad + 56  # 顶部留标题区
    for code, name, lbg, lbd, fbg, blocks in LAYERS:
        cols = 2 if len(blocks) > 1 else 1
        rows = (len(blocks) + cols - 1) // cols
        blk_h = 82
        layer_h = inner_pad * 2 + rows * blk_h + (rows - 1) * block_gap_y
        layouts.append((code, name, lbg, lbd, fbg, blocks, y, layer_h, cols, blk_h))
        y += layer_h + gap_layer
    img_h = y - gap_layer + pad

    img = Image.new("RGB", (img_w * S, img_h * S), BG)
    d = ImageDraw.Draw(img)

    fp_bold, fp_reg = _resolve_font(True), _resolve_font(False)
    f_title = ImageFont.truetype(fp_bold, 30 * S)
    f_code = ImageFont.truetype(fp_bold, 26 * S)
    f_lname = ImageFont.truetype(fp_bold, 22 * S)
    f_bname = ImageFont.truetype(fp_bold, 21 * S)
    f_bdesc = ImageFont.truetype(fp_reg, 17 * S)

    def ctext(x, y, text, font, fill, anchor="lm"):
        d.text((x * S, y * S), text, font=font, fill=fill, anchor=anchor)

    def width_of(text, font):
        return d.textlength(text, font=font) / S

    # 顶部标题
    ctext(pad, pad + 20, "ohauto 分层架构", f_title, TITLE, anchor="lm")
    ctext(pad + width_of("ohauto 分层架构", f_title) + 18, pad + 24,
          "六层 · L1–L4 共 25 个可校验模块（静态检查 LAY001 逐层校验：低层模块不得 import 高层模块）",
          f_bdesc, (110, 118, 132), anchor="lm")

    for (code, name, lbg, lbd, fbg, blocks, ly, lh, cols, blk_h) in layouts:
        # 层背景
        d.rounded_rectangle(
            (pad * S, ly * S, (img_w - pad) * S, (ly + lh) * S),
            radius=12 * S, fill=lbg, outline=lbd, width=2 * S)

        # 左侧层名标签
        lx0 = pad + 10
        d.rounded_rectangle(
            (lx0 * S, (ly + 10) * S, (lx0 + label_w - 12) * S, (ly + lh - 10) * S),
            radius=10 * S, fill=LABEL_BG, outline=lbd, width=2 * S)
        cx = lx0 + (label_w - 12) // 2
        cy = ly + lh / 2
        ctext(cx, cy - 26, code, f_code, lbd, anchor="mm")
        ctext(cx, cy + 2, name, f_lname, TITLE, anchor="mm")
        # 该层条目数
        cnt_label = {
            "L5": "3 类工装",
            "L4": "6 模块",
            "L3": "5 模块",
            "L2": "5 模块",
            "L1": "9 模块",
            "L0": "设备侧",
        }[code]
        ctext(cx, cy + 28, cnt_label, f_bdesc, (120, 128, 142), anchor="mm")

        # 内容区
        bx0 = pad + 10 + label_w + 6
        avail = content_w + inner_pad * 2 - (label_w + 16) + 6
        avail = (img_w - pad - 10) - bx0
        col_w = (avail - (cols - 1) * block_gap_x) / cols

        for i, (bname, bdesc, emph) in enumerate(blocks):
            r, c = divmod(i, cols)
            x0 = bx0 + c * (col_w + block_gap_x)
            y0 = ly + inner_pad + r * (blk_h + block_gap_y)
            fill = (255, 255, 255) if emph else fbg
            outline = lbd if emph else lbd
            d.rounded_rectangle((x0 * S, y0 * S, (x0 + col_w) * S, (y0 + blk_h) * S),
                                radius=8 * S, fill=fill, outline=outline,
                                width=(3 if emph else 2) * S)
            ctext(x0 + 16, y0 + 28, bname, f_bname, TITLE, anchor="lm")
            ctext(x0 + 16, y0 + 56, bdesc, f_bdesc, BODY, anchor="lm")

    # 外框
    d.rectangle((6 * S, 6 * S, (img_w - 6) * S, (img_h - 6) * S),
                outline=FRAME, width=3 * S)

    img = img.resize((img_w, img_h), Image.LANCZOS)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img.save(OUT, "PNG", optimize=True)
    print("saved:", OUT, img.size)


if __name__ == "__main__":
    build()
