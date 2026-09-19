"""
L4 应用层 —— 自动探索与页面状态图
=================================

让 Driver 自己「点着玩」，把应用的可达页面与跳转关系摸出来，
形成页面状态图，并把探索轨迹沉淀成可重放的回归脚本。

安全设计（真机上非常关键）：
    探索会真的点击 UI，因此内置**危险控件黑名单**。默认拦截含
    「删除 / 支付 / 退出 / 注销 / 卸载 / 格式化」等语义的控件，
    避免自动探索造成不可逆影响。可通过 allow_dangerous=True 放开。

    from ohauto.explorer import Explorer
    ex = Explorer(driver)
    graph = ex.explore(max_pages=8, max_actions_per_page=6)
    ex.save_graph('graph.json')
    print(ex.to_mermaid())
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .driver import Driver, DriverError
from .layout import LayoutNode, flatten


# ---------------------------------------------------------------- 安全策略

DEFAULT_DENY_PATTERNS = [
    r'删除', r'移除', r'清除', r'清空', r'格式化', r'重置',
    r'支付', r'付款', r'购买', r'下单', r'充值', r'转账',
    r'退出登录', r'注销', r'登出', r'解绑',
    r'卸载', r'停止', r'关闭账号', r'取消订单',
    r'delete', r'remove', r'pay', r'purchase', r'logout',
    r'sign\s*out', r'uninstall', r'reset', r'format',
]


@dataclass
class SafetyPolicy:
    """探索期安全策略。"""
    deny_patterns: List[str] = field(default_factory=lambda: list(DEFAULT_DENY_PATTERNS))
    allow_dangerous: bool = False
    max_text_len: int = 40          # 排除超长文本块（多为正文而非按钮）

    def is_dangerous(self, node: LayoutNode) -> Tuple[bool, str]:
        if self.allow_dangerous:
            return False, ''
        blob = f'{node.text} {node.id} {node.descr} {node.hint}'
        for p in self.deny_patterns:
            if re.search(p, blob, re.I):
                return True, p
        return False, ''

    def is_clickable_candidate(self, node: LayoutNode) -> Tuple[bool, str]:
        if not node.visible:
            return False, 'invisible'
        if not node.enabled:
            return False, 'disabled'
        if node.rect.width < 20 or node.rect.height < 20:
            return False, 'too-small'
        if node.type in ('Text', 'Image', 'Divider', 'Blank', 'Stack'):
            if not node.clickable:
                return False, f'passive-{node.type}'
        if len(node.text) > self.max_text_len:
            return False, 'text-too-long'
        bad, pat = self.is_dangerous(node)
        if bad:
            return False, f'dangerous({pat})'
        if not (node.clickable or node.is_interactive()):
            return False, 'not-interactive'
        return True, ''


# ---------------------------------------------------------------- 状态图

@dataclass
class PageState:
    """一个页面状态（用控件树签名去重）。"""
    sid: str
    signature: str
    title: str = ''
    first_seen: float = field(default_factory=time.time)
    visits: int = 0
    screenshot: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {'id': self.sid, 'title': self.title, 'visits': self.visits,
                'screenshot': self.screenshot}


@dataclass
class Transition:
    """一条跳转边：在某页点了某控件，到了另一页。"""
    src: str
    dst: str
    control: str
    control_spec: Dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        d = {'src': self.src, 'dst': self.dst, 'control': self.control, 'ok': self.ok}
        if self.control_spec:
            d['spec'] = self.control_spec
        if self.note:
            d['note'] = self.note
        return d


class StateGraph:
    """页面状态图。"""

    def __init__(self):
        self.states: Dict[str, PageState] = {}
        self.transitions: List[Transition] = []
        self._auto = 0

    def add_state(self, signature: str, title: str = '',
                  screenshot: Optional[str] = None) -> PageState:
        key = _sig_hash(signature)
        if key in self.states:
            self.states[key].visits += 1
            return self.states[key]
        self._auto += 1
        st = PageState(sid=f'S{self._auto}', signature=signature,
                       title=title or f'页面{self._auto}', screenshot=screenshot)
        st.visits = 1
        self.states[key] = st
        return st

    def add_edge(self, src: str, dst: str, control: str,
                 spec: Optional[Dict[str, Any]] = None, ok: bool = True,
                 note: str = '') -> None:
        for t in self.transitions:
            if t.src == src and t.dst == dst and t.control == control:
                return
        self.transitions.append(Transition(src, dst, control, spec or {}, ok, note))

    def to_dict(self) -> Dict[str, Any]:
        return {
            'states': [s.to_dict() for s in self.states.values()],
            'transitions': [t.to_dict() for t in self.transitions],
        }


def _sig_hash(sig: str) -> str:
    import hashlib
    return hashlib.md5(sig.encode('utf-8', 'replace')).hexdigest()[:12]


# ---------------------------------------------------------------- 探索器

class Explorer:
    """基于状态图的广度优先自动探索。"""

    def __init__(self, driver: Driver, policy: Optional[SafetyPolicy] = None,
                 artifact_dir: Optional[str] = None, verbose: bool = True):
        self.driver = driver
        self.policy = policy or SafetyPolicy()
        self.graph = StateGraph()
        self.artifact_dir = artifact_dir or getattr(driver, 'artifact_dir', None)
        self.verbose = verbose
        self.skipped: List[Dict[str, Any]] = []

    def log(self, msg: str) -> None:
        if self.verbose:
            print(f'[explore] {msg}')

    # ------------------------------------------------------------ 页面

    def _page_signature(self) -> str:
        """用「可见控件的类型/id/text 序列」作为页面指纹。"""
        root = self.driver.refresh()
        parts = []
        for n in flatten(root, only_visible=True):
            if n.rect.area <= 0:
                continue
            parts.append(f'{n.type}|{n.id}|{n.text}|{n.rect.width}x{n.rect.height}')
        return ';'.join(parts)

    def _page_title(self) -> str:
        """猜测页面标题：取顶部区域字号最大/最靠上的非空文本。"""
        try:
            root = self.driver.root
            cands = []
            for n in root.walk():
                if n.visible and n.text and len(n.text) <= 20 and n.type in (
                        'Text', 'Title', 'NavigationTitle', 'NavDestinationTitle'):
                    cands.append((n.rect.top, -n.rect.area, n.text))
            if cands:
                cands.sort()
                return cands[0][2]
        except Exception:
            pass
        return ''

    def _candidates(self) -> List[LayoutNode]:
        """当前页面的可点击候选控件。"""
        out = []
        seen: Set[Tuple[int, int, str]] = set()
        for n in flatten(self.driver.refresh(), only_visible=True):
            ok, why = self.policy.is_clickable_candidate(n)
            if not ok:
                if why not in ('invisible', 'passive-Text'):
                    self.skipped.append({'label': n.label, 'reason': why})
                continue
            key = (n.rect.center[0] // 8, n.rect.center[1] // 8, n.type)
            if key in seen:
                continue
            seen.add(key)
            out.append(n)
        return out

    def _spec_of(self, node: LayoutNode) -> Dict[str, Any]:
        """给控件生成优先稳定的匹配器规格：id 优先，其次 text，再次 label。"""
        if node.id:
            return {'id': node.id}
        if node.text:
            return {'text': node.text}
        if node.descr:
            return {'descr': node.descr}
        return {'type': node.type, 'nth': 0}

    # ------------------------------------------------------------ 主循环

    def explore(self, max_pages: int = 8, max_actions_per_page: int = 6,
                max_seconds: int = 300, return_back: bool = True
                ) -> StateGraph:
        """从当前页面开始广度优先探索。

        max_pages:            最多发现多少个页面状态
        max_actions_per_page: 每页最多尝试多少个控件
        max_seconds:          总时间上限，防止失控
        return_back:          每次跳转后尝试返回，维持 BFS 层次
        """
        t0 = time.time()
        sig = self._page_signature()
        start = self.graph.add_state(sig, self._page_title())
        self.log(f'起始页面 {start.sid} 「{start.title}」')

        queue: deque = deque([start])
        visited_sigs: Set[str] = {_sig_hash(sig)}

        while queue and len(self.graph.states) < max_pages:
            if time.time() - t0 > max_seconds:
                self.log(f'达到时间上限 {max_seconds}s，停止探索')
                break

            cur = queue.popleft()
            self.log(f'--- 探索 {cur.sid}「{cur.title}」 (第 {cur.visits} 次访问)')

            try:
                self.driver.refresh()
            except DriverError as e:
                self.log(f'刷新控件树失败: {e}')
                continue

            cands = self._candidates()[:max_actions_per_page]
            self.log(f'候选控件 {len(cands)} 个')

            for node in cands:
                if len(self.graph.states) >= max_pages:
                    break
                if time.time() - t0 > max_seconds:
                    break

                spec = self._spec_of(node)
                label = f'{node.type}:{node.label}'

                shot = None
                if self.artifact_dir:
                    shot = os.path.join(self.artifact_dir,
                                        f'explore_{cur.sid}_{len(self.graph.transitions)}.png')

                try:
                    self.driver.tap(node, post_idle=True)
                    if shot:
                        self.driver.screenshot(shot)
                except Exception as e:
                    self.graph.add_edge(cur.sid, cur.sid, label, spec,
                                        ok=False, note=str(e)[:120])
                    self.log(f'  点击「{node.label}」失败: {str(e)[:80]}')
                    continue

                try:
                    new_sig = self._page_signature()
                except DriverError as e:
                    self.log(f'  跳转后无法读取控件树: {str(e)[:70]}')
                    new_sig = sig

                h = _sig_hash(new_sig)
                if h == _sig_hash(cur.signature):
                    self.graph.add_edge(cur.sid, cur.sid, label, spec,
                                        note='页面未变化')
                    self.log(f'  点击「{node.label}」-> 页面无变化')
                else:
                    st = self.graph.states.get(h)
                    fresh = st is None
                    if fresh:
                        st = self.graph.add_state(new_sig, self._page_title(), shot)
                    self.graph.add_edge(cur.sid, st.sid, label, spec)      # type: ignore[union-attr]
                    self.log(f'  点击「{node.label}」-> {st.sid}「{st.title}」'  # type: ignore[union-attr]
                             f'{"（新页面）" if fresh else "（已知页面）"}')
                    if fresh:
                        queue.append(st)                                   # type: ignore[arg-type]

                if return_back:
                    try:
                        self.driver.back()
                        back_sig = self._page_signature()
                        if _sig_hash(back_sig) != _sig_hash(cur.signature):
                            # 返回没回到原页，重新定位到当前页
                            self.log('  返回后页面不符，尝试重新进入')
                            self.driver.start(wait=True)
                    except Exception as e:
                        self.log(f'  返回失败: {str(e)[:70]}')
                        try:
                            self.driver.start(wait=True)
                        except Exception:
                            pass

        self.log(f'探索结束：发现 {len(self.graph.states)} 个页面、'
                 f'{len(self.graph.transitions)} 条跳转，耗时 {time.time() - t0:.1f}s')
        return self.graph

    # ------------------------------------------------------------ 导出

    def to_mermaid(self) -> str:
        """导出 Mermaid 状态图 —— 可直接贴进文档或 Markdown 渲染。"""
        states = list(self.graph.states.values())
        if not states:
            return 'stateDiagram-v2\n    [*]'

        lines = ['stateDiagram-v2']

        # 状态声明。标题里的冒号会破坏 Mermaid 语法，替换掉。
        for s in states:
            title = (s.title or s.sid).replace(':', '：').replace('\n', ' ')
            lines.append(f'    {s.sid}: {title}')

        # 起始状态
        lines.append(f'    [*] --> {states[0].sid}')

        # 跳转边。自环不画（页面无变化的点击对状态图无信息量）。
        for t in self.graph.transitions:
            if t.src == t.dst:
                continue
            arrow = '-->' if t.ok else '-x->'
            label = t.control.replace(':', '：').replace('\n', ' ')[:24]
            lines.append(f'    {t.src} {arrow} {t.dst}: {label}')

        return '\n'.join(lines)

    def save_graph(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        payload = self.graph.to_dict()
        payload['mermaid'] = self.to_mermaid()
        payload['skipped_controls'] = _dedup_skipped(self.skipped)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        self.log(f'状态图已保存: {path}')
        return path

    def generate_case(self, path: str, name: str = '自动探索轨迹') -> str:
        """把探索轨迹里「成功的页面跳转」串成一条可重放用例。"""
        from .action import dump_case
        steps: List[Dict[str, Any]] = [{'start': True}]
        for t in self.graph.transitions:
            if not t.ok or t.src == t.dst or not t.control_spec:
                continue
            steps.append({'tap': t.control_spec})
            steps.append({'waitIdle': True})
        case = {'name': name, 'bundle': self.driver.bundle,
                'ability': self.driver.ability, 'steps': steps}
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(dump_case(case))
        self.log(f'用例已生成: {path}（{len(steps)} 步）')
        return path


def _dedup_skipped(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for it in items:
        k = (it.get('label'), it.get('reason'))
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out
