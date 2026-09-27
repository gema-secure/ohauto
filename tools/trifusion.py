"""三源融合 CLI —— 通用内核（`ohauto.fusion`）的一层薄封装。

三个源怎么接、融合规则怎么写，都在 `ohauto/fusion.py` 里；
**这个文件只负责「把命令行参数变成 SourceResult」**。

用法::

    # 声明源 = 源码（有源码的应用）
    python tools/trifusion.py --static-project D:/project/ohauto-hypium-test \\
        --runtime-json _out/trifusion_src/layout.json --page pages/Index

    # 声明源 = 已沉淀的用例（没源码的应用也能查「用例有没有失效」）
    python tools/trifusion.py --case examples/cases/calculator.yaml \\
        --runtime-json datasets/real_samples_20260919/sample_calc.json

    # 只有运行时（没有声明源）—— 会同降级并说明，不会假装做过对齐
    python tools/trifusion.py --runtime-json datasets/real_samples_20260919/app_settings.json
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto import fusion                                 # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description='三源（N 源）融合，通用内核')
    ap.add_argument('--static-project', help='声明源：ArkTS 工程目录')
    ap.add_argument('--case', dest='case_path', help='声明源：用例 YAML')
    ap.add_argument('--runtime-json', help='观察源：运行时控件树 JSON')
    ap.add_argument('--ocr-image', help='观察源：截图路径（**本地** OCR 读文字，免费离线）')
    ap.add_argument('--vision-image',
                    help='观察源：截图路径（**云视觉模型**理解，需要 OHAUTO_VISION_* key）')
    ap.add_argument('--vision-instruction',
                    help='给视觉模型的提问。默认只让它读文字（便于与其它源对齐）；'
                         '问「这是什么页面/这个图标干什么」就换成语义理解')
    ap.add_argument('--page', default='', help='作用域（页面路由），如 pages/Index')
    ap.add_argument('--out', help='报告输出路径（Markdown）')
    args = ap.parse_args()

    sources = []
    if args.static_project:
        sources.append(fusion.source_static_project(args.static_project, page=args.page))
    if args.case_path:
        sources.append(fusion.source_case(args.case_path))
    if args.runtime_json:
        if not os.path.isfile(args.runtime_json):
            print('运行时控件树不存在: %s' % args.runtime_json)
            return 2
        with open(args.runtime_json, encoding='utf-8') as f:
            sources.append(fusion.source_runtime_tree(f.read(), name='runtime',
                                                      scope=args.page))
    if args.ocr_image:
        sources.append(fusion.source_ocr(args.ocr_image))
    if args.vision_image:
        vkw = {}
        if args.vision_instruction:
            vkw['instruction'] = args.vision_instruction
        sources.append(fusion.source_vision(args.vision_image, **vkw))

    if not sources:
        print('至少要给一个源（--static-project / --case / --runtime-json）')
        return 2

    print('参与融合的源：')
    for s in sources:
        print('  %-10s %-12s %s' % (s.name, s.role,
                                    ('%d 条主张' % len(s.claims)) if s.ok else
                                    ('未运行：%s' % s.reason)))

    rep = fusion.fuse(sources, scope=args.page)
    md = fusion.render_md(rep, title='融合报告（%s）' % (args.page or '全局'))
    print()
    print(md)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(md)
        print('报告已写入 %s' % args.out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
