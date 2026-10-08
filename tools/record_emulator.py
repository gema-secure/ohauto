# -*- coding: utf-8 -*-
"""模拟器素材录制器 —— 把模拟器画面录成 mp4 演示素材。

★ 为什么需要这个脚本
--------------------

真机录屏路线已被证伪（截图 1.26 秒/张 ≈ 0.8 fps，且无 screenrecorder，
详见 docs/演示材料.md）。模拟器没有这个限制，但宿主机侧有三种死法：

1. **覆盖污染** —— ImageGrab 抓的是桌面合成结果，任何盖在模拟器上面的
   窗口都会被录进去（实测抓到过 ChatGPT 登录页）。
2. **PrintWindow 抓不到 GL** —— 模拟器手机屏是 OpenGL 渲染，
   PrintWindow 只能抓到窗口边框和工具栏，内容区全白（实测）。
3. **前台抢不过** —— 脚本进程没有前台权限，SetForegroundWindow
   会被 Windows 拒绝（实测 AttachThreadInput 也救不回来）。

因此提供两种模式：

- `screen`（30 fps 级，流畅）：宿主机屏幕捕获模拟器窗口矩形。
  **要求录制期间模拟器窗口在前台且不被遮挡**——脚本置前失败时会
  明确退出并提示人工点一下窗口，绝不带病开录。
- `guest`（2~4 fps，稳）：guest 端 `snapshot_display` 连拍内屏原生帧，
  与宿主机窗口状态完全无关，画面即内屏 2210x2416 原图。
  拍完后按「帧数/实际耗时」设定播放帧率，成片即实时速度。

用法
----

    # guest 连拍 20 秒（不怕遮挡，推荐无人值守）
    python tools/record_emulator.py --mode guest --seconds 20

    # 宿主机录屏 15 秒（先手动点一下模拟器窗口把它带到前台）
    python tools/record_emulator.py --mode screen --seconds 15

    # 指定输出
    python tools/record_emulator.py --mode guest --out D:/x/demo.mp4

依赖：workbuddy python（cv2 / numpy / pillow 均已装）。
素材口径提醒：**模拟器素材必须标注「模拟器 Mate X7」，不得与真机素材混用**（素材口径见项目内部指标档案，仓库外）。
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import datetime
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

#: 项目标准 hdc（hdc.config.json 里的那份，模拟器在它下面同样可见）
DEFAULT_HDC = r"D:\ohos-sdk\15\toolchains\hdc.exe"
DEFAULT_SERIAL = "127.0.0.1:5555"
GUEST_SNAP = "/data/local/tmp/_record_frame.jpeg"


def _dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:                                      # noqa: BLE001
        pass


def find_emulator_window() -> tuple:
    """找 Emulator.exe 主窗口，返回 (hwnd, (l,t,r,b))。找不到抛 RuntimeError。"""
    _dpi_aware()
    u32 = ctypes.windll.user32
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Emulator.exe",
                          "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, errors="replace").stdout
    pids = [int(l.split('","')[1]) for l in out.strip().splitlines() if '","' in l]
    if not pids:
        raise RuntimeError("Emulator.exe 没在运行 —— 先启动模拟器")
    pid = pids[0]

    wins = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, _):
        p = ctypes.c_uint()
        u32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(h):
            r = wt.RECT()
            u32.GetWindowRect(h, ctypes.byref(r))
            wins.append((h, (r.left, r.top, r.right, r.bottom)))
        return True
    u32.EnumWindows(cb, None)
    if not wins:
        raise RuntimeError("Emulator.exe 在跑，但没有可见窗口")
    hwnd, rect = max(wins, key=lambda w: (w[1][2] - w[1][0]) * (w[1][3] - w[1][1]))
    if rect[0] <= -30000:
        # 最小化窗口的坐标是 (-32000,-32000) —— 先还原再取矩形
        u32.ShowWindow(hwnd, 9)                                # SW_RESTORE
        time.sleep(1.0)
        r = wt.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(r))
        rect = (r.left, r.top, r.right, r.bottom)
        if rect[0] <= -30000:
            raise RuntimeError("模拟器窗口最小化且自动还原失败 —— 请手动展开窗口")
    return hwnd, rect


def force_foreground(hwnd: int) -> bool:
    """尽力把窗口带到前台。Windows 前台锁可能拒绝 —— 返回是否成功。"""
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    fg = u32.GetForegroundWindow()
    tid_fore = u32.GetWindowThreadProcessId(fg, None)
    tid_mine = k32.GetCurrentThreadId()
    u32.AttachThreadInput(tid_mine, tid_fore, True)
    u32.keybd_event(0x12, 0, 0, 0)                         # ALT 按下解锁前台
    u32.ShowWindow(hwnd, 9)                                # SW_RESTORE
    u32.SetForegroundWindow(hwnd)
    u32.keybd_event(0x12, 0, 2, 0)                         # ALT 抬起
    u32.AttachThreadInput(tid_mine, tid_fore, False)
    time.sleep(1.0)
    return u32.GetForegroundWindow() == hwnd


def record_screen(seconds: float, fps: float, out_path: str) -> None:
    """宿主机屏幕捕获模式：抓模拟器窗口矩形。"""
    import cv2                                             # noqa: PLC0415
    import numpy as np                                     # noqa: PLC0415
    from PIL import ImageGrab                              # noqa: PLC0415

    hwnd, rect = find_emulator_window()
    if not force_foreground(hwnd):
        print("✗ 模拟器窗口不在前台且置前失败（Windows 前台锁）。")
        print("  请**手动点一下模拟器窗口**把它带到最前，然后重跑本命令。")
        print("  或改用 --mode guest（不怕遮挡，但帧率低）。")
        raise SystemExit(2)
    print(f"窗口 rect={rect}，开始录制 {seconds}s @ {fps}fps …")

    u32 = ctypes.windll.user32
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    w, h = w - w % 2, h - h % 2                            # mp4v 要求偶数尺寸
    writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"),
                             fps, (w, h))
    n, t0 = 0, time.time()
    try:
        while time.time() - t0 < seconds:
            if n % 100 == 99:
                # 每 ~5s 重申一次前台 —— 实测终端等窗口会抢焦点，
                # 录到的就变成别人的画面了。ALT 技巧解锁前台限制。
                u32.keybd_event(0x12, 0, 0, 0)
                u32.SetForegroundWindow(hwnd)
                u32.keybd_event(0x12, 0, 2, 0)
            img = np.asarray(ImageGrab.grab(bbox=rect, all_screens=True))
            writer.write(img[:h, :w, ::-1])                # RGB->BGR
            n += 1
            time.sleep(max(0.0, n / fps - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\n（Ctrl+C 提前结束）")
    writer.release()
    _report(out_path, n, time.time() - t0)


def record_guest(seconds: float, hdc: str, serial: str, out_path: str) -> None:
    """guest 连拍模式：snapshot_display 直读内屏，不受宿主机窗口影响。"""
    import cv2                                             # noqa: PLC0415

    tmp = os.path.join(os.path.dirname(out_path), "_frames_tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)

    def sh(*args, timeout=30):
        return subprocess.run([hdc, "-t", serial, *args],
                              capture_output=True, timeout=timeout)

    # 帧尺寸探测 + 写手就绪
    if sh("shell", "snapshot_display", "-f", GUEST_SNAP).returncode != 0:
        raise RuntimeError("snapshot_display 失败 —— 模拟器是否在线？")
    probe = os.path.join(tmp, "probe.jpeg")
    sh("file", "recv", GUEST_SNAP, probe, timeout=30)
    import cv2 as _cv                                      # noqa: PLC0415
    first = _cv.imread(probe)
    if first is None:
        raise RuntimeError("guest 帧解码失败")
    h, w = first.shape[:2]
    w, h = w - w % 2, h - h % 2

    print(f"guest 帧尺寸 {w}x{h}，连拍 {seconds}s …（每帧 = snapshot+recv，约 0.3~0.6s）")
    writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"),
                             30, (w, h))                   # fps 最后按实际改
    n, t0 = 0, time.time()
    try:
        while time.time() - t0 < seconds:
            if sh("shell", "snapshot_display", "-f", GUEST_SNAP).returncode != 0:
                print("  [警告] 单帧 snapshot 失败，跳过")
                continue
            local = os.path.join(tmp, f"f{n:05d}.jpeg")
            if sh("file", "recv", GUEST_SNAP, local).returncode != 0:
                print("  [警告] 单帧 recv 失败，跳过")
                continue
            frame = cv2.imread(local)
            if frame is None:
                continue
            writer.write(frame[:, :w, :h])
            n += 1
    except KeyboardInterrupt:
        print("\n（Ctrl+C 提前结束）")
    elapsed = time.time() - t0
    writer.release()
    shutil.rmtree(tmp, ignore_errors=True)
    _retime(out_path, n, elapsed)                          # 播放速率 = 实时
    _report(out_path, n, elapsed)


def _retime(path: str, n: int, elapsed: float) -> None:
    """把成片帧率改成 n/elapsed，即按实时速度回放。"""
    if n < 2 or elapsed <= 0:
        return
    real_fps = max(1.0, n / elapsed)
    import cv2                                             # noqa: PLC0415
    cap = cv2.VideoCapture(path)
    ok, frames = True, []
    while ok:
        ok, f = cap.read()
        if ok:
            frames.append(f)
    cap.release()
    if not frames:
        return
    h, w = frames[0].shape[:2]
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"),
                             real_fps, (w, h))
    for f in frames:
        writer.write(f)
    writer.release()
    print(f"  播放帧率已按实时速度设定：{real_fps:.2f} fps")


def _report(out_path: str, n: int, elapsed: float) -> None:
    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    print("=" * 50)
    print(f"  输出  : {out_path}")
    print(f"  帧数  : {n}（实际 {n/elapsed:.2f} fps）" if elapsed else f"  帧数  : {n}")
    print(f"  时长  : {elapsed:.1f}s   大小 {size/1e6:.1f} MB")
    if n == 0:
        print("  ✗ 没有录到任何帧 —— 检查模拟器是否在线")
    else:
        print("  ✓ 素材已生成。**对外使用必须标注「模拟器 Mate X7」**")
    print("=" * 50)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=("screen", "guest"), default="guest",
                    help="screen=宿主录屏(流畅,需前台) guest=连拍(稳,不怕遮挡)")
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--fps", type=float, default=20.0, help="screen 模式目标帧率")
    ap.add_argument("--out", default=None, help="输出 mp4 路径")
    ap.add_argument("--hdc", default=DEFAULT_HDC)
    ap.add_argument("--serial", default=DEFAULT_SERIAL)
    a = ap.parse_args()

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = a.out or os.path.join(ROOT, "_out", "record",
                                f"emulator_{a.mode}_{stamp}.mp4")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    if a.mode == "screen":
        record_screen(a.seconds, a.fps, out)
    else:
        record_guest(a.seconds, a.hdc, a.serial, out)


if __name__ == "__main__":
    main()
