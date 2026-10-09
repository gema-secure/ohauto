# -*- coding: utf-8 -*-
"""跨应用实证叠加图生成 —— 「多模态怎么表现」的图解。

对每个样本：截图 + 控件树叠加（蓝框 = 可交互控件，绿线 = 有文案控件），
直观呈现三种形态：
  1. 树健康（有 id/文案）→ L1 免调模型
  2. 树缺 id（可交互但无标识）→ 视觉通道兜底
  3. 纯 Canvas（控件树 0 可交互）→ OCR 唯一通道

用法：python tools/make_overlays.py --out tools/_out/multiapp_overlays
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PIL import Image, ImageDraw, ImageFont            # noqa: E402
from ohauto.layout import parse_layout                # noqa: E402

FONT = 'C:/Windows/Fonts/msyh.ttc'
BLUE = (8, 168, 246)
GREEN = (117, 189, 66)
INK = (26, 26, 26)

#: 样本 → 说明（形态）
CASES = [
    ('app_settings', 'datasets/gallery_13app',
     '形态二：183 节点 / 28 可交互 / 0 个 id —— 视觉通道兜底'),
    ('launcher', 'datasets/gallery_13app',
     '形态一：211 节点 / 18 可交互（有文案）—— L1 免调模型'),
    ('etsclock', 'datasets/gallery_13app',
     '形态三：77 节点 / 0 可交互 —— 纯 Canvas，OCR 唯一通道'),
]


def overlay(png_path: str, json_path: str, out_path: str, note: str) -> None:
    im = Image.open(png_path).convert('RGB')
    d = ImageDraw.Draw(im)
    root = parse_layout(json_path)
    n_i = n_r = 0
    for n in root.walk():
        r = n.rect
        if r.left < 0 or r.top < 0 or r.right > im.width or r.bottom > im.height:
            continue
        if n.is_interactive():
            d.rectangle([r.left, r.top, r.right, r.bottom],
                        outline=BLUE, width=3)
            n_i += 1
        elif (n.text or '').strip() and r.height > 8:
            d.line([(r.left, r.bottom - 1), (r.right, r.bottom - 1)],
                   fill=GREEN, width=2)
            n_r += 1
    font = ImageFont.truetype(FONT, 22)
    d.rectangle([0, im.height - 46, im.width, im.height], fill=(255, 255, 255))
    d.text((12, im.height - 38), note, font=font, fill=INK)
    d.text((12, 6), f'可交互 {n_i} · 文案行 {n_r}', font=font, fill=BLUE)
    im.save(out_path)
    print('overlay:', out_path, f'interactive={n_i}')


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='tools/_out/multiapp_overlays')
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    for name, src, note in CASES:
        png = os.path.join(ROOT, src, f'{name}.png')
        js = os.path.join(ROOT, src, f'{name}.json')
        if not (os.path.isfile(png) and os.path.isfile(js)):
            print(f'[skip] {name}: 样本缺失')
            continue
        overlay(png, js, os.path.join(args.out, f'{name}_overlay.png'), note)
    return 0


if __name__ == '__main__':
    sys.exit(main())
