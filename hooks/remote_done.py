#!/usr/bin/env python3
"""Stop 钩子：离开模式下，每轮对话结束把助手最后的回复推到飞书，
让手机端知道任务是"做完了在等指令"还是还在跑。异常一律静默，不打扰本地。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import remote_approve as ra

MAX_TEXT = 800


def main():
    data = json.loads(sys.stdin.read())
    ra.tmux_state('')       # 这轮说完了 → 状态条复位空闲灰（通用，与离开模式无关）
    ra.tmux_banner_clear()  # 兜底清横幅（正常路径钩子表决完已清；防被杀残留）
    if os.environ.get('CC_SESSION_KEY'):
        sys.exit(0)  # cc-connect 会话自己会回复手机，不重复推
    if not os.path.exists(ra.MARKER):
        sys.exit(0)  # 离开模式没开，安静
    path = data.get('transcript_path') or ''
    if not os.path.exists(path):
        sys.exit(0)
    text = ra.last_assistant_text(path)
    if not text:
        sys.exit(0)
    brief = text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + '\n…（内容较长已截断，回电脑看全文）'
    body = (f'🏁 这轮回复说完了（离开模式）\n{brief}\n'
            f'——没下文=我在等你下一步；要解除离开模式，对本群或助手说「关闭离开模式」即可')
    tok = ra.get_token()
    ra.push(body, tok)
    ra.log(f'stop-hook pushed {len(body)} chars')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        ra.log(f'stop-hook ERROR: {e!r}')
        sys.exit(0)
