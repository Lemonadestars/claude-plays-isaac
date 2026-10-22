"""
Claude plays The Binding of Isaac, frame-stepped.

The Lua mod freezes the game and writes state.json; this script asks Claude for
the next few frames of input, writes actions.json, and the game advances exactly
that many updates (30 updates = 1 second of game time) before freezing again.

Setup:
    pip install anthropic
    set ANTHROPIC_API_KEY=sk-ant-...        (PowerShell: $env:ANTHROPIC_API_KEY="...")
    python agent.py

Safety rails learned from the Celeste run:
  * Fail fast on death: stop and report instead of silently re-running the same plan.
  * Stall detection: if the game stops producing states (menu, pause, cutscene), say so.
  * Hard cap on frames per decision and on thinking time.
"""

import json
import os
import time
from pathlib import Path

import anthropic

BRIDGE_DIR = Path("C:/isaac_ai")          # must match BRIDGE_DIR in main.lua
STATE_PATH = BRIDGE_DIR / "state.json"
ACT_PATH = BRIDGE_DIR / "actions.json"
ACT_TMP = BRIDGE_DIR / "actions.tmp"
# Subtitles for OBS ("Text (GDI+)" source -> "Read from file"). Pick one layout:
OVERLAY_PATH = BRIDGE_DIR / "overlay.txt"        # both languages, Chinese line first
OVERLAY_ZH_PATH = BRIDGE_DIR / "overlay_zh.txt"  # Chinese only
OVERLAY_EN_PATH = BRIDGE_DIR / "overlay_en.txt"  # English only
LOG_PATH = BRIDGE_DIR / "log.jsonl"

# Two models: a strategist for decisions that matter, a cheap pilot for the rest.
STRATEGY_MODEL = "claude-opus-5-5"        # items, deals, shops, route planning
PILOT_MODEL = "claude-opus-5-5"           # dodging, shooting, walking (was Haiku: too
                                          # error-prone; change back to save money)
# The strategist is called when: you enter a room, a room becomes clear, or the
# set of notable pickups changes (new item/trinket/chest, anything with a price).
# Its plan is handed to the pilot until the next strategist call.
STRATEGY_THINKING = None                  # None = model default (thinks when useful)
PILOT_THINKING = {"type": "disabled"}     # keep the pilot fast and cheap
SHOW_MODEL_IN_SUBTITLE = True             # prefix subtitles with the model name
MODEL_TAGS = {"claude-opus-5-5": "Opus", "claude-sonnet-5-5": "Sonnet", "claude-haiku-5-5": "Haiku"}
NOTABLE_KINDS = {"item", "trinket", "card", "pill", "chest", "locked_chest", "empty_pedestal"}

MAX_FRAMES_PER_DECISION = 30               # cap while enemies are alive (1s of game time)
MAX_FRAMES_WHEN_CLEAR = 60                 # cap once the room is clear (walking, looting)
STALL_SECONDS = 20                         # no new state for this long -> warn
HISTORY_TURNS = 2                          # recent decisions kept in context

# Parts of the state that rarely change inside one room. They go into the cached
# system prompt (resent only when they change) instead of every single message.
STATIC_PLAYER_KEYS = ("items", "stats", "active", "trinket")
STATIC_ROOM_KEYS = ("stage", "room_index", "room_type", "bounds", "obstacles", "map")

# --- Automatic recording with OBS (OBS 28+ has the WebSocket server built in) ---
# OBS: Tools -> WebSocket Server Settings -> Enable, note the port and password.
# The recording is paused while the game is frozen waiting for Claude, so the
# video contains only real gameplay. Set OBS_ENABLED = False to skip this.
OBS_ENABLED = True
OBS_HOST = "localhost"
OBS_PORT = 4455
OBS_PASSWORD = os.environ.get("OBS_PASSWORD", "")
DEATH_TAIL_SECONDS = 4                     # keep recording this long after death
# Continuous background music for the cut video (set Isaac's own music volume to 0,
# keep sound effects). Leave empty for no music. Use music you may publish.
BGM_PATH = ""                              # e.g. r"C:\music\bgm.mp3"
BGM_VOLUME = 0.35

SYSTEM = """You are playing The Binding of Isaac. The game is frozen while you think.
Each turn you get the game state as JSON (positions in game pixels, y grows downward;
velocities in pixels per update; 30 updates = 1 second). Decide the inputs for the
next 1 to 30 updates (up to 60 once the room is clear). Use few frames when
projectiles or enemies are close; use many frames for long safe walks to save time.
The room layout and your build are given once in the system prompt; each message
only carries what changes (positions, enemies, projectiles, pickups, doors, hearts).

Reply with ONLY a JSON object:
{"thought_en": "<one short sentence in English, at most 15 words, shown as a subtitle>",
 "thought_zh": "<the same thought in natural Simplified Chinese, at most 30 characters>",
 "plan": [{"move": "<left|right|up|down|upleft|upright|downleft|downright|none>",
           "shoot": "<left|right|up|down|none>",
           "bomb": false, "item": false, "pillcard": false, "frames": <int>}]}
"item" uses the active item, "pillcard" uses the held card or pill.

Combat: tears fly straight in the shooting direction. Dodge projectiles by moving
perpendicular to their velocity. Clear the room, then walk through an open door.
Avoid walking into obstacles.

Items and pickups (you decide, nothing is picked up automatically on your behalf):
- Touching a pickup takes it. To SKIP something, just steer around it.
- state.pickups lists everything in the room. kind "item" is a pedestal item with
  name, desc and quality (0 = bad, 4 = great). Item names may look like "SAD ONION";
  use what you know about The Binding of Isaac to judge what each item does.
- price > 0 costs coins. price < 0 is a devil deal paid in hearts (-1 = one red
  heart container, -2 = two, -3 = three soul hearts, ...). Never take a heart deal
  that would leave you with no hearts.
- Pickups sharing a choice_group: taking one makes the others vanish. Pick the best.
- state.player.items is your current build and state.player.stats your stats. Judge
  new items against them: synergies, whether an active item replaces the one you
  hold, and whether something hurts your build (e.g. lowers damage or range).
- Some items are bad to take blindly (curses, risky actives). If unsure, explain
  your reasoning in your thoughts (thought_en and thought_zh) before deciding.
Before walking over any item, say in your thoughts (thought_en and thought_zh) what it does and why you take or skip it.

Route planning (the map is in the system prompt, doors say where they lead):
- map.rooms lists every room you can see on the minimap: type, visited, clear.
  doors[].leads_to tells you what is behind each door of the current room.
- Think across rooms, not just this one. Examples:
  * Before taking an item or pill that fully heals you, check whether a sacrifice
    room (spikes) is reachable: taking spike hits first and healing afterwards wastes
    nothing. This only pays off for full heals; a plain +1 heart container gains little.
  * Visit treasure rooms, shops and other special rooms before the boss.
  * Spend coins only after you know what the shop offers; keep a key for the
    treasure room or shop if one is locked.
- When you enter a new room or floor, state your route plan briefly in your thoughts (thought_en and thought_zh)."""

STRATEGIST_ROLE = """
ROLE: STRATEGIST. You are called only at decision points (new room, room cleared,
new or changed pickups). A cheaper pilot model plays the frames between your calls
and follows your strategy. Besides "plan", add a "strategy" field to your JSON:
"strategy": "<short, concrete instructions for the pilot in English: what to take,
what to avoid touching, which door to leave through and why, any danger to watch>"
Be explicit about pickups to AVOID, since the pilot will take whatever it touches."""

PILOT_ROLE = """
ROLE: PILOT. You handle dodging, shooting and walking. A strategist has decided what
to do in this room; follow its strategy below and do not contradict it.
Do not overshoot: when walking to a target, compute the distance from your position
and velocity and plan only enough frames to reach it; stop short and re-check rather
than run past it. Never pick up an item, trinket or
paid pickup the strategy did not approve (steer around it). Keep thoughts short.
Strategist's current instructions:
"""


def is_strategic(state, prev_state, last_strategy_room):
    """True when the expensive model should decide."""
    if prev_state is None or state["room_index"] != last_strategy_room:
        return True                                   # entered a room
    if state.get("room_clear") and not prev_state.get("room_clear"):
        return True                                   # room just became clear
    def notable(s):
        return sorted(
            json.dumps([p.get("kind"), p.get("sub"), p.get("price")])
            for p in s.get("pickups", [])
            if p.get("kind") in NOTABLE_KINDS or (p.get("price") or 0) != 0)
    return notable(state) != notable(prev_state)      # item appeared, vanished or changed


def read_state(last_seq):
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):   # missing, mid-write or locked: try again next poll
        return None
    return data if data.get("seq", -1) != last_seq else None


def write_actions(frames, disable=False):
    payload = {"frames": frames}
    if disable:
        payload["disable"] = True
    ACT_TMP.write_text(json.dumps(payload), encoding="utf-8")
    # Windows refuses to replace a file the game is reading at that instant: retry.
    deadline = time.time() + 5
    while True:
        try:
            os.replace(ACT_TMP, ACT_PATH)
            return
        except PermissionError:
            if time.time() > deadline:
                raise
            time.sleep(0.005)


class Recorder:
    """Keeps thinking time out of the video, two ways at once:
    1. pauses OBS while the game is frozen (works when OBS allows pausing), and
    2. logs every frozen stretch on the video timeline; after recording stops,
       cut_pauses.py cuts whatever OBS did not manage to pause (needs ffmpeg).
    """

    MIN_GAP_MS = 120                  # frozen stretches shorter than this are kept

    def __init__(self):
        self.client = None
        self.running = False
        self.paused = False
        self.freeze_at = None         # video time (ms) when the current freeze began
        self.gaps = []                # [start_ms, end_ms] still present in the video
        self.warned = False
        self.reported = set()
        if not OBS_ENABLED:
            return
        import logging
        logging.getLogger("obsws_python").setLevel(logging.CRITICAL)   # we report errors ourselves
        try:
            import obsws_python as obs
            self.client = obs.ReqClient(host=OBS_HOST, port=OBS_PORT,
                                        password=OBS_PASSWORD, timeout=3)
            print("[obs] Connected. Recording starts with the first room.")
        except ImportError:
            print("[obs] Missing package: run  pip install obsws-python   (recording disabled)")
        except Exception as e:
            print(f"[obs] Could not connect ({e}). Is OBS open with the WebSocket server on? "
                  "(recording disabled)")

    def _call(self, name, quiet=False):
        if not self.client:
            return None
        try:
            return getattr(self.client, name)()
        except Exception as e:
            # 502/503 = "already paused" / "not paused": harmless, the cut handles it.
            if not (quiet and any(c in str(e) for c in ("502", "503"))):
                if name not in self.reported:
                    self.reported.add(name)
                    print(f"[obs] {name} failed: {e}  (further errors of this kind are hidden)")
            return None

    def _video_ms(self):
        st = self._call("get_record_status")
        return getattr(st, "output_duration", None) if st else None

    def freeze(self):
        """Game just froze for a decision."""
        if not self.client:
            return
        if not self.running:
            if getattr(self._call("get_record_status"), "output_active", False):
                print("[obs] OBS was already recording (started by hand?). Stop it and let "
                      "agent.py start the recording, or pauses can't be cut.")
            self._call("start_record")
            time.sleep(0.5)           # OBS needs a moment before it accepts a pause
            self.running = True
        if not self.paused:
            self.freeze_at = 0 if self.freeze_at is None else self._video_ms()
            self._call("pause_record", quiet=True)
            self.paused = True

    def unfreeze(self):
        """About to let the game run again."""
        if not (self.client and self.running and self.paused):
            return
        now = self._video_ms()
        if self.freeze_at is not None and now is not None and now - self.freeze_at > self.MIN_GAP_MS:
            self.gaps.append([self.freeze_at, now])   # pause did not stop the clock
            if not self.warned and self.freeze_at > 0:     # the opening gap is expected
                self.warned = True
                print("[obs] OBS kept recording while paused. The frozen parts will be cut "
                      "after recording (see notes in cut_pauses.py).")
        self._call("resume_record", quiet=True)
        self.paused = False

    def stop(self, tail=0):
        if not (self.client and self.running):
            return
        if tail:
            self.unfreeze()
            time.sleep(tail)
        resp = self._call("stop_record")
        self.running = False
        path = getattr(resp, "output_path", None) if resp else None
        print(f"[obs] Recording saved: {path or '(see OBS Settings -> Output for the folder)'}")
        if not path:
            print("[cut] OBS did not report the file path; cut it by hand with cut_pauses.py.")
        elif not self.gaps and not BGM_PATH:
            print("[cut] Nothing to cut: OBS paused correctly, the recording has no frozen parts.")
        if path and (self.gaps or BGM_PATH):
            time.sleep(2)             # let OBS finish writing the file
            gaps_file = Path(path).with_suffix(".pauses.json")
            gaps_file.write_text(json.dumps(self.gaps), encoding="utf-8")
            try:
                from cut_pauses import smart_cut
                smart_cut(path, self.gaps, music=BGM_PATH or None, music_volume=BGM_VOLUME)
            except Exception as e:
                print(f"[cut] Could not cut automatically ({e}). Run later:  "
                      f'python cut_pauses.py "{path}"')


def split_state(state):
    """Return (static, dynamic): static = room layout + build, dynamic = everything else."""
    static = {k: state.get(k) for k in STATIC_ROOM_KEYS}
    static["player"] = {k: state["player"].get(k) for k in STATIC_PLAYER_KEYS}
    dynamic = {k: v for k, v in state.items() if k not in STATIC_ROOM_KEYS}
    dynamic["player"] = {k: v for k, v in state["player"].items() if k not in STATIC_PLAYER_KEYS}
    return static, dynamic


def build_system(static, strategist, strategy=""):
    """Rules + role + room are cached; the strategist's latest plan goes after the cache."""
    room = json.dumps(static, separators=(",", ":"))
    blocks = [
        {"type": "text", "text": SYSTEM + (STRATEGIST_ROLE if strategist else PILOT_ROLE)},
        {"type": "text",
         "text": "Current room layout and your build (fixed until you change room or items):\n" + room,
         "cache_control": {"type": "ephemeral"}},
    ]
    if not strategist:
        blocks.append({"type": "text", "text": "Strategy: " + (strategy or
                       "(none yet) Fight carefully and do not pick up items.")})
    return blocks


class Brain:
    """Calls the right model and keeps per-model thinking settings and usage."""

    def __init__(self, client):
        self.client = client
        self.thinking = {"strategist": STRATEGY_THINKING, "pilot": PILOT_THINKING}
        self.usage = {}

    def ask(self, role, model, system, messages):
        kwargs = dict(model=model, system=system, messages=messages, timeout=120,
                      max_tokens=8000 if role == "strategist" else 1000)
        if self.thinking.get(role) is not None:
            kwargs["thinking"] = self.thinking[role]
        try:
            resp = self.client.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            if "thinking" not in kwargs or "thinking" not in str(e).lower():
                raise
            print(f"[info] {model} does not accept the thinking setting; using its default.")
            self.thinking[role] = None
            kwargs.pop("thinking")
            resp = self.client.messages.create(**kwargs)

        u = resp.usage
        t = self.usage.setdefault(f"{model} ({role})", {"calls": 0, "input": 0, "cache_read": 0,
                                          "cache_write": 0, "output": 0})
        t["calls"] += 1
        t["input"] += u.input_tokens or 0
        t["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        t["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        t["output"] += u.output_tokens or 0

        # Thinking blocks come before the answer; keep only text.
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        if not text:
            raise ValueError("no text block in response")
        return text, u

    def report(self):
        for model, t in self.usage.items():
            print(f"   [usage] {model}: {t['calls']} calls | input {t['input']} | "
                  f"cache read {t['cache_read']} | cache write {t['cache_write']} | "
                  f"output {t['output']} tokens")


def expand(plan, cap=MAX_FRAMES_PER_DECISION):
    frames = []
    for step in plan:
        n = max(1, int(step.get("frames", 1)))
        frame = {
            "move": step.get("move", "none"),
            "shoot": step.get("shoot", "none"),
            "bomb": bool(step.get("bomb", False)),
            "item": bool(step.get("item", False)),
            "pillcard": bool(step.get("pillcard", False)),
        }
        frames.extend([frame] * n)
        if len(frames) >= cap:
            break
    frames = frames[:cap]
    # bomb/item only on the first frame they appear, so we don't spam them
    for i in range(1, len(frames)):
        for key in ("bomb", "item", "pillcard"):
            if frames[i][key] and frames[i - 1][key]:
                frames[i] = dict(frames[i], **{key: False})
    return frames or [{"move": "none", "shoot": "none", "bomb": False, "item": False, "pillcard": False}]


def parse_reply(text):
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def log(entry):
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main():
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    for stale in (STATE_PATH, ACT_PATH):      # never act on a previous session's files
        stale.unlink(missing_ok=True)
    brain = Brain(anthropic.Anthropic())
    recorder = Recorder()
    print(f"Strategist: {STRATEGY_MODEL} | Pilot: {PILOT_MODEL}")
    print("Waiting for the game... (launch Isaac with --luadebug and start a run)")
    try:
        run(brain, recorder)
    except KeyboardInterrupt:
        print("\n[stop] Stopped by you.")
    finally:
        recorder.stop()
        brain.report()


SUB_WIDTH_ZH = 24                          # characters per subtitle line (Chinese)
SUB_WIDTH_EN = 56                          # characters per subtitle line (English)


def wrap(text, width, cjk):
    if not text:
        return ""
    if cjk:
        return "\n".join(text[i:i + width] for i in range(0, len(text), width))
    import textwrap
    return "\n".join(textwrap.wrap(text, width)) or text


def write_subtitles(thought_zh, thought_en, tag):
    prefix = f"[{tag}] " if SHOW_MODEL_IN_SUBTITLE and tag else ""
    thought_zh = wrap(prefix + thought_zh if thought_zh else "", SUB_WIDTH_ZH, cjk=True)
    thought_en = wrap(thought_en, SUB_WIDTH_EN, cjk=False)
    prefix = ""                                # already part of the first line
    OVERLAY_ZH_PATH.write_text(prefix + thought_zh if thought_zh else "", encoding="utf-8")
    OVERLAY_EN_PATH.write_text(prefix + thought_en if thought_en else "", encoding="utf-8")
    lines = [t for t in (thought_zh, thought_en) if t]
    if lines:
        lines[0] = prefix + lines[0]
    OVERLAY_PATH.write_text("\n".join(lines), encoding="utf-8")


def run(brain, recorder):
    history = []
    last_seq = None
    last_static = None
    prev_state = None
    strategy = ""
    strategy_room = None
    calls = 0
    last_state_time = time.time()
    expected_by = None              # when the game should have produced the next state

    while True:
        state = read_state(last_seq)
        if state is None:
            if expected_by and time.time() > expected_by:
                expected_by = None
                recorder.freeze()   # unexpected stop (pause menu?): keep it out of the video
                print("[stall] The game stopped unexpectedly, probably Isaac's pause menu "
                      "(it opens when the game window loses focus). Click RESUME GAME in Isaac. "
                      "Recording is paused until the game runs again.")
            if last_seq is not None and time.time() - last_state_time > STALL_SECONDS:
                print(f"[stall] Still waiting ({STALL_SECONDS}s): pause menu, menu or cutscene? "
                      "Resume or confirm it in the game.")
                last_state_time = time.time()
            time.sleep(0.05)
            continue

        last_seq = state["seq"]
        last_state_time = time.time()
        expected_by = None

        if state.get("lua_error"):
            print(f"[lua error] The mod could not read the game state:\n    {state['lua_error']}\n"
                  "Stopping. Send this message to Claude to get it fixed.")
            log({"event": "lua_error", "error": state["lua_error"]})
            recorder.unfreeze()
            write_actions([], disable=True)   # hand control back to you
            return

        # Fail fast: death ends the run. Stop and hand control back to the human.
        if state["player"]["dead"]:
            print("[dead] Isaac died. Stopping so you can review log.jsonl before the next run.")
            recorder.unfreeze()
            write_actions([], disable=True)
            log({"event": "death", "state": state})
            recorder.stop(tail=DEATH_TAIL_SECONDS)   # keep the death animation in the video
            return

        recorder.freeze()                     # game is frozen now: pause the video

        static, dynamic = split_state(state)
        if last_static is not None and static["room_index"] != last_static["room_index"]:
            history = []                      # old room's turns are useless in a new room
            strategy = ""
        last_static = static

        strategic = is_strategic(state, prev_state, strategy_room)
        model = STRATEGY_MODEL if strategic else PILOT_MODEL
        tag = MODEL_TAGS.get(model, model) + ("·决策" if strategic and STRATEGY_MODEL == PILOT_MODEL else "")
        system = build_system(static, strategic, strategy)
        user_msg = {"role": "user", "content": json.dumps(dynamic, separators=(",", ":"))}

        try:
            text, u = brain.ask("strategist" if strategic else "pilot", model, system, history + [user_msg])
            decision = parse_reply(text)
        except Exception as e:  # API error or unparseable reply: stand still one frame, retry
            print(f"[error] {model}: {e}")
            recorder.unfreeze()
            write_actions(expand([{"move": "none", "shoot": "none", "frames": 1}]))
            continue                           # prev_state unchanged, so a failed
                                               # strategist call is retried next time
        prev_state = state
        if strategic:
            strategy = decision.get("strategy", "") or strategy
            strategy_room = state["room_index"]

        safe = state.get("room_clear") and not state.get("projectiles")
        frames = expand(decision.get("plan", []),
                        cap=MAX_FRAMES_WHEN_CLEAR if safe else MAX_FRAMES_PER_DECISION)

        thought_en = decision.get("thought_en") or decision.get("thought", "")
        thought_zh = decision.get("thought_zh", "")
        if not thought_en or not thought_zh:
            print(f"   [subtitle] model left out {'thought_en' if not thought_en else 'thought_zh'}")
        # Subtitles are written before the video resumes, so each clip opens with its thought.
        write_subtitles(thought_zh, thought_en, tag)
        recorder.unfreeze()                   # resume video first, then release the game
        write_actions(frames)
        expected_by = time.time() + len(frames) / 30 + 2.0

        calls += 1
        print(f"#{last_seq} {tag} [{len(frames)}f] {thought_zh} / {thought_en}")
        if strategic and strategy:
            print(f"   [strategy] {strategy}")
        if calls % 20 == 0:
            brain.report()
        log({"seq": last_seq, "model": model, "thought_en": thought_en, "thought_zh": thought_zh,
             "strategy": strategy if strategic else None, "frames": len(frames),
             "usage": {"in": u.input_tokens, "out": u.output_tokens,
                       "cache_read": getattr(u, "cache_read_input_tokens", 0)}})

        # Keep the assistant side short so history stays cheap.
        history += [user_msg, {"role": "assistant",
                               "content": json.dumps({"thought_en": thought_en,
                                                      "plan": decision.get("plan", [])})}]
        history = history[-2 * HISTORY_TURNS:]


if __name__ == "__main__":
    main()
