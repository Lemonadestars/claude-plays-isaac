# Claude Plays Isaac

让 Claude 一帧一帧地玩《以撒的结合：忏悔》。游戏在 Claude 思考时完全冻结，Claude 决定接下来几帧怎么按键后，游戏推进这几帧，再次冻结，如此循环。录像会自动剪掉冻结的部分，成片看起来就是连贯的实时游戏。

*Claude plays The Binding of Isaac: Repentance frame by frame. The game freezes while Claude thinks, runs the few frames Claude decided on, then freezes again. Recordings are cut automatically so the final video looks like continuous play. English summary at the bottom.*

## 工作原理

```
以撒 (Lua Mod)  ──state.json──▶  agent.py  ──▶  Claude API
      ▲                              │
      └──────actions.json────────────┘
                                     └──▶ OBS (WebSocket) ──▶ cut_pauses.py ──▶ 成片
```

- **Mod (`main.lua`)**：每次游戏更新后导出状态（以撒位置和属性、敌人、子弹、掉落物和道具、门、整层小地图），然后冻结游戏，等待按键指令。
- **控制程序 (`agent.py`)**：读取状态，调用 Claude，把回复的按键写回给 Mod。
  - **决策 / 操作分工**：进入新房间、房间清空、出现道具或付费物品时由「决策」调用（开启思考）定下计划；其余时间由「操作」调用（关闭思考）按计划躲弹幕、射击、走路。两者默认都用 Opus 5.5。
  - 固定信息（房间布局、地图、当前构筑）放在缓存的系统提示词里，每一步只发送变化的部分，以降低成本。
  - 以撒死亡立即停止，不会重复同一种死法。
- **录制**：通过 OBS WebSocket 自动开始、暂停、恢复、停止录像。
- **剪辑 (`cut_pauses.py`)**：用 ffmpeg 找出画面静止的片段，再与记录的思考时间核对，只剪掉思考造成的冻结，精确到帧。可选铺一条连续的背景音乐。
- **中英字幕**：Claude 每一步的想法写入 `overlay.txt`，供 OBS 显示。

## 需要的东西

- 《以撒的结合：重生》+ 忏悔 DLC（Repentance 或 Repentance+），Windows
- Python 3.10+
- Claude API 密钥（[platform.claude.com](https://platform.claude.com/settings/keys)），**按量付费**
- 可选：OBS Studio 28+（自动录制），ffmpeg（剪辑）

## 安装

1. 把 `isaac_ai_bridge` 文件夹复制到以撒的 `mods` 目录，例如
   `Steam\steamapps\common\The Binding of Isaac Rebirth\mods\`
2. 在 Steam 里给以撒添加启动参数 `--luadebug`。
   > ⚠️ `--luadebug` 允许**所有**已启用的 Mod 读写你的文件。只启用你信任的 Mod。
3. 安装依赖：
   ```powershell
   pip install anthropic obsws-python
   winget install Gyan.FFmpeg      # 剪辑需要，装完重新打开 PowerShell
   ```
4. 设置环境变量（只需一次，之后重新打开窗口生效）：
   ```powershell
   setx ANTHROPIC_API_KEY "你的 Claude API 密钥"
   setx OBS_PASSWORD "OBS WebSocket 密码"
   ```

## 使用

1. 打开 OBS：`Tools → WebSocket Server Settings` 中开启服务器；添加以撒的 **Game Capture** 源；添加读取 `C:\isaac_ai\overlay.txt` 的 **Text (GDI+)** 字幕源（Edit Transform 中 Positional Alignment 设为 Bottom Center，字幕向上生长不会出界）。
2. 在 `isaac_ai_bridge` 目录运行：
   ```powershell
   python agent.py
   ```
3. 用 `--luadebug` 启动以撒并开一局。录像在第一个房间自动开始。
4. 运行期间**不要点击以撒以外的窗口**：以撒失去焦点会自动弹出暂停菜单。
5. 以撒死亡或按 `Ctrl+C` 结束后，录像会被自动剪辑，生成 `原文件名_cut.mp4`。

手动剪辑已有录像：
```powershell
python cut_pauses.py "录像.mkv"               # 画面检测 + 思考时间核对（默认）
python cut_pauses.py "录像.mkv" --detect      # 只看画面（没有 .pauses.json 时）
python cut_pauses.py "录像.mkv" --music bgm.mp3   # 加连续背景音乐
```

## 配置（`agent.py` 顶部）

| 设置 | 说明 |
| --- | --- |
| `STRATEGY_MODEL` / `PILOT_MODEL` | 决策和操作使用的模型 |
| `STRATEGY_THINKING` / `PILOT_THINKING` | 是否开启思考（关闭更便宜） |
| `MAX_FRAMES_PER_DECISION` | 每次决策最多推进的帧数（30 帧 = 1 秒） |
| `BGM_PATH` / `BGM_VOLUME` | 剪辑时铺的背景音乐（请将以撒自带音乐音量调为 0） |
| `SHOW_MODEL_IN_SUBTITLE` | 字幕前显示 `[Opus]` 等模型标签 |
| `OBS_ENABLED` | 是否自动录制 |

## 成本提示

每秒游戏时间至少调用一次 API。一局打到 Boss 大约需要数百次调用，实际时长约为成片的十倍。请在 Console 中关注用量，必要时把 `PILOT_MODEL` 换成更便宜的模型。

## 已知限制

- 道具名称多为本地化键名（如 `SAD ONION`），冷门道具的效果依赖模型自身的知识，可能判断错误。
- 只导出当前小地图可见的房间，不作弊。
- 菜单、过场动画和选择角色仍需手动操作。

## 致谢

思路来自让 Claude 冻结游戏、逐帧游玩《蔚蓝》的实验。

---

## English summary

A Lua mod freezes *The Binding of Isaac: Repentance* after every game update and exports the state (player, enemies, projectiles, pickups and items, doors, visible floor map) to a JSON file. `agent.py` sends it to Claude, which replies with inputs for the next 1–30 frames; the mod plays them and freezes again. A "strategist" call (extended thinking) handles items, deals and route planning on key moments; a cheaper "pilot" call follows its plan for dodging and shooting. OBS is driven over WebSocket to record automatically, and `cut_pauses.py` uses ffmpeg frame-freeze detection, cross-checked against logged thinking times, to remove every frozen stretch frame-accurately. Optional looped background music and bilingual (Chinese/English) subtitles.

**Setup:** copy `isaac_ai_bridge` into Isaac's `mods` folder, add the `--luadebug` launch option (this lets every enabled mod access your files), `pip install anthropic obsws-python`, install ffmpeg, set `ANTHROPIC_API_KEY` and `OBS_PASSWORD`, then run `python agent.py` and start a run. API usage is billed per call; a run to the first boss takes hundreds of calls.
