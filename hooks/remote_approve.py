#!/usr/bin/env python3
"""PreToolUse 离开模式审批钩子。

设计定稿（见开发.md 需求2）：
- 标记文件 ~/.remote_approve_on 不存在 → 什么都不做（本地正常弹问）
- cc-connect 无头会话（CC_SESSION_KEY 存在）→ 跳过，防双重审批
- 含 remote_approve 的操作 → 白名单秒过（开关不能卡死自己）
- 低危（搜索/读取/找文件/下载）→ 秒过不推
- 中危（改/写文件、普通命令）→ 推「claude遥控器」群，2/3 分钟催办，15 分钟超时默认同意
- 高危（删除、git push、部署、sudo 等）→ 推群，超时默认拒绝
- 群内只认表情表决（👍/👎 反应标签，不产生消息）；文字表决已作废——群文字是指令直通通道
- 窗口状态色（通用，不看离开模式）：干活绿/待批中危黄/高危或被拒红/Stop 复位空闲灰，
  由钩子写 tmux 窗口级 @ccstate/@ccnote，状态条实时变色；非 tmux 窗口降级 macOS 横幅
- 任何异常 → exit 0 回退本地弹问，绝不卡死任务
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request

MARKER = os.path.expanduser('~/.remote_approve_on')
# 凭据与群号集中在 feishu.json（模板见包根目录 config.example.json，权限建议 chmod 600）
CONFIG_PATH = os.path.expanduser('~/.claude/remote/feishu.json')
LOG_PATH = os.path.expanduser('~/.claude/hooks/remote_approve.log')
REMIND_1, REMIND_2, AUTO_SEC, POLL_SEC = 120, 180, 900, 3

LOW_TOOLS = {
    'Read', 'Glob', 'Grep', 'WebSearch', 'WebFetch', 'NotebookRead',
    'Task', 'TodoWrite', 'TaskCreate', 'TaskGet', 'TaskList', 'TaskUpdate',
    'TaskOutput', 'TaskStop', 'CronCreate', 'CronDelete', 'CronList',
    'ListMcpResourcesTool', 'ReadMcpResourceTool', 'EnterPlanMode',
    'ExitPlanMode', 'AskUserQuestion', 'Skill', 'Config', 'Brief',
    'WaitForMcpServers', 'SlashCommand',
}
MED_TOOLS = {'Edit', 'Write', 'MultiEdit', 'NotebookEdit'}
MCP_LOW_SUBSTR = ('list', 'get', 'read', 'search', 'screenshot', 'snapshot',
                  'query', 'analyze', 'capture', 'status', 'view', 'describe', 'fetch')

LOW_CMDS = {
    'ls', 'pwd', 'echo', 'printf', 'cat', 'head', 'tail', 'less', 'more',
    'grep', 'egrep', 'fgrep', 'rg', 'fd', 'find', 'tree', 'which', 'whereis',
    'type', 'date', 'whoami', 'id', 'uname', 'hostname', 'uptime', 'stat',
    'file', 'du', 'df', 'ps', 'sort', 'uniq', 'wc', 'cut', 'tr', 'env', 'history',
    'base64', 'md5', 'shasum', 'diff', 'cmp', 'seq', 'basename', 'dirname', 'cd',
}
HIGH_FIRST = {'rm', 'rmdir', 'unlink', 'trash', 'shred', 'dd', 'mkfs', 'diskutil'}
TEMP_PREFIXES = ('/tmp/', '/private/tmp/', '/var/folders/',
                 os.path.expanduser('~/.Trash/'))


def log(msg):
    try:
        with open(LOG_PATH, 'a') as f:
            f.write(f'[{time.strftime("%m-%d %H:%M:%S")}] {msg}\n')
    except Exception:
        pass


def tmux_state(state, note=''):
    """给本窗口状态条染色：g 干活 / y 待批(中危) / r 高危·被拒 / '' 空闲复位。
    返回是否在 tmux 里（False 时调用方可用横幅兜底）。非 tmux/异常一律静默。"""
    pane = os.environ.get('TMUX_PANE')
    if not pane:
        return False
    try:
        args = ['tmux', 'setw']
        args += ['-u', '-t', pane, '@ccstate'] if not state else ['-t', pane, '@ccstate', state]
        r = subprocess.run(args, capture_output=True, timeout=3)
        nargs = ['tmux', 'setw']
        nargs += ['-u', '-t', pane, '@ccnote'] if not note else ['-t', pane, '@ccnote', note[:24]]
        subprocess.run(nargs, capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def banner(title, msg):
    """非 tmux 窗口的降级兜底：macOS 横幅（标题带目录名以辨窗口）。"""
    try:
        subprocess.run(['osascript', '-e',
                        f'display notification {json.dumps(msg)} with title {json.dumps(title)}'],
                       capture_output=True, timeout=5)
    except Exception:
        pass


def tmux_session():
    """本窗口的 tmux session 名（卡片来源用）；非 tmux 返回空串。"""
    pane = os.environ.get('TMUX_PANE')
    if not pane:
        return ''
    try:
        r = subprocess.run(['tmux', 'display', '-p', '-t', pane, '#{session_name}'],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else ''
    except Exception:
        return ''


def tmux_banner(tier, rid):
    """待批文字播报：占用该窗口整条状态行显示黄/红横幅（颜色系统只管标签，字靠这个）。"""
    pane = os.environ.get('TMUX_PANE')
    if not pane:
        return
    if tier == 'HIGH':
        text = '#[bg=colour1,fg=colour231,bold] 🔴 高危待审批 ' + rid + ' · 手机群点 👍/👎 · 15分钟未回自动拒绝 #[default]'
    else:
        text = '#[bg=colour3,fg=colour16,bold] 🟡 待审批 ' + rid + ' · 手机群点 👍放行/👎拒绝 · 15分钟未回自动放行 #[default]'
    try:
        subprocess.run(['tmux', 'setw', '-t', pane, 'status-format[0]', text],
                       capture_output=True, timeout=3)
    except Exception:
        pass


def tmux_banner_clear():
    pane = os.environ.get('TMUX_PANE')
    if not pane:
        return
    try:
        subprocess.run(['tmux', 'setw', '-u', '-t', pane, 'status-format[0]'],
                       capture_output=True, timeout=3)
    except Exception:
        pass


def cfg():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def api_post(url, payload, token=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method='POST')
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def api_get(url, token):
    req = urllib.request.Request(url)
    req.add_header('Authorization', f'Bearer {token}')
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def get_token():
    c = cfg()
    return api_post(
        'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
        {'app_id': c['app_id'], 'app_secret': c['app_secret']})['tenant_access_token']


def push(text, token):
    r = api_post(
        'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',
        {'receive_id': cfg()['group_chat_id'], 'msg_type': 'text',
         'content': json.dumps({'text': text}, ensure_ascii=False)},
        token)
    return (r.get('data') or {}).get('message_id', '')


def add_reaction(token, message_id, emoji_type):
    api_post(
        f'https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions',
        {'reaction_type': {'emoji_type': emoji_type}}, token)


ALLOW_EMOJI = {'THUMBSUP', 'OK', 'Yes', 'CheckMark'}
DENY_EMOJI = {'ThumbsDown', 'No', 'CrossMark'}


def reactions(token, message_id):
    data = api_get(
        f'https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions',
        token)
    out = []
    for it in data.get('data', {}).get('items', []):
        op = (it.get('operator') or {}).get('operator_type')
        if op != 'user':
            continue
        out.append((it.get('reaction_id'),
                    (it.get('reaction_type') or {}).get('emoji_type')))
    return out


ENV_ASSIGN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


def first_token(parts):
    for p in parts:
        if ENV_ASSIGN.match(p):
            continue  # 环境变量赋值前缀 FOO=bar
        return os.path.basename(p)
    return ''


def classify_segment(seg):
    """单段命令 → LOW/MED/HIGH"""
    try:
        parts = shlex.split(seg)
    except ValueError:
        return 'MED'
    first = first_token(parts)
    if first == 'sudo':
        return 'HIGH'
    if first in HIGH_FIRST:
        args = [p for p in parts[1:] if not p.startswith('-')]
        if first == 'rm' and args and all(
                a.startswith(TEMP_PREFIXES) for a in args):
            return 'LOW'  # rm /tmp/xxx 这类临时文件秒过
        return 'HIGH'
    if first == 'git':
        if any(a in parts[1:] for a in ('push', 'clean', 'reset', 'rebase')):
            return 'HIGH'
        if len(parts) > 1 and parts[1] in {
                'status', 'log', 'diff', 'show', 'blame', 'ls-files',
                'ls-remote', 'shortlog', 'describe', 'config'}:
            return 'LOW'
        return 'MED'
    if first in {'bash', 'sh', 'zsh', 'eval', 'xargs', 'source'} and re.search(
            r'\b(rm|rmdir|unlink|shred|mkfs|sudo)\b|git\s+push', seg):
        return 'HIGH'  # 解释器里藏着删除命令，按高危
    if first in {'npm', 'pnpm', 'yarn', 'bun'} and any(
            a in parts[1:] for a in ('publish', 'deploy')):
        return 'HIGH'
    if first in {'deploy', 'kubectl', 'terraform', 'wrangler', 'vercel', 'fly'} \
            and any(a in parts[1:] for a in ('apply', 'deploy', 'push', 'publish', 'up', '--prod')):
        return 'HIGH'
    if first in {'docker', 'docker-compose', 'podman'} and any(
            a in parts[1:] for a in ('rm', 'rmi', 'prune', 'down')):
        return 'HIGH'
    if first == 'find' and any(a in ('-delete',) or a.startswith('-exec')
                               or a.startswith('-ok') for a in parts[1:]):
        return 'HIGH'
    if first in {'curl', 'wget'}:
        risky = any(a in parts for a in ('-X', '--request', '-d', '--data',
                                         '--data-binary', '-F', '--upload-file', '-T'))
        post_like = re.search(r'-X\s*(?!GET\b)[A-Z]+', seg)
        return 'LOW' if not risky and not post_like else 'MED'
    if first in LOW_CMDS:
        return 'LOW'
    return 'MED'


def classify_bash(cmd):
    tier = 'LOW'
    for seg in re.split(r'&&|\|\||;|\n|\|', cmd):
        seg = seg.strip()
        if not seg:
            continue
        t = classify_segment(seg)
        if t == 'HIGH':
            return 'HIGH'
        if t == 'MED':
            tier = 'MED'
    return tier


def _seg_parts(seg):
    try:
        parts = shlex.split(seg)
    except ValueError:
        parts = seg.split()
    first, idx = '', 0
    for i, p in enumerate(parts):
        if not ENV_ASSIGN.match(p):
            first, idx = os.path.basename(p), i
            break
    return first, parts[idx + 1:]


def summarize(tool, ti):
    """一句话中文描述这个操作实际在干什么。"""
    try:
        if tool == 'Bash':
            cmd = (ti.get('command') or '').strip()
            descs = []
            for seg in re.split(r'&&|\|\||;|\n|\|', cmd):
                seg = seg.strip()
                if not seg:
                    continue
                first, rest = _seg_parts(seg)
                args = [p for p in rest if not p.startswith('-')]
                if first == 'rm':
                    items = '、'.join(
                        f'「{os.path.basename(p.rstrip("/"))}」(位于 {os.path.dirname(p) or "/"})'
                        for p in args[:6])
                    descs.append(f'删除文件 {items}' + (f' 等{len(args)}个' if len(args) > 6 else ''))
                elif first == 'git' and 'push' in rest:
                    descs.append('把本地 git 提交推送到远程仓库')
                elif first == 'git' and 'commit' in rest:
                    descs.append('提交改动到本地 git 仓库')
                elif first == 'mv':
                    descs.append('移动/重命名: ' + ' → '.join(args[:3]))
                elif first == 'cp':
                    descs.append('复制: ' + ' → '.join(args[:3]))
                elif first == 'sudo':
                    descs.append('以管理员权限执行后续命令')
                elif first in {'curl', 'wget'}:
                    descs.append(f'网络请求({first}): ' + (args[0] if args else seg[:60]))
                elif first in {'mkdir', 'touch'}:
                    descs.append(f'{"新建目录" if first=="mkdir" else "新建/更新时间戳文件"}: ' + '、'.join(args[:3]))
                elif first in {'chmod', 'chown'}:
                    descs.append('修改文件权限/归属: ' + '、'.join(args[:3]))
                elif first in {'bash', 'sh', 'zsh', 'eval', 'xargs', 'source'}:
                    inner = ' '.join(args)[:80]
                    if re.search(r'\b(rm|rmdir|unlink|shred|mkfs)\b|git\s+push', seg):
                        descs.append(f'⚠ 脚本内藏删除/推送操作: {inner}')
                    else:
                        descs.append(f'运行脚本片段: {inner}')
                elif first in {'python', 'python3', 'node', 'npm', 'pnpm', 'yarn'}:
                    descs.append(f'运行 {first}: ' + ' '.join(args[:3])[:60])
                elif first in {'kill', 'pkill', 'killall'}:
                    descs.append('终止进程: ' + ' '.join(args[:3]))
                elif first in LOW_CMDS:
                    continue  # ls/cat 这类只读操作不用啰嗦
                else:
                    descs.append(f'执行命令「{seg[:60]}」')
            return '；'.join(descs[:4]) or '只读查询，不改动任何文件'
        if tool in ('Edit', 'MultiEdit'):
            path = ti.get('file_path', '')
            o = (ti.get('old_string') or '').splitlines()
            n = (ti.get('new_string') or '').splitlines()
            pairs = []
            for i in range(max(len(o), len(n))):
                a = o[i].strip() if i < len(o) else ''
                b = n[i].strip() if i < len(n) else ''
                if a != b:
                    pairs.append(f'{a or "(新行)"} → {b or "(删除)"}')
                if len(pairs) >= 5:
                    break
            head = f'修改文件 {os.path.basename(path)} (位于 {os.path.dirname(path) or "/"})：'
            return head + ('；'.join(p[:80] for p in pairs[:5]) or '内容有调整')
        if tool == 'Write':
            path = ti.get('file_path', '')
            lines = len((ti.get('content') or '').splitlines())
            return f'覆写/新建文件 {os.path.basename(path)} (位于 {os.path.dirname(path) or "/"})，共 {lines} 行'
        if tool == 'NotebookEdit':
            return f'修改 Jupyter 笔记本 {os.path.basename(ti.get("notebook_path", ""))} 的单元格'
        return f'调用 {tool}'
    except Exception:
        return f'调用 {tool}'


def tool_detail(tool, ti):
    """审批详情全文（与本地弹问框一致的内容）。"""
    if tool in ('Edit', 'MultiEdit'):
        return (f"文件: {ti.get('file_path', '')}\n"
                f"──── 旧内容 ────\n{ti.get('old_string', '')}\n"
                f"──── 新内容 ────\n{ti.get('new_string', '')}")
    if tool == 'Write':
        return f"文件: {ti.get('file_path', '')}\n──── 写入内容 ────\n{ti.get('content', '')}"
    if tool == 'NotebookEdit':
        return (f"文件: {ti.get('notebook_path', '')}\n"
                f"──── 新单元格 ────\n{ti.get('new_source', '')}")
    if tool == 'Bash':
        return f"命令:\n{ti.get('command', '')}"
    return json.dumps(ti, ensure_ascii=False, indent=1)


def classify(data):
    """→ (tier, 详情全文)"""
    tool = data.get('tool_name', '')
    ti = data.get('tool_input') or {}
    if tool in LOW_TOOLS:
        return 'LOW', ''
    if tool == 'Bash':
        return classify_bash(ti.get('command', '')), tool_detail(tool, ti)
    if tool in MED_TOOLS:
        return 'MED', tool_detail(tool, ti)
    if tool.startswith('mcp__'):
        low = any(s in tool.lower() for s in MCP_LOW_SUBSTR)
        return ('LOW' if low else 'MED'), tool_detail(tool, ti)
    return 'MED', tool_detail(tool, ti)


def emit(decision, reason):
    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': decision,
        'permissionDecisionReason': reason}}, ensure_ascii=False))
    sys.exit(0)


def clip(s, n):
    return s if len(s) <= n else s[:n] + f'\n…（内容过长，共 {len(s)} 字，此处仅显示前 {n} 字）'


TAIL_BYTES = 250000


def _tail_dicts(path):
    """从会话转写尾部反向逐行取 JSON（截断行/坏行自动跳过）。"""
    with open(path, 'rb') as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        lines = f.read().decode('utf-8', 'replace').splitlines()
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if isinstance(d, dict):
            yield d


def _text_of(content):
    """消息纯文本：str 直取；list 只收 type=='text' 块（tool_result 自动跳过）。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get('text', '') for b in content
                 if isinstance(b, dict) and b.get('type') == 'text']
        return '\n'.join(p for p in parts if p.strip()).strip()
    return ''


def last_assistant_text(path):
    for d in _tail_dicts(path):
        if d.get('type') != 'assistant' or d.get('isSidechain'):
            continue
        text = _text_of((d.get('message') or {}).get('content'))
        if text:
            return text
    return ''


def last_user_text(path):
    for d in _tail_dicts(path):
        if d.get('type') != 'user' or d.get('isSidechain'):
            continue
        text = _text_of((d.get('message') or {}).get('content'))
        if text and not text.startswith(('<', 'Caveat:')):
            return text  # 过滤系统注入/XML标签类文本，只留真实用户输入
    return ''


TASK_TAG_RE = re.compile(r'【任务[:：]\s*([^】]{2,60})】')


def condense(text, n=30):
    """长文提炼成清单式短标签：按句读/逗号切段拼到 n 字为止。"""
    parts = [p.strip() for p in re.split(r'[。！？!?；;\n，,、]+', text.strip()) if p.strip()]
    out = ''
    for p in parts:
        out = f'{out}，{p}' if out else p
        if len(out) >= n:
            break
    return out[:n] + '…' if len(out) > n else out


def task_label(path):
    """两层制：优先助手回复带的【任务：…】短标签（全局CLAUDE.md约定），没有就提炼用户原话首句。"""
    seen = 0
    for d in _tail_dicts(path):
        if d.get('type') != 'assistant' or d.get('isSidechain'):
            continue
        m = TASK_TAG_RE.search(_text_of((d.get('message') or {}).get('content')))
        if m:
            return m.group(1).strip()[:40]
        seen += 1
        if seen >= 6:
            break
    return condense(last_user_text(path))


def transcript_context(path):
    """提取 (任务短标签≤30字, 本步目的≤60字)。读不到返回 ('','')，绝不抛错。"""
    try:
        task = task_label(path)
        goal = last_assistant_text(path).replace('\n', ' ').strip()
    except Exception:
        return '', ''
    if len(goal) > 60:
        goal = goal[:60] + '…'
    return task, goal


def wait_decision(tok, t0_ms, rid, tier, title, body):
    """推群 + 轮询表情表决（👍放行/👎拒绝；文字表决作废）。返回 'allow'/'deny'。网络异常向上抛。"""
    deadline = time.time() + AUTO_SEC
    tok_ts = time.time()
    reminded = 0
    req_mid = push(body, tok)
    if req_mid:
        # 预贴表情让消息下方长出👍/👎标签，用户单击标签即可表决（旧客户端兼容未验证，失败不致命）
        for e in ('THUMBSUP', 'ThumbsDown'):
            try:
                add_reaction(tok, req_mid, e)
            except Exception as ex:
                log(f'{rid} pre-reaction {e} failed: {ex!r}')
    seen_reactions = set()
    log(f'{rid} push ok tier={tier} mid={req_mid[-8:] if req_mid else "-"}')
    while time.time() < deadline:
        time.sleep(POLL_SEC)
        if os.getppid() == 1:
            sys.exit(0)  # 父会话已终止（如被中断/杀掉），不再推催办
        now = time.time()
        if now - tok_ts > 500:
            tok = get_token()
            tok_ts = now
        if req_mid:
            for reac_id, etype in reactions(tok, req_mid):
                if not reac_id or reac_id in seen_reactions:
                    continue
                seen_reactions.add(reac_id)
                if etype in ALLOW_EMOJI:
                    push(f'✅ 收到 {etype} 表情 → 已放行（{rid}）', tok)
                    log(f'{rid} emoji allow: {etype}')
                    return 'allow'
                if etype in DENY_EMOJI:
                    push(f'⛔ 收到 {etype} 表情 → 已拒绝（{rid}）', tok)
                    log(f'{rid} emoji deny: {etype}')
                    return 'deny'
                log(f'{rid} unknown emoji: {etype}')
        waited = now - (t0_ms / 1000)
        if reminded == 1 and waited >= REMIND_2:
            reminded = 2
            push(f'⏳ 催办2：即将自动裁决（{rid}）\n{title}', tok)
        elif reminded == 0 and waited >= REMIND_1:
            reminded = 1
            push(f'⏳ 催办1：等待表决（{rid}）\n{title}\n点原消息下方的 👍/👎 表情标签，一下即生效', tok)
    auto = 'deny' if tier == 'HIGH' else 'allow'
    word = '默认拒绝' if tier == 'HIGH' else '默认同意'
    push(f'⏰ 超时 15 分钟未回复（{rid}）→ 按{tier}级策略自动{word}，任务{"跳过此步" if auto == "deny" else "继续"}', tok)
    log(f'{rid} timeout auto={auto}')
    return auto


def main():
    raw = sys.stdin.read()
    data = json.loads(raw)
    # 1. cc-connect 无头会话自带审批通道，跳过防双推
    if os.environ.get('CC_SESSION_KEY') or os.environ.get('CC_CONNECT_PERMISSION_HOOK_SKIP'):
        sys.exit(0)
    # 2. 开关白名单：含 remote_approve 的操作秒过（防止自我卡死），顺手标绿=在干活
    if 'remote_approve' in raw:
        tmux_state('g')
        emit('allow', '离开模式开关操作白名单，秒过')
    tool = data.get('tool_name', '')
    tier, detail = classify(data)
    # 3. 状态色通用上报（不管离开模式开没开）：低危=干活绿；中危=黄待批；高危=红待批
    in_tmux = (tmux_state('g') if tier == 'LOW'
               else tmux_state('y' if tier == 'MED' else 'r', '待批'))
    # 4. 离开模式未开 → 不干预审批流程（颜色已经提示这窗在等本地批复）
    if not os.path.exists(MARKER):
        sys.exit(0)
    if tier == 'LOW':
        emit('allow', '低危操作，离开模式秒过')
    # 5. 非 tmux 窗口看不到状态条，弹 macOS 横幅兜底（标题带目录名）
    if not in_tmux:
        win = os.path.basename((data.get('cwd') or '').rstrip('/')) or '终端'
        banner(f'Claude·{win}·{"高" if tier == "HIGH" else "中"}危待批',
               '审批卡已推送，手机点 👍/👎 表决')
    rid = f'{int(time.time()) % 100000:05d}'
    tier_desc = '高危·超时默认拒绝' if tier == 'HIGH' else '中危·超时默认同意'
    icon = '🔴' if tier == 'HIGH' else '🟡'
    brief = summarize(tool, data.get('tool_input') or {})
    t_task, t_goal = transcript_context(data.get('transcript_path') or '')
    sess = tmux_session()
    win = os.path.basename((data.get('cwd') or '').rstrip('/')) or '终端'
    src = f'{sess}·{win}' if sess else f'非tmux·{win}'
    lines = [f'{icon} 离开模式审批（{tier_desc}）', f'来源：{src}']
    if t_task:
        lines.append(f'任务：「{t_task}」')
    if t_goal:
        lines.append(f'本步目的：{t_goal}')
    lines += [f'操作：{brief}',
              f'工具: {tool}',
              f'──── 完整内容 ────\n{clip(detail, 3000) or "(无)"}',
              '────────────────',
              f'ID: {rid}',
              '👇 单击本消息下方的 👍/👎 表情标签表决（群里打字会被当指令进窗口，表决请只点表情）',
              '2/3分钟催办，15分钟未回按危险度自动裁决']
    body = '\n'.join(lines)
    tok = get_token()
    t0_ms = int(time.time() * 1000)
    tmux_banner(tier, rid)  # 电脑端同步挂出黄/红文字横幅，等批复期间常驻
    decision = wait_decision(tok, t0_ms, rid, tier,
                             title=f'审批 {rid} ({tool})', body=body)
    tmux_banner_clear()
    if decision == 'allow':
        tmux_state('g')  # 放行即转绿：这窗在干活了
    else:
        tmux_state('r', '已拒绝拦停')
    reason = ('手机端已批准' if decision == 'allow'
              else '手机端已拒绝（或高危超时自动拒绝），该操作被拦下')
    emit(decision, reason)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        # 安全网：钩子自身出错绝不阻塞任务，回退本地弹问
        log(f'ERROR fallback to local prompt: {e!r}')
        sys.exit(0)
