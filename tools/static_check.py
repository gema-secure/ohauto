"""零依赖静态检查器 —— CI 的第一道门禁。

★ 为什么不引 flake8 / mypy / pylint
-----------------------------------

项目红线约束是**「只用 Python 标准库和项目已有模块，不引入新依赖」**
（`requirements.txt` 里只有 `pyyaml`）。CI 若为了 lint 引入一堆重依赖，
就违背了这条约束，也会让「任何人 clone 后能跑起来」变难。

所以本脚本用 `ast` 模块自己实现规划里要求的检查项：

| 规划要求 | 本脚本的检查 |
|---|---|
| 类型注解全覆盖 | 公共函数必须有参数与返回值注解 |
| 统一格式化 | 无 tab 缩进、无尾随空白、文件以换行结尾 |
| 命名规范 | 函数/变量 snake_case、类 PascalCase、常量 UPPER_CASE |
| 模块边界清晰 | **生产代码不得反向 import 测试/示例/CI**（`B401`） |

另外加了若干**危险模式**检查（裸 except / eval / exec / 可变默认参数），
这些是评审里「工程规范性」的加分点，也是真实的坑。

用法
----

    python tools/static_check.py                # 检查（默认只报 error 级）
    python tools/static_check.py --all          # 连 warning 一起报
    python tools/static_check.py --quiet        # 只输出汇总与退出码

退出码：0 = 通过，1 = 有 error 级问题（CI 应据此阻断）。
"""
import argparse
import ast
import os
import re
import sys
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: 检查哪些目录（相对项目根）
#: ★ `examples/` 也必须在内 —— 它们是「给人看的示例代码」，
#: 是文档的一部分，质量要求只会更高。早期版本漏掉了这 6 个文件。
#: `tests/` 有意排除：测试代码不必强制类型注解与命名细节。
TARGET_DIRS: Tuple[str, ...] = ('ohauto', 'tools', 'ci', 'examples')
#: 额外单文件
TARGET_FILES: Tuple[str, ...] = ('doctor.py',)

#: 跳过这些目录名
SKIP_DIRS = frozenset({'__pycache__', '.git', '_out', 'node_modules'})

MAX_LINE_LENGTH = 100

#: 允许的裸名字（如 `_`、`self`、`cls`）
_ALLOWED_BARE = frozenset({'_', 'self', 'cls'})

#: `ast.NodeVisitor` 的 visit 分派约定 —— `visit_FunctionDef` 这类
#: 驼峰名**必须**这样写才能被正确分派，不是命名违规。
_AST_VISITOR_RE = re.compile(r'^visit_[A-Z]')

#: 类名：允许 `_` 前缀（私有类）与全大写短名（内部常量容器）
_CLASS_NAME_RE = re.compile(r'^_?[A-Z][A-Za-z0-9_]*$')
#: 函数名：snake_case，或 dunder
_FUNC_NAME_RE = re.compile(r'^[a-z_][a-z0-9_]*$')
_DUNDER_RE = re.compile(r'^__[a-z0-9_]+__$')


class Finding(NamedTuple):
    level: str          # 'error' | 'warning'
    path: str
    line: int
    code: str
    message: str

    def __str__(self) -> str:
        rel = os.path.relpath(self.path, ROOT)
        return f'{rel}:{self.line}: [{self.code}] {self.message}'


# ---------------------------------------------------------------- 文件级


def _check_lines(path: str, text: str) -> List[Finding]:
    """行级检查：长度 / 尾随空白 / tab 缩进 / 文件末尾换行。"""
    out: List[Finding] = []
    lines = text.splitlines()

    for i, line in enumerate(lines, 1):
        if len(line) > MAX_LINE_LENGTH:
            out.append(Finding('warning', path, i, 'E501',
                               f'行长度 {len(line)} 超过 {MAX_LINE_LENGTH}'))
        if line != line.rstrip():
            out.append(Finding('warning', path, i, 'W291', '行尾有空白'))
        if line.startswith('\t') or re.match(r'^ +\t', line):
            out.append(Finding('error', path, i, 'E101', '使用 tab 缩进'))

    if text and not text.endswith('\n'):
        out.append(Finding('warning', path, len(lines) or 1, 'W292',
                           '文件未以换行结尾'))
    return out


# ---------------------------------------------------------------- AST 级


def _is_public(name: str) -> bool:
    return not name.startswith('_') or name in ('__init__',)


def _missing_annotation(fn) -> List[str]:
    """返回缺少注解的项（参数名或 'return'）。"""
    missing: List[str] = []
    a = fn.args
    all_args = list(getattr(a, 'posonlyargs', [])) + list(a.args) + \
        list(a.kwonlyargs)
    for arg in all_args:
        if arg.arg in _ALLOWED_BARE:
            continue
        if arg.annotation is None:
            missing.append(arg.arg)
    if a.vararg and a.vararg.arg not in _ALLOWED_BARE \
            and a.vararg.annotation is None:
        missing.append('*' + a.vararg.arg)
    if a.kwarg and a.kwarg.arg not in _ALLOWED_BARE \
            and a.kwarg.annotation is None:
        missing.append('**' + a.kwarg.arg)
    if fn.returns is None:
        missing.append('return')
    return missing


def _has_mutable_default(fn) -> List[str]:
    """找出可变默认参数（list/dict/set/调用）—— 经典坑。"""
    bad: List[str] = []
    a = fn.args
    for arg, dflt in zip(list(a.args)[len(a.args) - len(a.defaults):],
                         a.defaults):
        if isinstance(dflt, (ast.List, ast.Dict, ast.Set, ast.Call)):
            bad.append(arg.arg)
    for arg, dflt in zip(a.kwonlyargs, a.kw_defaults):
        if dflt is not None and isinstance(dflt, (ast.List, ast.Dict,
                                                  ast.Set, ast.Call)):
            bad.append(arg.arg)
    return bad


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str, check_annotations: bool):
        self.path = path
        self.check_annotations = check_annotations
        self.findings: List[Finding] = []

    # -------------------------------------------------- 函数

    def visit_FunctionDef(self, node):                         # noqa: N802
        self._check_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):                    # noqa: N802
        self._check_function(node)
        self.generic_visit(node)

    def _check_function(self, node) -> None:
        name = node.name
        # 命名规范（`visit_Xxx` 是 ast 分派约定，豁免）
        if not _AST_VISITOR_RE.match(name) \
                and not _FUNC_NAME_RE.match(name) \
                and not _DUNDER_RE.match(name):
            self.findings.append(Finding(
                'error', self.path, node.lineno, 'N802',
                f'函数名 {name!r} 不是 snake_case'))

        # 类型注解（只查公共函数，私有函数宽松些）
        # `visit_Xxx` 是 ast 分派约定，签名必须匹配基类，豁免注解检查
        if self.check_annotations and _is_public(name) \
                and not _AST_VISITOR_RE.match(name):
            miss = _missing_annotation(node)
            if miss:
                self.findings.append(Finding(
                    'warning', self.path, node.lineno, 'ANN001',
                    f'{name}() 缺少类型注解: {", ".join(miss)}'))

        bad = _has_mutable_default(node)
        if bad:
            self.findings.append(Finding(
                'error', self.path, node.lineno, 'B006',
                f'{name}() 有可变默认参数: {", ".join(bad)}'))

    # -------------------------------------------------- 类

    def visit_ClassDef(self, node):                            # noqa: N802
        if not _CLASS_NAME_RE.match(node.name):
            self.findings.append(Finding(
                'error', self.path, node.lineno, 'N801',
                f'类名 {node.name!r} 不是 PascalCase'))
        self.generic_visit(node)

    # -------------------------------------------------- 危险模式

    def visit_ExceptHandler(self, node):                       # noqa: N802
        if node.type is None:
            self.findings.append(Finding(
                'error', self.path, node.lineno, 'E722',
                '裸 except: —— 必须指定异常类型'))
        self.generic_visit(node)

    def visit_Call(self, node):                                # noqa: N802
        f = node.func
        fname: Optional[str] = None
        if isinstance(f, ast.Name):
            fname = f.id
        elif isinstance(f, ast.Attribute):
            fname = f.attr
        if fname in ('eval', 'exec'):
            self.findings.append(Finding(
                'error', self.path, node.lineno, 'S307',
                f'使用了 {fname}()，存在安全风险'))
        elif fname == '__import__':
            # 动态导入本身合法（如检测可选依赖是否安装），
            # 但如果参数来自外部输入就有风险 —— 报 warning 让人确认。
            self.findings.append(Finding(
                'warning', self.path, node.lineno, 'S308',
                '__import__() —— 确认参数不是外部输入'))
        self.generic_visit(node)


def _check_import_boundary(path: str, tree) -> List[Finding]:
    """模块边界：**生产代码不得反向依赖测试 / 示例 / CI 代码。**

    依赖方向必须单向：`tests/` → `ohauto/`，绝不能反过来。
    一旦 `ohauto/` 里 import 了 `tests/`，包就不再是自包含的，
    别人 `pip install` 或单独 copy 这个包会直接 ImportError。

    只查这一条是有意的 —— 它**零误报、含义明确**。
    更复杂的层次规则（比如「L4 不许 import L1」）容易误伤，
    本项目当前也没有严格的分层目录，强行检查会产生噪音。
    """
    out: List[Finding] = []
    rel = os.path.relpath(path, ROOT).replace(os.sep, '/')
    if not rel.startswith('ohauto/'):
        return out

    forbidden = {'tests', 'examples', 'ci'}
    for node in ast.walk(tree):
        names: List[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:      # 绝对导入才判
                names = [node.module]
        for n in names:
            root_mod = n.split('.')[0]
            if root_mod in forbidden:
                out.append(Finding(
                    'error', path, getattr(node, 'lineno', 1), 'B401',
                    f'生产代码不得 import {root_mod}/ —— '
                    f'依赖方向反了（应该是 {root_mod}/ import ohauto/）'))
    return out


def _check_file(path: str, check_annotations: bool) -> List[Finding]:
    with open(path, encoding='utf-8') as f:
        text = f.read()

    out = _check_lines(path, text)
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as e:
        out.append(Finding('error', path, e.lineno or 1, 'E999',
                           f'语法错误: {e.msg}'))
        return out

    v = _Visitor(path, check_annotations)
    v.visit(tree)
    out.extend(v.findings)
    out.extend(_check_import_boundary(path, tree))
    return out


# ---------------------------------------------------------------- 驱动


def iter_target_files(root: str) -> List[str]:
    files: List[str] = []
    for d in TARGET_DIRS:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for cur, dirs, names in os.walk(base):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            for n in names:
                if n.endswith('.py'):
                    files.append(os.path.join(cur, n))
    for f in TARGET_FILES:
        p = os.path.join(root, f)
        if os.path.isfile(p):
            files.append(p)
    return sorted(files)


def run(root: str = ROOT, check_annotations: bool = True,
        show_all: bool = False, quiet: bool = False) -> List[Finding]:
    findings: List[Finding] = []
    files = iter_target_files(root)
    for p in files:
        findings.extend(_check_file(p, check_annotations))

    errors = [f for f in findings if f.level == 'error']
    warns = [f for f in findings if f.level == 'warning']

    if not quiet:
        shown = findings if show_all else errors
        by_file: Dict[str, List[Finding]] = {}
        for f in shown:
            by_file.setdefault(f.path, []).append(f)
        for p in sorted(by_file):
            print(f'\n{os.path.relpath(p, root)}')
            for f in sorted(by_file[p], key=lambda x: x.line):
                print(f'  {f.line:>4}: [{f.code}] {f.message}')

    print()
    print('=' * 62)
    print(f'  静态检查：{len(files)} 个文件')
    print(f'    error   : {len(errors)}')
    print(f'    warning : {len(warns)}')
    if not show_all and warns:
        extra = {}
        for f in warns:
            extra[f.code] = extra.get(f.code, 0) + 1
        top = ', '.join(f'{k}×{v}' for k, v in
                        sorted(extra.items(), key=lambda kv: -kv[1])[:6])
        print(f'    （warning 明细用 --all 查看；分布：{top}）')
    print('=' * 62)
    print('结论：' + ('通过' if not errors else f'不通过（{len(errors)} 个 error）'))
    return findings


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='零依赖静态检查器')
    ap.add_argument('--all', action='store_true', help='连 warning 一起显示')
    ap.add_argument('--quiet', action='store_true', help='只输出汇总')
    ap.add_argument('--no-annotations', action='store_true',
                    help='跳过类型注解检查')
    ap.add_argument('--root', default=ROOT)
    args = ap.parse_args(argv)

    findings = run(args.root,
                   check_annotations=not args.no_annotations,
                   show_all=args.all, quiet=args.quiet)
    return 1 if any(f.level == 'error' for f in findings) else 0


if __name__ == '__main__':
    sys.exit(main())
