# claude双端互通 · 安装说明书

把这台电脑上的 Claude Code 变成"手机可遥控、可审批、可看画面"的双端工作台。
本包是 **macOS 版**（Linux 大部分可用，差异见文末；Windows 原生不支持，需 WSL2，见文末）。

## AI 执行须知（如果安装任务是交给 Claude Code 之类 AI 执行的，先读这节）

- **你自己跑的步**：第 1、3、5、6、7、8、10 步（装依赖、铺文件、合并配置、起服务、验收）全部照命令执行即可。
- **必须停下来找用户的步**：
  - 第 2 步（飞书开放平台建应用/开权限/拉群）——网页后台人工操作，AI 无法代办。开工前先问用户要：`app_id`、`app_secret`、`group_chat_id`、`p2p_chat_id`；用户已备好 `~/.claude/remote/feishu.json` 的话直接跳到第 5 步。
  - 第 4 步的四个值**只能由用户提供**，严禁编造或猜测。
- **合并 settings.json / zshrc / tmux.conf 时**：目标文件已存在就先 Read 再 Edit 做追加/合并，禁止整文件覆盖。
- **验收（第 10 步）排错顺序**：①watcher 起不来 → `tail ~/.claude/remote/watcher.log`（多半是 feishu.json 缺失或群号错、机器人没在群里）；②群收不到卡 → 手动跑 token 获取命令看 code 是否 0（权限没发布/secret 错），再 `tail ~/.claude/hooks/remote_approve.log`；③表情点了不生效 → 检查应用是否开了「读取消息表情回复」权限；④开关不响应 → watcher 进程是否在（`pgrep -f tmux_feishu_watch`）、消息是否 ≤12 字。
- **红线**：验收任何一条不过，不得绕过安全机制（如把中高危改成秒过、关掉超时策略）来"让它通过"；报告用户等指示。
- 装完后 `~/.claude/remote/feishu.json` 含密钥，权限保持 600，不要打进任何提交/日志。

## 装好后能干什么

| 能力 | 载体 |
|---|---|
| 手机上直接跟电脑 Claude 对话 | cc-connect（独立桥，本包不含，可选） |
| 离开工位后，Claude 的每次危险操作推手机审批，👍/👎 表情表决 | 本包 hooks |
| 手机群发一句话 → 直接打进电脑 tmux 窗口（遥控器） | 本包 watcher |
| 绑定窗口画面每稳定 6 秒自动推群（看现场） | 本包 watcher |
| 手机说「打开/关闭离开模式」程序化生效（不依赖任何 AI 理解） | 本包 watcher |
| 窗口状态条变色：绿=干活 黄=待批 红=高危/被拒 灰=空闲；待批时整条状态行挂文字横幅 | 本包 tmux.conf + hooks |
| 人回到电脑动一下键盘 → 自动解除离开模式 + 🔓 通知手机 | 本包 sentry + launchd |

## 第 0 步 · 包内文件 → 安装目标位置总览

```
本包目录结构                          安装到（新电脑上）
├── README.md                        （本说明书，不用装）
├── config.example.json              → ~/.claude/remote/feishu.json（照抄后填写真实值）
├── hooks/
│   ├── remote_approve.py            → ~/.claude/hooks/remote_approve.py
│   ├── remote_done.py               → ~/.claude/hooks/remote_done.py
│   └── leave_sentry.py              → ~/.claude/hooks/leave_sentry.py
├── remote/
│   └── tmux_feishu_watch.py         → ~/.claude/remote/tmux_feishu_watch.py
├── dotfiles/
│   ├── tmux.conf                    → ~/.tmux.conf（若已有则合并内容）
│   ├── zshrc-claude-function.sh     → 追加进 ~/.zshrc 末尾
│   └── claude-CLAUDE.md             → ~/.claude/CLAUDE.md（若已有则合并；可选）
├── snippets/
│   └── settings-hooks.json          → 合并进 ~/.claude/settings.json 的 hooks 段
└── launchd/
    └── com.claude-remote.leave-sentry.plist → ~/Library/LaunchAgents/（先替换 __HOME__）
```

运行时会自动生成的文件（不用创建）：
- `~/.remote_approve_on` —— 离开模式标记，存在=已开启
- `~/.claude/remote/watcher_target` —— 遥控器当前绑定的 tmux 目标
- `~/.claude/remote/leave_sentry.log`、`~/.claude/hooks/remote_approve.log` —— 日志

## 第 1 步 · 前置软件

```bash
# 缺哪个装哪个（需要 Homebrew，装法见 brew.sh 官网）
brew install tmux python@3.12
tmux -V          # 建议 ≥3.2（本机 3.7c 验证）
python3 -V       # 脱敏后的脚本只依赖标准库，系统 python3 也能跑；但哨兵日志 plist 里用 /usr/bin/python3 即可
claude --version # Claude Code 本体，另行安装
```

## 第 2 步 · 创建飞书自建应用（拿 4 个值）

1. 打开 open.feishu.cn → 开发者后台 → 创建**企业自建应用**
2. 「应用能力」里启用**机器人**
3. 「权限管理」搜索 **im** ，把消息相关权限全部开通并发起版本发布（管理员审批通过后生效），至少需要：
   - 获取与发送单聊/群聊消息（发消息、读群历史消息都要）
   - 读取消息的表情回复（审批卡靠 👍/👎 表决，必须）
4. 「凭证与基础信息」页拿到 **app_id**（`cli_` 开头）和 **app_secret**
5. 建一个群（名字随意，建议叫「claude遥控器」），在群设置里**把机器人应用拉进群**
6. 拿两个 chat_id：
   - **群 chat_id**（`oc_` 开头）：`curl` 调 `GET https://open.feishu.cn/open-apis/im/v1/chats`（先按第 7 步拿 token）从列表里按群名找；或临时跑一次 watcher 前用机器人被拉进群时事件里的 chat_id
   - **私聊 chat_id**：先跟机器人单聊发一句话，再调 `GET /open-apis/im/v1/chats`，列表里 `chat_mode == "p2p"` 的那条
7. 手动拿一次 token 验证通路（把 app_id/secret 换成真实值）：
```bash
curl -sX POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal \
  -H 'Content-Type: application/json' \
  -d '{"app_id":"cli_xxx","app_secret":"xxx"}' | head -c 200
# 返回里 code=0 且出现 tenant_access_token 即通
```

## 第 3 步 · 铺文件

```bash
mkdir -p ~/.claude/hooks ~/.claude/remote
cd <本包目录>
cp hooks/*.py          ~/.claude/hooks/
cp remote/tmux_feishu_watch.py ~/.claude/remote/
```

## 第 4 步 · 写凭据配置

```bash
cp config.example.json ~/.claude/remote/feishu.json
chmod 600 ~/.claude/remote/feishu.json
# 编辑 ~/.claude/remote/feishu.json，把第 2 步拿到的 4 个值填进去
```

## 第 5 步 · 注册 Claude Code hooks

打开 `~/.claude/settings.json`（没有就新建），把 `snippets/settings-hooks.json` 里的
`PreToolUse` 和 `Stop` 两段**合并进已有的 hooks 对象**（注意别覆盖你原有配置；
若已有其它 Stop 钩子，把我们那条追加进数组即可）。

## 第 6 步 · tmux / zshrc / 任务标签约定

```bash
# tmux 配色：没有就整拷；已有内容就把本包 dotfiles/tmux.conf 的行追加进去
[ -f ~/.tmux.conf ] && cat dotfiles/tmux.conf >> ~/.tmux.conf || cp dotfiles/tmux.conf ~/.tmux.conf

# claude 自动进 tmux：把函数追加进 zshrc（bash 用户同样可用，但 ${(@q)} 是 zsh 语法，bash 跳过此步）
cat dotfiles/zshrc-claude-function.sh >> ~/.zshrc && source ~/.zshrc

# 【任务：…】标签约定（让审批卡显示任务名，可选但推荐）
[ -f ~/.claude/CLAUDE.md ] && cat dotfiles/claude-CLAUDE.md >> ~/.claude/CLAUDE.md || cp dotfiles/claude-CLAUDE.md ~/.claude/CLAUDE.md
```

## 第 7 步 · 键盘哨兵自启（launchd，macOS）

```bash
sed "s|__HOME__|$HOME|g" launchd/com.claude-remote.leave-sentry.plist \
  > ~/Library/LaunchAgents/com.claude-remote.leave-sentry.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.claude-remote.leave-sentry.plist
launchctl print gui/$(id -u)/com.claude-remote.leave-sentry | grep state   # 期望 running
```

## 第 8 步 · 启动遥控器 watcher

```bash
cd ~/.claude/remote
nohup python3 tmux_feishu_watch.py >> watcher.log 2>&1 &
tail -1 watcher.log        # 期望 [watcher] started, target=...
```

（想开机自启：参照第 7 步再写一个 plist，把 ProgramArguments 指向本脚本；本包未附带模板，需要可自行仿写。）

## 第 9 步 ·（可选）cc-connect 手机直聊

本包四大能力都**不依赖** cc-connect；只有"手机上直接跟 Claude 对话"这条线需要它。
cc-connect 是独立第三方桥（launchd 守护，配置 config.toml），按其官方文档安装，
飞书 app_id/secret 用第 2 步同一个应用，并把 `admin_from` 填成你自己的 open_id。
**注意：一台电脑上的 cc-connect 和另一台的不要共用同一个飞书应用**（事件长连接互抢）。

## 第 10 步 · 验收清单

1. 手机群（或私聊机器人）发「打开离开模式」→ 秒回 ✅ 确认，电脑 `ls ~/.remote_approve_on` 存在
2. 电脑上开一个 tmux 窗口跑 `claude`，让它执行任意 Edit → 手机群收到四层审批卡（含来源/任务/目的/操作/全文），电脑该窗口状态条变黄并挂「🟡 待审批」整行横幅 → 点 👍 → 操作放行、状态变绿、横幅消失
3. 等它说完 → 手机收到 🏁 完成通知、状态复位灰
4. 群里发「#列表」→ 回窗口列表；发「#切 名字」→ 绑定成功；再说一句普通话 → 打进那个窗口；窗口画面稳定后自动推「【xxx 画面】」
5. 开着离开模式回到电脑敲几下键盘 → 自动解除 + 🔓 通知
6. 群发「关闭离开模式」→ ✅，之后审批恢复本地弹问

## 日常速查

- 开关：群里/私聊说「打开离开模式」「关闭离开模式」（≤12 字短句才触发，防误伤）
- 表决：**只认表情**。点卡片消息下方的 👍/👎 标签。群里打字会被当指令进窗口
- 遥控指令：`#列表`、`#切 名字`（切面板必须冒号：`#切 12:0.1`，点号 `12.0.1` 会被拒）
- 颜色：绿=在干活（含刚放行） 黄=中危待批（15 分钟未回自动放行） 红=高危待批或被拒（高危超时自动拒绝） 灰=空闲
- tmux 常用：`Ctrl-B D` 脱离（手机继续遥控）；`tmux attach -t 名字` 回来；滚轮直接翻历史（鼠标已开）
- 日志：`~/.claude/remote/watcher.log`、`~/.claude/remote/leave_sentry.log`、`~/.claude/hooks/remote_approve.log`

## 卸载

```bash
launchctl bootout gui/$(id -u)/com.claude-remote.leave-sentry
rm ~/Library/LaunchAgents/com.claude-remote.leave-sentry.plist
pkill -f tmux_feishu_watch
rm ~/.claude/hooks/remote_approve.py ~/.claude/hooks/remote_done.py ~/.claude/hooks/leave_sentry.py
rm -rf ~/.claude/remote
# settings.json 里删掉两段 hook；zshrc/tmux.conf 里删掉对应行
```

## 限制与注意

- **安全模型**：离开模式只是把"是否放行"的决策权搬到手机，审批卡内容含完整操作详情；不要把群拉进不相干的人——群里任何人都能点 👍/👎 表决，普通文字消息也都会打进绑定窗口
- **rm 白名单**：含 `remote_approve` 字样的操作秒过（防止开关卡死自己），知悉即可
- **低危不打扰**：读文件/搜索/只读命令永远秒过，不推手机
- **keyboard sentry 语义**：只有"真实键鼠"能触发自动解除；SSH、程序模拟输入都不算（这是特性：远程操作不会把离开模式弄关）
- **双端别同时活跃**：手机 cc-connect 会话和电脑 tmux 会话操作同一项目时，一次只在一端动手

## 跨系统说明

- **Linux**：hooks/watcher/审批/遥控/变色全可用（tmux 通用）；两处降级——非 tmux 窗口的 macOS 横幅（osascript）失效、键盘哨兵失效（HIDIdleTime 读不到，`idle_sec()` 返回 None 自动静默）→ 离开模式只能用关键词关。plist 换成 systemd user service 即可自启。
- **Windows**：原生无 tmux，整套遥控器/配色/镜像跑不了。**推荐 WSL2**：等同于 Linux 情况（哨兵同样失效，键鼠识别不到 WSL 外）。若只要"审批推手机"这一半功能，可以在原生 Windows 装 Claude Code + 单独用 hooks（去掉 tmux_state/osascript 调用后 remote_approve.py 可纯 python 运行），但遥控器镜像需要 Linux/macOS。
