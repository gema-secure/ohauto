# -*- coding: utf-8 -*-
"""演示视频粗剪合成器 —— 分镜脚本 + 素材清单 → 带草稿旁白的 1080p 成片。

★ 这条流水线解决什么
--------------------

分镜脚本（`docs/视频分镜脚本-*.md`）定义了 7 个镜头与解说词，
但把「素材 → 成片」的合成工作留给了人工剪辑。本脚本把这一步自动化：

    素材（截图/录屏）── 按镜头表裁剪/定格 ──▶ 无声片段
    解说词 N1~N7 ── Windows SAPI 中文语音 ──▶ 草稿旁白 wav
    两轨 ffmpeg 合成（旁白定镜头时长）──▶ 中间片段 ── concat ──▶ 成片 mp4

⚠️ **草稿定位**：SAPI（Microsoft Huihui）的中文有明显机械感，本产物用于
**验证叙事节奏与镜头时长**，不是最终配音。替换真人配音只需把
`_out/video/work/voice_N*.wav` 换成真人录音（同名同路径），重跑本脚本。

依赖：imageio-ffmpeg（自带 ffmpeg 7.1，无需系统安装）、Pillow、Windows SAPI。

用法::

    python tools/make_video.py --assets _out/video/stills --out _out/video/draft.mp4

素材准备（本脚本不生成，见 README 的说明）：
    _out/video/stills/ 下需要：card1_title.png / card2_arch.png /
    card3_nums.png / card4_end.png（docs/demo/video_assets.html 截取）、
    gallery.png / onepager.png
    录屏：_out/record/explore_matex7.mp4、closed_loop_demo_v4.mp4
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:                                           # noqa: BLE001
    FFMPEG = 'ffmpeg'

W, H, FPS = 1920, 1080, 30
BG = '0x05070A'          # 与 video_assets.html 同色，pad 出来的边框不突兀

#: 解说词（抄自分镜脚本 §二，如改动务必同步那份文档）
NARRATION = {
    'N1': 'OpenHarmony 生态正在快速增长，应用质量保障缺少与之匹配的自动化测试基础设施。'
          '本作品，面向 OpenHarmony 的多模态智能测试系统，在真机上完成控件定位、'
          '用例生成、执行与失败归因的完整流程。',
    'N2': '团队对 7 个真实应用采集 800 个控件节点，其中具备 id 属性的仅占 5.62%。'
          '画面中的两个样本更为典型：系统设置页面 12 个可交互控件，id 全部为空；'
          '纯 Canvas 渲染的时钟应用，控件树中不存在任何可交互节点。'
          '单一控件树通道无法支撑稳定的控件定位。与此同时，测试失败难以区分'
          '设备异常、应用缺陷与脚本缺陷；大语言模型生成的用例可执行却缺乏实质验证。'
          '三项问题构成本作品的出发点。',
    'N3': '系统以四源融合定位为核心：静态源码、运行时控件树、屏幕截图与 OCR '
          '四路证据独立判断，融合规则只依据证据的声明与观察角色，不依赖来源名称。'
          '基于该机制确立缺陷判据：源码声明存在而运行时缺失的控件，判定为疑似缺陷。'
          '在此之上，系统构建失败归因与定位自愈闭环，以及可信用例生成校验。',
    'N4': '自动探索过程中，系统遍历应用页面，构建页面状态图。'
          '实测 157 秒得到 8 个页面状态、10 条跳转关系。'
          '对于控件树无法覆盖的控件，视觉通道依据屏幕截图完成定位，'
          '与控件树标注的一致率为 94.7%，平均交并比 0.842。'
          '该指标为一致性口径，并非绝对准确率。',
    'N5': '系统对生成用例逐步校验两项条件：该步骤是否具备失败可能，'
          '失败原因是否指向应用；缺乏实质验证的用例在导出前被拦截。'
          '通过校验的用例导出为 hypium 测试脚本，在测试框架中直接执行。'
          '执行过程中每一步均留存截图与控件树记录，失败步骤自动归因。',
    'N6': '当定位失效且同一位置连续失败达到阈值，系统基于历史控件树'
          '重新生成定位器并换代执行，用例由失败转为通过，全程无需人工干预。'
          '注入式评测中，失败归因 40 例全部正确，定位自愈 10 例全部成功。'
          '受控注入实验里，崩溃判定置信度 1.0，布局异常误报为 0。',
    'N7': '至此，系统完成从探索、生成、执行到归因自愈的完整闭环。'
          '全部能力均以可复现的实测数据支撑，随仓库一同交付。',
}

#: 镜头表：kind=image 定格图 / video 录屏截段；voice=旁白键；min_dur=时长下限（秒）
SHOTS = [
    dict(name='shot1_intro', kind='image', src='card1_title.png', voice='N1', min_dur=18),
    dict(name='shot2_problem', kind='image', src='gallery.png', voice='N2', min_dur=42),
    dict(name='shot3_arch', kind='image', src='card2_arch.png', voice='N3', min_dur=32),
    dict(name='shot4_explore', kind='video', src=os.path.join(ROOT, '_out', 'record', 'explore_matex7.mp4'),
         start=15, voice='N4', min_dur=38),
    dict(name='shot5_case', kind='image', src='onepager.png', voice='N5', min_dur=40),
    dict(name='shot6_loop', kind='video', src=os.path.join(ROOT, '_out', 'record', 'closed_loop_demo_v4.mp4'),
         start=42, voice='N6', min_dur=40),
    dict(name='shot7_nums', kind='image', src='card3_nums.png', voice='N7', min_dur=14),
    dict(name='shot8_end', kind='image', src='card4_end.png', voice=None, min_dur=5),
]


def run(cmd, timeout=600):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        tail = (r.stdout + r.stderr).decode('utf-8', errors='replace')[-800:]
        raise RuntimeError('命令失败: %s\n%s' % (' '.join(cmd[:4]), tail))
    return r


def synth_voice(text: str, wav_path: str) -> float:
    """用 Windows SAPI（中文 Huihui）合成旁白，返回时长（秒）。已存在则跳过。"""
    if os.path.isfile(wav_path) and os.path.getsize(wav_path) > 1000:
        pass
    else:
        txt = wav_path + '.txt'
        with open(txt, 'w', encoding='utf-8') as f:
            f.write(text)
        ps = wav_path + '.ps1'
        with open(ps, 'w', encoding='utf-8') as f:
            f.write(
                "Add-Type -AssemblyName System.Speech\n"
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
                "try { $s.SelectVoice('Microsoft Huihui Desktop') } catch {}\n"
                "$s.Rate = 0\n"
                "$t = Get-Content -Path '%s' -Encoding UTF8 -Raw\n"
                "$s.SetOutputToWaveFile('%s')\n"
                "$s.Speak($t)\n"
                "$s.Dispose()\n" % (txt, wav_path))
        subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                        '-File', ps], capture_output=True, timeout=300)
    with wave.open(wav_path, 'rb') as w:
        return w.getnframes() / float(w.getframerate())


def render_segment(shot: dict, assets: str, work: str) -> str:
    """渲染一个镜头片段（视频 + 旁白），返回 mp4 路径。"""
    name, dur = shot['name'], float(shot['min_dur'])
    if shot.get('voice'):
        wav = os.path.join(work, 'voice_%s.wav' % shot['voice'])
        v_dur = synth_voice(NARRATION[shot['voice']], wav)
        dur = max(dur, v_dur + 2.0)              # 旁白 + 尾部留白
        print('  [旁白] %s: %.1fs → 镜头 %.1fs' % (shot['voice'], v_dur, dur))
    raw = os.path.join(work, name + '_raw.mp4')
    seg = os.path.join(work, name + '.mp4')

    vf = (f'scale={W}:{H}:force_original_aspect_ratio=decrease,'
          f'pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={BG},fps={FPS},format=yuv420p')
    if shot['kind'] == 'image':
        img = os.path.join(assets, shot['src'])
        run([FFMPEG, '-y', '-loop', '1', '-framerate', str(FPS), '-i', img,
             '-t', '%.2f' % dur, '-vf', vf, '-c:v', 'libx264', '-preset',
             'veryfast', '-crf', '20', '-an', raw], timeout=900)
    else:
        run([FFMPEG, '-y', '-ss', str(shot['start']), '-i', shot['src'],
             '-t', '%.2f' % dur, '-vf', vf, '-c:v', 'libx264', '-preset',
             'veryfast', '-crf', '20', '-an', raw], timeout=900)

    if shot.get('voice'):
        wav = os.path.join(work, 'voice_%s.wav' % shot['voice'])
        # ⚠️ 不能裸用 apad + -shortest：apad 补的是无限静音，-c:v copy 下
        #    -shortest 判定失效，ffmpeg 会一直编码到超时（实测 900s 卡死）。
        #    改为 apad=whole_dur=<镜头时长>，音频显式定长，不再依赖 -shortest。
        run([FFMPEG, '-y', '-i', raw, '-i', wav, '-filter_complex',
             '[1:a]aresample=44100,pan=stereo|c0=c0|c1=c0,'
             'apad=whole_dur=%.2f[a]' % dur,
             '-map', '0:v', '-map', '[a]', '-t', '%.2f' % dur,
             '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k', seg], timeout=900)
    else:
        # 无旁白镜头：定长静音源（同样显式限时，避免无限流）
        run([FFMPEG, '-y', '-i', raw, '-f', 'lavfi',
             '-i', 'anullsrc=r=44100:cl=stereo', '-t', '%.2f' % dur,
             '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k', seg], timeout=900)
    print('  [片段] %s: %.1fs' % (name, dur))
    return seg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--assets', default=os.path.join(ROOT, '_out', 'video', 'stills'))
    ap.add_argument('--work', default=os.path.join(ROOT, '_out', 'video', 'work'))
    ap.add_argument('--out', default=os.path.join(ROOT, '_out', 'video', 'draft.mp4'))
    a = ap.parse_args()
    os.makedirs(a.work, exist_ok=True)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)

    segs = []
    for shot in SHOTS:
        src = os.path.join(a.assets, shot['src']) if shot['kind'] == 'image' else shot['src']
        if not os.path.isfile(src):
            print('✗ 素材缺失: %s' % src)
            return 2
        print('== %s ==' % shot['name'])
        segs.append(render_segment(shot, a.assets, a.work))

    lst = os.path.join(a.work, 'concat.txt')
    with open(lst, 'w', encoding='utf-8') as f:
        for s in segs:
            f.write("file '%s'\n" % s.replace('\\', '/'))
    run([FFMPEG, '-y', '-f', 'concat', '-safe', '0', '-i', lst,
         '-c', 'copy', a.out], timeout=600)
    size = os.path.getsize(a.out) / 1e6
    total = sum(float(s['min_dur']) for s in SHOTS)
    print('=' * 52)
    print('  成片: %s（%.1f MB）' % (a.out, size))
    print('  ⚠️ 草稿定位：旁白为 SAPI 机械音，替换真人配音见头注')
    print('=' * 52)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
