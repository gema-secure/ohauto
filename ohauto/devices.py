"""跨形态测试的形态档案（跨形态）。

## 这个模块解决什么

UI 自动化在换一种设备形态（手机 → 折叠展开 → 平板）后，控件的
**坐标体系、安全区、圆角、挖孔位置全部变了**。同一套用例要能在这几种
形态下跑，就必须先把「每种形态长什么样」这件事**数据化**。

## 数据从哪来（关键决策）

形态数据有两个来源，优先级如下：

1. **DevEco 的设备产品配置**（`productConfig.json`）—— 官方数据，最权威。
   实测本机路径：
   `C:\\Users\\<user>\\AppData\\Local\\Huawei\\Emulator26.0\\productConfig.json`
   里面直接给出每种设备的**内外双屏分辨率、DPI、挖孔路径、圆角半径**，
   折叠态是配置好的（`outerScreenWidth/Height`）。
2. **设备实时 dump**（`hidumper`）—— 用来核对当前实际生效的形态。

> **为什么不只靠 hidumper**：实测真机（直板机）上 `FoldStatus` / `CUTOUT INFO`
> 这些字段**根本不存在**，且真机没有 `wm` 命令、改不了分辨率。
> 而 DevEco 的产品配置里连三折屏（Mate XT）的形态都有。
> 所以「配置当档案、dump 当校验」才是可靠的分工。

## 数据来源与设计取舍

最初的设计稿写「用 `hidumper -s DisplayManagerService -a -a` 采形态档案」。
实测该命令**在真机上报 arguments are illegal**，且真机上根本没有折叠字段。
本模块改用「产品配置 + 正确 hidumper 命令核对」两条腿走路，
真实数据的来源与实测结论见 `实测-跨形态测试.md`。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------- 挖孔路径解析

# 产品配置里的挖孔是 **SVG path 的极简子集**，实测只有两种指令：
#   M<x> <y>          移动到起点
#   L<x> <y>          直线到
#   v <dy>            相对纵向移动
#   h <dx>            相对横向移动
#   Z / z             闭合
# 例：'M624 42 L694 42 v 70 h -70 Z'  →  矩形 (624,42)-(694,112)
# ⚠️ 指令之间可能有空格（'M624 42 L694 42 v 70 h -70 Z' 里 L/v/h 前都有空格），
# 所以正则必须允许前导空白，否则第二次 match 就断在空格上了。
_CUTOUT_TOKEN_RE = re.compile(
    r'\s*([MLvVhHzZ])\s*(-?\d+(?:\.\d+)?)?(?:\s+(-?\d+(?:\.\d+)?))?')
_NUM_RE = re.compile(r'-?\d+(?:\.\d+)?')


def is_no_cutout_placeholder(path: Any) -> bool:
    """判断这条挖孔 path 是不是「本设备没有挖孔」的占位符。

    华为的配置里，**无挖孔设备不是省略字段，而是给一条零尺寸 path**：

        'M0 0 L0 0 v 0 h -0 Z'

    （MatePad 系列实测如此。）它和「字段缺失」语义相同，都表示无孔。
    不把它识别出来，就会被当成「解析失败」而误报一条 caveat。
    """
    if not isinstance(path, str):
        return False
    nums = _NUM_RE.findall(path)
    return bool(nums) and all(abs(float(n)) < 1e-9 for n in nums)


def parse_cutout_path(path: str) -> Optional[Tuple[int, int, int, int]]:
    """把挖孔 SVG path 解析成包围盒 `(x, y, w, h)`。

    只支持产品配置里实际出现的那几条指令（M/L/v/h/Z）。
    **解析不出来就返回 None，绝不猜** —— 挖孔坐标一旦猜错，
    下游「控件是否被挖孔遮挡」的判定会全盘错位。

    >>> parse_cutout_path('M624 42 L694 42 v 70 h -70 Z')
    (624, 42, 70, 70)
    """
    if not path or not isinstance(path, str):
        return None

    xs: List[float] = []
    ys: List[float] = []
    cx = cy = 0.0
    saw_move = False

    # 逐个 token 走，维护当前点
    i = 0
    s = path.strip()
    while i < len(s):
        m = _CUTOUT_TOKEN_RE.match(s, i)
        if not m:
            # 遇到不认识的指令就放弃（宁可 None，不要错值）
            return None
        cmd = m.group(1)
        nums = [float(g) for g in (m.group(2), m.group(3)) if g is not None]
        upper = cmd.upper()

        if upper == 'M':
            if len(nums) < 2:
                return None
            cx, cy = nums[0], nums[1]
            saw_move = True
        elif upper == 'L':
            if len(nums) < 2:
                return None
            cx, cy = nums[0], nums[1]
        elif upper == 'V':
            if not nums:
                return None
            # 小写 v = 相对，大写 V = 绝对
            cy = cy + nums[0] if cmd.islower() else nums[0]
        elif upper == 'H':
            if not nums:
                return None
            cx = cx + nums[0] if cmd.islower() else nums[0]
        elif upper == 'Z':
            pass
        else:                                   # pragma: no cover - 防御
            return None

        if saw_move:
            xs.append(cx)
            ys.append(cy)
        i = m.end()

    if not xs or not ys:
        return None

    x0, y0 = min(xs), min(ys)
    x1, y1 = max(xs), max(ys)
    w = x1 - x0
    h = y1 - y0
    if w <= 0 or h <= 0:
        return None
    return int(round(x0)), int(round(y0)), int(round(w)), int(round(h))


def _parse_radii(raw: Any) -> Tuple[float, ...]:
    """圆角半径。产品配置里可能是 `"23,22"` 或 `"54.6"` 或 None。"""
    if raw is None:
        return ()
    if isinstance(raw, (int, float)):
        return (float(raw),)
    nums = _NUM_RE.findall(str(raw))
    return tuple(float(n) for n in nums)


# ---------------------------------------------------------------- 形态档案


@dataclass
class FormProfile:
    """一种设备形态的完整档案。

    这是 `switch_form(profile)` 的输入，也是跨形态差异比对的基准。
    """

    name: str                       # 档案名，如 'Mate_X7_unfolded'
    device: str                     # 设备型号，如 'Mate X7'
    kind: str                       # 'phone' / 'foldable_unfolded' /
                                    # 'foldable_folded' / 'tablet' / ...
    width: int                      # 逻辑宽（px）
    height: int                     # 逻辑高（px）
    density: int = 0                # DPI
    diagonal: float = 0.0           # 屏幕对角线（inch）
    corner_radius: Tuple[float, ...] = ()   # 圆角半径（可能有多个角）
    cutouts: Tuple[Tuple[int, int, int, int], ...] = ()  # 挖孔包围盒
    status_bar_h: int = 0           # 顶部安全区（实测填充）
    nav_bar_h: int = 0              # 底部安全区（实测填充）
    fold_status: Optional[str] = None       # EXPANDED / FOLDED / DOUBLE；
                                            # 直板机为 None
    source: str = 'product_config'  # 数据来源，便于追溯
    extra: Dict[str, Any] = field(default_factory=dict)

    #: 可信度备注。空 = 完全来自配置；有值 = 该形态有需要复核的地方。
    #: 典型场景：三折叠（Mate XT）在配置里**只有一个挖孔坐标**，折叠态与
    #: 双屏态直接复用了展开态的孔 —— 物理上未必对，但配置原文如此，
    #: 我们**不造数据**，只如实标注，交给实测复核。
    caveats: Tuple[str, ...] = ()

    # -------------------------------------------------- 便捷视图

    @property
    def safe_area(self) -> Tuple[int, int, int, int]:
        """可用内容区 `(left, top, right, bottom)`，已扣掉状态栏与导航栏。

        ⚠️ **静态配置里没有安全区信息**，所以 `status_bar_h` / `nav_bar_h`
        默认都是 0，此时 `safe_area` 就等于整屏 —— 这是**「还没测过」而不是
        「没有安全区」**。真实值必须由 hidumper 实测填充：

            hidumper -s WindowManagerService -a '-a'

        见 `ohauto/tests/fixtures/crossform/displaymanager_*.txt` 与
        `实测-跨形态测试.md` 第二节。

        注意这里给的是**矩形近似**。异形屏（挖孔/圆角）的精确避让要靠
        `cutouts` 与 `corner_radius`，见 `rect_hits_cutout()`。
        """
        return (0, self.status_bar_h, self.width,
                max(self.status_bar_h, self.height - self.nav_bar_h))

    @property
    def has_measured_safe_area(self) -> bool:
        """安全区是否已经由实测填充过（False = 还用着整屏近似值）。"""
        return self.status_bar_h > 0 or self.nav_bar_h > 0

    @property
    def is_folded(self) -> bool:
        """是否是「折起来的那个形态」。

        判据用 `kind` 后缀而不是 `fold_status` 字段 —— 因为 `kind` 是按
        设备类型前缀拼出来的，对 Foldable / WideFold / TripleFold /
        2in1_Foldable 四种折叠设备一致生效，不会漏判。
        """
        return self.kind.endswith('_folded')

    @property
    def is_double(self) -> bool:
        """是否是三折叠设备的「双屏中间态」（只有 Mate XT 这类有）。"""
        return self.kind.endswith('_double')

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['corner_radius'] = list(self.corner_radius)
        d['cutouts'] = [list(c) for c in self.cutouts]
        d['caveats'] = list(self.caveats)
        d['safe_area'] = list(self.safe_area)
        d['is_folded'] = self.is_folded
        d['has_measured_safe_area'] = self.has_measured_safe_area
        return d

    # -------------------------------------------------- 判定helper

    def rect_within_screen(self, rect: Sequence[int]) -> bool:
        """控件是否完全在屏幕内。`rect` = (left, top, right, bottom)。"""
        if len(rect) != 4:
            return False
        l, t, r, b = (int(v) for v in rect)
        return l >= 0 and t >= 0 and r <= self.width and b <= self.height

    def rect_hits_cutout(self, rect: Sequence[int]) -> bool:
        """控件是否与挖孔区相交 —— 相交意味着**看得见但点不到**。

        这是「不可达」这一类跨形态缺陷的核心判据：
        元素渲染出来了（在屏幕内），但中心点落在挖孔/异形区里，
        `uiInput` 点下去会被系统吃掉。
        """
        if len(rect) != 4 or not self.cutouts:
            return False
        l, t, r, b = (int(v) for v in rect)
        for cx, cy, cw, ch in self.cutouts:
            if l < cx + cw and r > cx and t < cy + ch and b > cy:
                return True
        return False

    def center_hits_cutout(self, rect: Sequence[int]) -> bool:
        """控件**中心点**是否落在挖孔里 —— 比整块相交更严格的判据。

        点击用的是中心点，所以中心点被挡才是真正「点不到」。
        """
        if len(rect) != 4 or not self.cutouts:
            return False
        l, t, r, b = (int(v) for v in rect)
        cx_ = (l + r) // 2
        cy_ = (t + b) // 2
        for x, y, w, h in self.cutouts:
            if x <= cx_ <= x + w and y <= cy_ <= y + h:
                return True
        return False

    def rect_overflows_parent(self, child: Sequence[int],
                              parent: Sequence[int]) -> bool:
        """子控件是否溢出父容器 bounds（跨形态常见的「布局溢出」）。"""
        if len(child) != 4 or len(parent) != 4:
            return False
        cl, ct, cr, cb = (int(v) for v in child)
        pl, pt, pr, pb = (int(v) for v in parent)
        return cl < pl or ct < pt or cr > pr or cb > pb


# ---------------------------------------------------------------- 加载器


#: DevEco 把设备产品配置放在这几个位置（随版本而异，逐个试）
PRODUCT_CONFIG_PATHS: Tuple[str, ...] = (
    os.path.expandvars(
        r'%LOCALAPPDATA%\Huawei\Emulator26.0\productConfig.json'),
    os.path.expandvars(
        r'%LOCALAPPDATA%\Huawei\Emulator\productConfig.json'),
    os.path.expandvars(
        r'%LOCALAPPDATA%\Huawei\DevEcoStudio26.0\productConfig.json'),
)

#: 设备类型 → 我们的 kind 前缀
_KIND_MAP = {
    'Phone': 'phone',
    'Foldable': 'foldable',
    'WideFold': 'wide_fold',
    'TripleFold': 'triple_fold',
    'Tablet': 'tablet',
    '2in1': 'pc',
    '2in1_Foldable': 'pc_foldable',
    'TV': 'tv',
    'Wearable': 'wearable',
    'WearableKid': 'wearable',
    'Car': 'car',
}


def find_product_config(explicit: Optional[str] = None) -> Optional[str]:
    """找到 DevEco 的设备产品配置文件。找不到返回 None（不猜、不造）。"""
    if explicit:
        return explicit if Path(explicit).exists() else None
    for p in PRODUCT_CONFIG_PATHS:
        if p and Path(p).exists():
            return p
    return None


def load_product_config(path: Optional[str] = None) -> Dict[str, Any]:
    """读产品配置原始 JSON。找不到文件返回空 dict（调用方自行降级）。"""
    real = find_product_config(path)
    if not real:
        return {}
    try:
        with open(real, encoding='utf-8') as f:
            return json.load(f)
    except Exception:                                  # noqa: BLE001
        return {}


#: 形态档的三档。折叠屏在配置里只有三组分辨率字段，
#: 分别对应展开 / 折叠 / （三折叠的）双屏中间态。
FORM_EXPANDED = 'EXPANDED'
FORM_FOLDED = 'FOLDED'
FORM_DOUBLE = 'DOUBLE'          # 仅 Mate XT 这类三折叠设备有


def _profile_from_device(kind_key: str, dev: Dict[str, Any],
                         form: Optional[str] = None) -> Optional[FormProfile]:
    """把产品配置里的一个设备（或它的某个折叠形态）转成形态档案。

    Parameters
    ----------
    form
        `FORM_EXPANDED` / `FORM_FOLDED` / `FORM_DOUBLE`。None 表示
        「这台设备只有一个形态」（普通 Phone / Tablet / TV …）。

    Notes
    -----
    折叠屏的挖孔**不是一个设备的两个孔，而是两个形态各自的孔**：
      - `oneCutoutPath` → 展开（内屏）态的孔，靠右
      - `twoCutoutPath` → 折叠（外屏）态的孔，居中
    先前版本把 `twoCutoutPath` 当作「第二个挖孔」一并塞进展开态，
    这是错的。这里按形态分别取。
    """
    base_kind = _KIND_MAP.get(kind_key, kind_key.lower())
    multi_form = 'outerScreenWidth' in dev

    if form == FORM_FOLDED and multi_form:
        w = dev.get('outerScreenWidth')
        h = dev.get('outerScreenHeight')
        diag = dev.get('outerScreenDiagonal') or 0
        cutout_raw = dev.get('twoCutoutPath') or dev.get('oneCutoutPath')
        kind = f'{base_kind}_folded'
        name = f"{dev['name']}_folded"
    elif form == FORM_DOUBLE and 'outerDoubleScreenWidth' in dev:
        w = dev.get('outerDoubleScreenWidth')
        h = dev.get('outerDoubleScreenHeight')
        diag = dev.get('outerDoubleScreenDiagonal') or 0
        cutout_raw = dev.get('oneCutoutPath')
        kind = f'{base_kind}_double'
        name = f"{dev['name']}_double"
    else:
        w = dev.get('screenWidth')
        h = dev.get('screenHeight')
        diag = dev.get('screenDiagonal') or 0
        cutout_raw = dev.get('oneCutoutPath')
        kind = base_kind
        name = f"{dev['name']}" + ('_unfolded' if multi_form else '')

    if form is None:
        fold_status = None
    else:
        fold_status = form

    # 分辨率缺失或非法就跳过，不造数据
    try:
        wi, hi = int(w), int(h)
    except (TypeError, ValueError):
        return None
    if wi <= 0 or hi <= 0:
        return None

    cutob = parse_cutout_path(cutout_raw) if isinstance(cutout_raw, str) else None
    radii = _parse_radii(dev.get('device.radius'))
    if 'realDevice.radius' in dev:
        rr = _parse_radii(dev.get('realDevice.radius'))
        if rr:
            radii = rr

    # 如实标注「这条档案哪里可能是错的」，不造数据也不假装没问题
    caveats: List[str] = []
    if form == FORM_FOLDED and multi_form and not dev.get('twoCutoutPath'):
        # 折叠态本该用 twoCutoutPath，但配置里没有 → 退化复用了展开态的孔
        if cutob:
            caveats.append(
                'folded_form_reuses_unfolded_cutout: 配置里没有 twoCutoutPath，'
                '折叠态挖孔坐标直接复用了展开态的 —— 需实测（hidumper）复核')
    if form == FORM_DOUBLE:
        caveats.append(
            'double_form_inherits_cutout: 配置里没有为双屏中间态单独给挖孔，'
            '此处沿用展开态坐标 —— 需实测复核')
    if (isinstance(cutout_raw, str) and cutout_raw and cutob is None
            and not is_no_cutout_placeholder(cutout_raw)):
        caveats.append(f'cutout_unparsed: 挖孔 path 解析失败，原文={cutout_raw!r}')

    return FormProfile(
        name=name,
        device=str(dev.get('name') or ''),
        kind=kind,
        width=wi,
        height=hi,
        density=int(dev.get('screenDensity') or 0),
        diagonal=float(diag or 0),
        corner_radius=radii,
        cutouts=(cutob,) if cutob else (),
        fold_status=fold_status,
        source='product_config',
        extra={'product_series': dev.get('productSeries'),
               'raw_cutout': cutout_raw},
        caveats=tuple(caveats),
    )


def load_profiles(path: Optional[str] = None, *,
                  kinds: Optional[Sequence[str]] = None) -> List[FormProfile]:
    """加载全部形态档案。

    Parameters
    ----------
    kinds
        只要这些设备类型（如 `['Foldable', 'Tablet']`）。None = 全部。

    Notes
    -----
    折叠屏会**展开成多条档案**：每款折叠屏至少产出「展开态 + 折叠态」两条，
    三折叠设备（`Mate XT`）额外产出「双屏中间态」一条。因为它们在测试视角下
    就是不同的形态 —— 这也是跨形态测试最想覆盖的形态切换。

    形态命名规则（`name`）：
      - 普通设备：`<设备名>`，如 `Mate 60`
      - 折叠屏展开态：`<设备名>_unfolded`，如 `Mate X7_unfolded`
      - 折叠屏折叠态：`<设备名>_folded`
      - 三折叠中间态：`<设备名>_double`
    """
    cfg = load_product_config(path)
    if not cfg:
        return []

    out: List[FormProfile] = []
    for kind_key, devices in cfg.items():
        if kinds is not None and kind_key not in kinds:
            continue
        if not isinstance(devices, list):
            continue
        for dev in devices:
            if not isinstance(dev, dict):
                continue
            multi_form = 'outerScreenWidth' in dev
            forms: Tuple[Optional[str], ...]
            if multi_form:
                forms = (FORM_EXPANDED, FORM_FOLDED)
                if 'outerDoubleScreenWidth' in dev:
                    forms = (FORM_EXPANDED, FORM_DOUBLE, FORM_FOLDED)
            else:
                forms = (None,)
            for f in forms:
                p = _profile_from_device(kind_key, dev, form=f)
                if p:
                    out.append(p)

    _disambiguate_names(out)
    return out


def _disambiguate_names(profiles: List['FormProfile']) -> None:
    """就地消除重名档案（原位改 `name`）。

    配置里每个设备类别下都有一个通用「自定义设备」模板，四类都叫
    `Customize`：

        Phone/Customize    1316x2832
        Tablet/Customize   2560x1600
        2in1/Customize     3120x2080
        Car/Customize      3402x1620

    名字撞车会让 `find_profile()` 随机命中其中一个 —— 跨形态测试选错了
    基准形态，比没有这个形态更危险。所以重名的补 `kind` 前缀区分。
    """
    seen: Dict[str, int] = {}
    for p in profiles:
        seen[p.name] = seen.get(p.name, 0) + 1
    for p in profiles:
        if seen[p.name] > 1:
            p.name = f'{p.kind}/{p.name}'


def find_profile(name: str, path: Optional[str] = None,
                 profiles: Optional[Sequence[FormProfile]] = None
                 ) -> Optional[FormProfile]:
    """按名字（不区分大小写、忽略空格）找一条形态档案。"""
    pool = list(profiles) if profiles is not None else load_profiles(path)
    key = name.replace(' ', '').replace('_', '').lower()
    for p in pool:
        if p.name.replace(' ', '').replace('_', '').lower() == key:
            return p
    return None
