"""CI 质量门禁 —— 一条命令跑完全部检查。

对应规划「工程规范与 CI 质量门禁」高地：

| 规划要求 | 本脚本的关卡 |
|---|---|
| 静态检查 | [1/3] `ci/static_check.py`（零依赖，见该文件说明） |
| 单元测试 | [2/3] `unittest discover`（589 项） |
| 覆盖率 ≥70% | [2/3] `coverage report`（`.coveragerc` 里 `fail_under`） |
| 模拟端到端 | [3/3] `examples/offline_demo.py`（不需要真机） |
| 门禁：失败即阻断 | 任一步失败 → 退出码非 0 |

★ 设计要点
----------

1. **跑完全部关卡再汇总**，不遇到第一个错误就退出 ——
   CI 上「一次看到所有问题」比「修一个跑一次」高效得多。
2. **不需要真机**。真机相关测试用 skip 守卫，端到端走 `sim.py` 模拟设备。
   这样任何人 clone 后立刻能跑。
3. **耗时也报出来**。CI 变慢本身就是质量问题。

用法
----

    python ci/quality_gate.py                 # 全部关卡
    python ci/quality_gate.py --fast          # 跳过覆盖率（本地快速自测）
    python ci/quality_gate.py --only style    # 只跑某一关
    python ci/quality_gate.py --json out.json # 额外输出机器可读结果

退出码：0 = 全绿；1 = 有失败关卡。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

#: 覆盖率硬下限（与 .coveragerc 的 fail_under 保持一致）
COVERAGE_FLOOR = 70


class Stage:
    def __init__(self, key: str, title: str, cmd: Sequence[str],
                 cwd: str = ROOT, parse=None, timeout: int = 900):
        self.key = key
        self.title = title
        self.cmd = list(cmd)
        self.cwd = cwd
        self.parse = parse
        self.timeout = timeout
        self.rc: Optional[int] = None
        self.seconds = 0.0
        self.stdout = ''
        self.stderr = ''
        self.extra: Dict[str, Any] = {}

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def run(self) -> None:
        t0 = time.time()
        try:
            p = subprocess.run(self.cmd, cwd=self.cwd, capture_output=True,
                               timeout=self.timeout)
            self.rc = p.returncode
            self.stdout = p.stdout.decode('utf-8', 'replace')
            self.stderr = p.stderr.decode('utf-8', 'replace')
        except subprocess.TimeoutExpired:
            self.rc = 124
            self.stderr = f'超时（>{self.timeout}s）'
        self.seconds = time.time() - t0
        if self.parse:
            self.extra = self.parse(self.stdout + self.stderr) or {}


# ---------------------------------------------------------------- 解析器


def _parse_unittest(text: str) -> Dict[str, Any]:
    """从 unittest 输出里抓 'Ran N tests' 与 OK/FAILED。"""
    out: Dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('Ran ') and ' test' in line:
            out['tests'] = int(line.split()[1])
        if line.startswith('OK'):
            out['result'] = 'OK'
        elif line.startswith('FAILED'):
            out['result'] = 'FAILED'
    return out


def _parse_coverage(text: str) -> Dict[str, Any]:
    """从 coverage report 里抓 TOTAL 行的覆盖率。"""
    out: Dict[str, Any] = {}
    for line in text.splitlines():
        if line.startswith('TOTAL'):
            parts = line.split()
            for p in parts:
                if p.endswith('%'):
                    out['percent'] = float(p.rstrip('%'))
    return out


def _parse_demo(text: str) -> Dict[str, Any]:
    """检测离线端到端输出里的**异常痕迹**。

    ★ 为什么不能只看返回码：有些脚本内部 `try/except` 吞掉异常后
    仍以 0 退出。只看返回码会**漏检**这种「表面通过、实际炸了」的情况。
    所以额外数一下 Traceback。

    注意不要用「失败」这类中文词做判据 —— 演示脚本本身会输出
    「失败 0 条」之类的正常文案，会造成假阳性。
    """
    return {
        'tracebacks': text.count('Traceback (most recent call last)'),
        'error_lines': sum(1 for l in text.splitlines()
                           if l.strip().startswith(('Error:', 'ERROR:'))),
    }


# ---------------------------------------------------------------- 关卡定义


def build_stages(include_coverage: bool = True) -> List[Stage]:
    stages: List[Stage] = []

    stages.append(Stage(
        'style', '静态检查（零依赖）',
        [PY, os.path.join('ci', 'static_check.py'), '--quiet'],
        # 这里不再从输出里猜「有没有 error」—— 早期版本加过一个
        # `has_error` 字段，用了 `--quiet` 后它恒为 False，而且从没被读取，
        # 属于纯误导的死代码。**判定一律用返回码**（static_check 有 error 时返回 1）。
    ))

    if include_coverage:
        stages.append(Stage(
            'coverage', '单元测试 + 覆盖率',
            [PY, '-m', 'coverage', 'run', '-m', 'unittest',
             'discover', '-s', 'tests', '-t', 'tests', '-q'],
            parse=_parse_unittest, timeout=1200))
        stages.append(Stage(
            'coverage_report', '覆盖率门禁（≥%d%%）' % COVERAGE_FLOOR,
            [PY, '-m', 'coverage', 'report', '-m'],
            parse=_parse_coverage, timeout=300))
    else:
        stages.append(Stage(
            'tests', '单元测试',
            [PY, '-m', 'unittest', 'discover', '-s', 'tests', '-t', 'tests',
             '-q'],
            parse=_parse_unittest, timeout=900))

    stages.append(Stage(
        'e2e', '离线端到端（模拟设备，不需要真机）',
        [PY, os.path.join('examples', 'offline_demo.py')],
        parse=_parse_demo, timeout=600))

    return stages


# ---------------------------------------------------------------- 主流程


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='CI 质量门禁')
    ap.add_argument('--fast', action='store_true',
                    help='跳过覆盖率（本地快速自测）')
    ap.add_argument('--only', help='只跑指定关卡（key）')
    ap.add_argument('--json', help='把结果写到指定 JSON 文件')
    args = ap.parse_args(argv)

    stages = build_stages(include_coverage=not args.fast)
    if args.only:
        stages = [s for s in stages if s.key == args.only]
        if not stages:
            print(f'[失败] 未知关卡: {args.only}')
            return 1

    # 覆盖率门禁是独立一关，但依赖前一关产出的 .coverage；
    # 若前一关失败，跳过它避免报一个无意义的错。
    print('=' * 66)
    print('  ohauto CI · 质量门禁')
    print('=' * 66)

    total = len(stages)
    failed: List[Stage] = []

    for i, st in enumerate(stages, 1):
        print(f'\n[{i}/{total}] {st.title}')
        if st.key == 'coverage_report' \
                and any(s.key == 'coverage' and not s.ok for s in stages):
            st.rc = 1
            st.stderr = '跳过：上一关（单元测试）未通过'
            print('      跳过 —— 上一关未通过')
            failed.append(st)
            continue

        st.run()
        line = f'      {st.seconds:.1f}s'

        if st.key in ('coverage', 'tests'):
            n = st.extra.get('tests')
            res = st.extra.get('result', '?')
            print(f'      {n if n is not None else "?"} 项测试 → {res}{line}')
        elif st.key == 'coverage_report':
            pct = st.extra.get('percent')
            if pct is None:
                print(f'      无法解析覆盖率{line}')
            else:
                mark = '通过' if pct >= COVERAGE_FLOOR else '不达标'
                print(f'      覆盖率 {pct:.0f}%'
                      f'（门禁 ≥{COVERAGE_FLOOR}%）→ {mark}{line}')
        elif st.key == 'style':
            for l in st.stdout.splitlines():
                if 'error' in l or 'warning' in l or '结论' in l:
                    print(f'      {l.strip()}')
            print(f'      {line.strip()}')
        elif st.key == 'e2e':
            tb = st.extra.get('tracebacks', 0)
            note = f'，检出 {tb} 处 Traceback' if tb else ''
            print(f'      完成{note}{line}')

        # ★ e2e 的额外判据：退出码 0 但输出里有 Traceback 也算失败 ——
        #   防止「内部吞异常、表面通过」的情况漏过门禁。
        suspicious = (st.key == 'e2e'
                      and st.extra.get('tracebacks', 0) > 0)
        if not st.ok or suspicious:
            failed.append(st)

    # ------------------------------------------------------------ 汇总
    print()
    print('=' * 66)
    ok_style = all(s.ok for s in stages if s.key == 'style')
    cov_stage = next((s for s in stages if s.key == 'coverage_report'), None)
    cov_pct = cov_stage.extra.get('percent') if cov_stage else None
    if cov_pct is None and not args.fast:
        cov_stage2 = next((s for s in stages if s.key == 'coverage'), None)
        cov_pct = cov_stage2.extra.get('percent') if cov_stage2 else None

    print('  汇总')
    print(f'    静态检查 : {"通过" if ok_style else "不通过"}')
    tests_stage = next((s for s in stages
                        if s.key in ('coverage', 'tests')), None)
    if tests_stage:
        print(f'    单元测试 : {tests_stage.extra.get("tests", "?")} 项 '
              f'{"通过" if tests_stage.ok else "失败"}')
    if not args.fast:
        print(f'    覆盖率   : '
              f'{f"{cov_pct:.0f}%" if cov_pct is not None else "?"} '
              f'（门禁 ≥{COVERAGE_FLOOR}%）')
    e2e_stage = next((s for s in stages if s.key == 'e2e'), None)
    if e2e_stage:
        print(f'    端到端   : {"通过" if e2e_stage.ok else "失败"}')

    total_s = sum(s.seconds for s in stages)
    print()
    if failed:
        print(f'  结论：不通过（{len(failed)} 个关卡失败，'
              f'耗时 {total_s:.0f}s）')
        for s in failed:
            print(f'    - {s.title}  rc={s.rc}')
            if s.stderr:
                print(f'      {s.stderr.strip().splitlines()[-1][:160]}')
    else:
        print(f'  结论：全绿 ✓（耗时 {total_s:.0f}s）')
    print('=' * 66)

    if args.json:
        payload = {
            'ok': not failed,
            'coverage_percent': cov_pct,
            'coverage_floor': COVERAGE_FLOOR,
            'total_seconds': round(total_s, 1),
            'stages': [{'key': s.key, 'title': s.title, 'rc': s.rc,
                        'seconds': round(s.seconds, 1), **s.extra}
                       for s in stages],
        }
        # ★ 两步都要做，缺一个就会在「干净检出」时崩：
        #   ① 建父目录 —— 否则 `--json _out/x.json` 在 _out 不存在时
        #      抛 FileNotFoundError。这一步曾在所有检查跑完之后崩掉，
        #      **把已经算出来的 CI 结果整个掩盖掉**（用户只看到 traceback，
        #      不知道检查究竟过没过）。
        #   ② 写入失败只报警告，**不改退出码** —— 退出码必须反映
        #      「检查是否通过」，而不是「附加产物能不能写出去」。
        try:
            out_path = os.path.abspath(args.json)
            parent = os.path.dirname(out_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f'  结果已写入 {out_path}')
        except OSError as e:
            print(f'  [警告] 无法写入 {args.json}: {e}'
                  f'（不影响上面的检查结论）')

    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
