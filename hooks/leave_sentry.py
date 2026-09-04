#!/usr/bin/env python3
"""键盘哨兵：离开模式下人回到电脑键鼠一动 → 自动解除离开模式 + 🔓推群。
原理：ioreg IOHIDSystem 的 HIDIdleTime 只被真实 HID 输入复位（SSH/合成事件不算），
所以「闲置曾≥30s 后突降到 <2s」= 人真的坐到电脑前了。异常一律静默重试。"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import remote_approve as ra

IDLE_SEEN = 30     # 连续无输入≥30s 视为已离席（此后第一次动键鼠才触发解除）
REARM_GAP = 60     # 解除后 60s 内不再重复触发（防群关键词刚开又被误关）
TRIGGER_UNDER = 2  # idle 低于此值算"人回来了"


def idle_sec():
    """真实键鼠闲置秒数；读不到返回 None（本周期跳过，绝不拿 0 当活动误关）。
    注意：HIDIdleTime 挂在 IOHIDSystem 的深层子设备上，不能加 -d 1，否则恒为空。"""
    r = subprocess.run(['ioreg', '-c', 'IOHIDSystem'],
                       capture_output=True, text=True, timeout=5)
    for line in r.stdout.splitlines():
        if 'HIDIdleTime' in line and '=' in line:
            try:
                return int(line.split('=')[-1].strip()) / 1e9
            except ValueError:
                return None
    return None


def disarm():
    try:
        os.remove(ra.MARKER)
    except FileNotFoundError:
        pass
    try:
        tok = ra.get_token()
        ra.push('🔓 检测到你回到电脑动了键盘，离开模式已自动解除，回到本地对话。'
                '需要再开：对本群或助手说「打开离开模式」', tok)
        ra.log('sentry: auto-disarmed + 🔓 pushed')
    except Exception as e:
        ra.log(f'sentry: disarmed but push failed: {e!r}')


def main():
    ra.log('sentry start')
    was_idle = False
    last_fire = 0.0
    while True:
        time.sleep(2)
        try:
            if not os.path.exists(ra.MARKER):
                was_idle = False
                continue
            i = idle_sec()
            if i is None:
                continue
            if i >= IDLE_SEEN:
                was_idle = True
            elif i < TRIGGER_UNDER and was_idle and time.time() - last_fire > REARM_GAP:
                last_fire = time.time()
                was_idle = False
                disarm()
        except Exception as e:
            ra.log(f'sentry loop err: {e!r}')
            time.sleep(5)


if __name__ == '__main__':
    main()
