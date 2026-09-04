#!/usr/bin/env python3
"""飞书遥控器（群「claude遥控器」⇄ tmux 真窗口）：
- 群内发言(不@机器人) → tmux send-keys 打进【绑定的任务窗口】；# 开头是遥控指令不外发
- 绑定窗口画面稳定6s → 推「【xxx 画面】」快照到群
- #列表 查看所有 tmux 窗口；#切 名字 换绑定目标（重启记忆）
- 私聊+群内命中「打开/关闭离开模式」类关键词 → 程序化 touch/rm 标记文件，绕开一切 agent
- 审批卡/完成通知在 remote_approve.py / remote_done.py 钩子（也推群），本脚本不管
- 依赖：tmux、python3（标准库即可）、飞书自建应用；详见包根 README"""
import json
import os
import re
import subprocess
import time
import urllib.request

# 凭据与群号集中在 feishu.json（模板见包根目录 config.example.json）
CONFIG_PATH = os.path.expanduser('~/.claude/remote/feishu.json')
with open(CONFIG_PATH) as _f:
    _CFG = json.load(_f)
APP_ID, APP_SECRET = _CFG['app_id'], _CFG['app_secret']
CHAT_ID = _CFG['p2p_chat_id']     # 机器人私聊（开关确认回执走这里）
GROUP_ID = _CFG['group_chat_id']  # 遥控器群（指令进/画面向/卡片落）
POLL = 2
STABLE = 6
TAIL_LINES = 35
FLAG = os.path.expanduser('~/.remote_approve_on')
HERE = os.path.dirname(os.path.abspath(__file__))
TARGET_FILE = os.path.join(HERE, 'watcher_target')
ON_WORDS = ('打开离开模式', '打开手机审批开关', '审批开')
OFF_WORDS = ('关闭离开模式', '手机审批关', '恢复普通模式')


def load_target():
    try:
        with open(TARGET_FILE) as f:
            t = f.read().strip()
            if t:
                return t
    except FileNotFoundError:
        pass
    return 'dev'


def save_target(name):
    with open(TARGET_FILE, 'w') as f:
        f.write(name)


def api(url, payload, token=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method='POST')
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def api_get(url, token):
    req = urllib.request.Request(url)
    req.add_header('Authorization', f'Bearer {token}')
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def get_token():
    return api(
        'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
        {'app_id': APP_ID, 'app_secret': APP_SECRET},
    )['tenant_access_token']


def refresh_token(ctx, force=False):
    if force or time.time() - ctx['token_ts'] > 5400:
        ctx['token'] = get_token()
        ctx['token_ts'] = time.time()


def send(text, token, chat=None):
    return api(
        'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',
        {'receive_id': chat or CHAT_ID, 'msg_type': 'text',
         'content': json.dumps({'text': text}, ensure_ascii=False)},
        token,
    )


def tmux(*args):
    r = subprocess.run(['tmux', *args], capture_output=True, text=True, timeout=5)
    return r.returncode, (r.stdout + r.stderr).strip()


def pane(session):
    code, out = tmux('capture-pane', '-t', session, '-p')
    if code != 0:
        return ''
    lines = [l.rstrip() for l in out.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    return '\n'.join(lines[-TAIL_LINES:])


def session_list():
    code, out = tmux('list-sessions', '-F',
                     '#{session_name}\t#{pane_current_path}\t#{pane_current_command}\t#{?session_attached,已打开,手机托管中}')
    if code != 0:
        return None
    return out.splitlines()


def match_toggle(text):
    """命中开关词返回 True(开)/False(关)，否则 None。规则：文本含词组且 ≤12 字。
    先判关后判开：避免「关闭手机审批开关」「审批开关关掉」被「审批开」子串误判。"""
    t = text.strip()
    if not t or len(t) > 12:
        return None
    for w in OFF_WORDS:
        if w in t:
            return False
    if ('关闭' in t or '关掉' in t) and ('模式' in t or '审批' in t):
        return False
    for w in ON_WORDS:
        if w in t:
            return True
    if ('打开' in t or '开启' in t) and ('模式' in t or '审批' in t):
        return True
    return None


def apply_flag(on):
    if on:
        with open(FLAG, 'a'):
            pass
    else:
        try:
            os.remove(FLAG)
        except FileNotFoundError:
            pass


def fetch_user_msgs(ctx, chat_id, since_ms):
    """拉某聊天 since 之后的用户文本消息 [(ts,text)] 旧→新；失败返回空表。"""
    url = ('https://open.feishu.cn/open-apis/im/v1/messages'
           f'?container_id_type=chat&container_id={chat_id}'
           '&sort_type=ByCreateTimeDesc&page_size=5')
    try:
        data = api_get(url, ctx['token'])
    except Exception:
        try:
            refresh_token(ctx, force=True)
            data = api_get(url, ctx['token'])
        except Exception as e2:
            print(f'[watcher] poll {chat_id[-6:]} failed: {e2}', flush=True)
            return []
    hits = []
    for m in data.get('data', {}).get('items') or []:
        if m.get('deleted') or m.get('msg_type') != 'text':
            continue
        if (m.get('sender') or {}).get('sender_type') != 'user':
            continue
        try:
            ts = int(m['create_time'])
            text = json.loads(m['body']['content']).get('text', '')
        except Exception:
            continue
        if ts > since_ms:
            hits.append((ts, text))
    hits.sort()
    return hits


def ack_toggle(ctx, chat, on):
    apply_flag(on)
    word = '已开启；审批和完成通知都推本群' if on else '已关闭，恢复本地对话'
    try:
        send(f'✅ 离开模式{word}', ctx['token'], chat)
    except Exception as e:
        print(f'[watcher] ack send failed: {e}', flush=True)
    print(f'[watcher] leave-mode {"ON" if on else "OFF"} (ack->{chat[-6:]})', flush=True)


def admin_cmd(ctx, text):
    """群里 # 开头的遥控指令。返回 True=已处理。"""
    t = text.strip()
    if t in ('#列表', '#list'):
        rows = session_list()
        if rows is None:
            body = '⚠ tmux 没在跑，没有任何窗口'
        else:
            cur = ctx['target']
            lines = [f'🎮 当前绑定：{cur}', '📟 窗口列表（名字 路径 程序 状态）：']
            for r in rows:
                p = r.split('\t')
                mark = '👉' if p[0] == cur else '  '
                lines.append(f'{mark}{p[0]}  [{p[1] or "?"}]  {p[2]}  {p[3] if len(p) > 3 else ""}')
            lines.append('#切 名字 → 换绑定目标')
            body = '\n'.join(lines)
        send(body, ctx['token'], GROUP_ID)
        return True
    m = re.match(r'^#切\s*(\S+)', t) or re.match(r'^#(attach|use)\s+(\S+)', t)
    if m:
        name = m.group(m.lastindex)
        code, out = tmux('has-session', '-t', name)
        if code != 0:
            send(f'❌ 没有窗口「{name}」，发 #列表 看现有的', ctx['token'], GROUP_ID)
        else:
            ctx['target'] = name
            ctx['pushed'] = ''
            save_target(name)
            send(f'🎯 遥控器已绑定「{name}」，这个群从现在起只跟进/指挥它', ctx['token'], GROUP_ID)
            print(f'[watcher] target -> {name}', flush=True)
        return True
    if t.startswith('#'):
        send('❓ 不认识的指令。用法：#列表 看窗口 / #切 名字 换目标 / 其它话直接进绑定窗口',
             ctx['token'], GROUP_ID)
        return True
    return False


def send_to_window(ctx, text):
    """群消息原样打进绑定窗口（先 -l 字面文本，再单独回车提交）。"""
    t = re.sub(r'@_user_\d+', ' ', text)  # 群里@机器人会带占位符，剥掉再传
    t = ' '.join(t.split())               # 换行/连续空白压平，保证一次提交
    if not t:
        return
    tgt = ctx['target']
    code, out = tmux('send-keys', '-t', tgt, '-l', t)
    if code != 0:
        send(f'⚠ 窗口「{tgt}」不存在了：{out}。发 #列表 看可用的，再 #切', ctx['token'], GROUP_ID)
        print(f'[watcher] send-keys no-session: {out}', flush=True)
        return
    tmux('send-keys', '-t', tgt, 'Enter')
    print(f'[watcher] ->{tgt}: {t[:60]}', flush=True)


def poll_incoming(ctx):
    for ts, text in fetch_user_msgs(ctx, CHAT_ID, ctx['p2p_ts']):
        ctx['p2p_ts'] = ts
        on = match_toggle(text)
        if on is not None:
            ack_toggle(ctx, CHAT_ID, on)
    for ts, text in fetch_user_msgs(ctx, GROUP_ID, ctx['grp_ts']):
        ctx['grp_ts'] = ts
        on = match_toggle(text)
        if on is not None:
            ack_toggle(ctx, GROUP_ID, on)
            continue
        if text.strip().startswith('#'):
            admin_cmd(ctx, text)
            continue
        send_to_window(ctx, text)


def main():
    now_ms = int(time.time() * 1000)
    ctx = {'token': get_token(), 'token_ts': time.time(),
           'p2p_ts': now_ms, 'grp_ts': now_ms, 'target': load_target()}
    last = pushed = pane(ctx['target'])
    stable_since = None
    print(f'[watcher] started, target={ctx["target"]}', flush=True)
    while True:
        time.sleep(POLL)
        try:
            refresh_token(ctx)
            poll_incoming(ctx)
        except Exception as e:
            print(f'[watcher] poll loop error: {e}', flush=True)
        cur = pane(ctx['target'])
        if not cur.strip():
            continue
        if cur != last:
            last = cur
            stable_since = time.time()
            continue
        if stable_since and time.time() - stable_since >= STABLE and cur != ctx.get('pushed', pushed):
            body = f'【{ctx["target"]} 画面】\n' + cur
            try:
                send(body, ctx['token'], GROUP_ID)
            except Exception:
                try:
                    refresh_token(ctx, force=True)
                    send(body, ctx['token'], GROUP_ID)
                except Exception as e2:
                    print(f'[watcher] send failed: {e2}', flush=True)
                    ctx['pushed'] = cur
                    stable_since = None
                    continue
            ctx['pushed'] = cur
            stable_since = None
            print(f'[watcher] pushed at {time.strftime("%H:%M:%S")}', flush=True)


if __name__ == '__main__':
    main()
