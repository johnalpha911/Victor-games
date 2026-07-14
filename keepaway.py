#!/usr/bin/env python3
"""
keepaway.py — Vector plays Keepaway with his cube (Cozmo-authentic).

Dangle the cube in front of Vector. He gets ready (lift up), sometimes fakes out
to bait you into flinching, then POUNCES — slamming his lift down. If the lift
strikes the cube, the cube registers a TAP: he caught it, his point. If you
pulled it away in time, no tap: your point.

  --- EVERYTHING BELOW IS DERIVED FROM REAL HARDWARE PROBE DATA ---

ANIMATIONS: these are CLIP names (anim_...), NOT trigger names. They are played
  with play_animation() and the Animation OBJECT constructed directly
  (protocol.Animation(name=...)). Passing a *string* makes the SDK call
  _ensure_loaded(), which tries to fetch the raw clip list — that request TIMES
  OUT on this Pi and the animation silently never plays. Passing the object
  skips that path entirely. This is the fix that made pounces actually fire.

GET-READY: raises the lift to ~93mm. The pounce SLAMS DOWN from there to ~30mm,
  so a pounce from a lowered lift doesn't work. We therefore ensure the ready
  stance BEFORE the fakeout-or-pounce decision, so it can never telegraph which
  one is coming.

ZONES (from the ToF): the round starts when the cube comes inside ~100mm. Vector
  only COMMITS A POUNCE once it's inside 40mm (where a pounce actually lands).
  He only FAKES YOU OUT when it's inside 30mm — right in his face, where a bait
  is worth it.

CATCH DETECTION: a real catch registers a cube TAP ~0.37-0.44s after the pounce
  fires. Successfully pulling the cube away registers NO tap (confirmed on
  hardware). Vector only commits a pounce once the cube is inside the strike
  zone (~30-40mm is where a pounce actually lands).

BEHAVIOR CONTROL: OVERRIDE_BEHAVIORS_PRIORITY, because Vector's autonomous
  behaviors were stealing control mid-pounce (control_lost_event).

CUBE: if it drops its BLE connection we reconnect ONLY between rounds, never
  mid-round. A round ends as soon as the cube's outcome LED animation finishes
  — you do NOT have to pull the cube away to start the next one.

RULES: after the mode is picked, Vector narrates the rules himself. Pet him at
  any point to skip the rest of the explanation.

MODES (pet-cycle at the very start, like sleepy vector):
  "First to 5" (default)  |  "Infinite" (pet anytime to end; a tie -> Vector wins)

CUBE LIGHTS (live all game):
  Vector sees the cube (ToF) -> GREEN.   Doesn't see it -> BLANK.
  During a round-outcome animation only:
    Vector won the round -> 3 RED   + 1 rotating blank
    Player won the round -> 3 GREEN + 1 rotating blank

GAME END: the win/lose GAME animation plays FIRST, then Vector announces
  "You got X points. I got X points. I won/lost."

Self-test (no robot):  python3 keepaway.py --selftest
"""

import argparse
import json
import random
from pathlib import Path
import sys
import threading
import time
import traceback

# anki_vector is imported lazily inside play_keepaway() so that --selftest can
# run in environments without the SDK.
lights = None
protocol = None


# ============================================================
# CONFIG  (values grounded in the hardware probe)
# ============================================================

BEHAVIOR_TIMEOUT_S = 30
CONNECT_TIMEOUT_S = 20.0
POLL_S = 0.02

# --- ToF (the reliable cube signal; camera is_visible flickers) ---
TOF_ROUND_START_MM = 100.0   # cube this close -> the round starts (~10cm)
TOF_STRIKE_MM = 40.0         # THE STRIKE ZONE. Vector only commits a pounce once
                             # the cube is this close. Measured on hardware: a
                             # pounce only lands when the cube is 30-40mm out
                             # (the ToF floors at ~30mm), so 40mm is the ceiling.
TOF_LOST_MM = 130.0          # beyond this -> "I can't see the cube" (lights blank)

# --- lift ---
READY_LIFT_MM = 70.0         # lift at/above this = already in the ready stance
                             # (get-ready raises it to ~93mm; the pounce slams
                             #  it down to ~30mm)

# --- pounce / tap ---
POUNCE_HESITATE_MIN_S = 0.2  # random hesitation once the cube enters the strike
POUNCE_HESITATE_MAX_S = 1.2  # zone, so the strike isn't perfectly predictable

# --- HE LEARNS -----------------------------------------------------------------
# Cozmo got better at keepaway the more you played it. Same here, but it cuts both
# ways: every game he LOSES sharpens him up, every game he WINS makes him
# complacent. Only the hesitation CEILING moves — the 0.2s floor never does — so as
# he improves, the window you have to yank the cube away gets tighter and tighter,
# and at his very best (ceiling == floor) he always strikes in 0.2s flat.
SKILL_FILE = Path.home() / ".vector_keepaway.json"   # Linux, Windows, macOS, Termux
SKILL_LOSS_FACTOR = 0.95     # he lost  -> he practises, gets faster
SKILL_WIN_FACTOR = 1.05      # he won   -> he gets cocky, slows down
SKILL_BEST = POUNCE_HESITATE_MIN_S   # can never be quicker than the floor
SKILL_WORST = POUNCE_HESITATE_MAX_S  # and never slower than he started
SKILL_PRACTISED_AT = 0.90    # ceiling below this -> he mentions the practice
SKILL_COCKY_AT = 1.10        # ceiling above this -> he gets cocky

# set from the skill file at startup; this is what the pounce hesitation reads
_hesitate_ceiling = POUNCE_HESITATE_MAX_S
TAP_WINDOW_S = 1.5           # watch for the catch-tap this long after the pounce
                             # fires (a real catch taps at +0.37 to +0.44s)

# --- fakeouts ---
MAX_FAKEOUTS = 2             # per round -> 0, 1 or 2, then a forced real pounce
FAKEOUT_CHANCE = 0.5
TOF_FAKEOUT_MM = 30.0        # A fakeout only fires when the cube is RIGHT in his
                             # face (25-30mm). He only bothers baiting you when
                             # you're teasing him up close — tighter than the
                             # strike zone, on purpose.
FAKEOUT_WAIT_S = 8.0         # If you never bring it that close, drop the bait and
                             # go for the real pounce rather than stalling.

# --- scoring ---
WIN_SCORE = 5                # first-to-5

# --- cube lights ---
CYCLE_STEP_S = 0.18          # moderate, same feel as the feeding spinner

# --- mode select ---
MODE_SETTLE_S = 3.0          # no pet for this long -> the mode locks in
MODE_PET_MAX_S = 5.0

# --- infinite mode ---
BETWEEN_ROUND_QUIT_S = 2.0   # window between rounds where a pet ends the game


# ============================================================
# ANIMATION CLIPS  (clip names — played via play_animation + Animation object)
# ============================================================

GETREADY = ["anim_keepaway_getready_01", "anim_keepaway_getready_02",
            "anim_keepaway_getready_03", "anim_keepaway_getreadyset_01"]
FAKEOUT = "anim_keepaway_fakeout_03"
POUNCE = ["anim_keepaway_pounce_mousetrap_04", "anim_keepaway_pounce_04"]
WINHAND = ["anim_keepaway_winhand_01", "anim_keepaway_winhand_02",
           "anim_keepaway_winhand_03"]
LOSEHAND = ["anim_keepaway_losehand_01", "anim_keepaway_losehand_02",
            "anim_keepaway_losehand_03"]
# The real thing. anim_keepaway_wingame_02 is LITERALLY the clip Cozmo used —
# Anki reused the Cozmo animation work in Vector, so the original has been sitting
# in the firmware all along. The lose path was already using the purpose-built
# keepaway clips; the win path was making do with a generic spin. Now it isn't.
WINGAME = ["anim_keepaway_wingame_01", "anim_keepaway_wingame_02",
           "anim_keepaway_wingame_03",
           "anim_launch_reacttoputdown"]   # the big spin, kept in the mix
LOSEGAME = ["anim_keepaway_losegame_01", "anim_keepaway_losegame_02",
            "anim_keepaway_losegame_03"]


# ============================================================
# PURE LOGIC (unit-tested offline via --selftest)
# ============================================================

def decide_fakeout(fakeouts_used, roll, max_fakeouts=MAX_FAKEOUTS,
                   chance=FAKEOUT_CHANCE):
    """True = fake out, False = commit a real pounce. Forced pounce once the
    fakeout cap is reached, so a round has 0, 1 or 2 fakeouts."""
    if fakeouts_used >= max_fakeouts:
        return False
    return roll < chance


def score_round(tapped):
    """A tap means the lift struck the cube -> Vector's point."""
    return "vector" if tapped else "player"


def game_over(vector_score, player_score, infinite, win_score=WIN_SCORE):
    if infinite:
        return False
    return vector_score >= win_score or player_score >= win_score


def vector_wins(vector_score, player_score):
    """A tie goes to Vector (matters in infinite mode)."""
    return vector_score >= player_score


def points(n):
    """English pluralisation: 1 -> 'point', everything else (including 0) ->
    'points'. Vector was saying 'You got 1 points'."""
    return "1 point" if n == 1 else f"{n} points"


def end_line(vector_score, player_score):
    result = "I won" if vector_wins(vector_score, player_score) else "I lost"
    return (f"You got {points(player_score)}. "
            f"I got {points(vector_score)}. {result}.")


def three_plus_blank(blank_pos, lit, off):
    """3 corners lit + 1 blank, the blank rotating."""
    return [off if i == blank_pos % 4 else lit for i in range(4)]


# ============================================================
# SDK HELPERS
# ============================================================

def _wait(fut, timeout=BEHAVIOR_TIMEOUT_S):
    if hasattr(fut, "result"):
        try:
            fut.result(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass


def _play_clip(robot, clip_name, wait=True):
    """Play an animation CLIP. Constructs the Animation OBJECT so the SDK skips
    _ensure_loaded() and never attempts the raw clip-list fetch (which times out
    on this Pi and silently kills the animation)."""
    try:
        fut = robot.anim.play_animation(protocol.Animation(name=clip_name))
        if wait:
            _wait(fut)
        return fut
    except Exception as exc:  # noqa: BLE001
        print(f"  [anim failed] {clip_name}: {exc}")
        return None


def _say(robot, text):
    try:
        _wait(robot.behavior.say_text(text), timeout=15)
    except Exception:  # noqa: BLE001
        pass


def _say_skippable(robot, text):
    """Say one line while watching the back sensor. Returns True if the player
    petted him during it (= skip the rest of the narration). The current line
    finishes speaking — audio can't be cut mid-word — but the rest is dropped."""
    try:
        fut = robot.behavior.say_text(text)
    except Exception:  # noqa: BLE001
        return _touching(robot)
    petted = False
    end = time.monotonic() + 15
    while time.monotonic() < end:
        if _touching(robot):
            petted = True
        if hasattr(fut, "done") and fut.done():
            break
        time.sleep(0.05)
    _wait(fut, timeout=5)
    return petted


def _lift_mm(robot):
    try:
        return robot.lift_height_mm
    except Exception:  # noqa: BLE001
        return None


def _tof(robot):
    """(distance_mm, found) from the front ToF sensor."""
    try:
        r = robot.proximity.last_sensor_reading
        if r is None:
            return (None, False)
        return (r.distance.distance_mm, bool(getattr(r, "found_object", False)))
    except Exception:  # noqa: BLE001
        return (None, False)


def _tof_within(robot, limit_mm):
    d, found = _tof(robot)
    return bool(found and d is not None and d <= limit_mm)


def _touching(robot):
    try:
        t = robot.touch.last_sensor_reading
        return t is not None and t.is_being_touched
    except Exception:  # noqa: BLE001
        return False


def _wait_pet_release(robot, timeout=MODE_PET_MAX_S):
    end = time.monotonic() + timeout
    while _touching(robot) and time.monotonic() < end:
        time.sleep(0.05)


def _tap_time(cube):
    try:
        return cube.last_tapped_time or 0.0
    except Exception:  # noqa: BLE001
        return 0.0


# ============================================================
# CUBE LIGHTS
# ============================================================

def _corners(cube, four):
    try:
        cube.set_light_corners(*four)
    except Exception:  # noqa: BLE001
        pass


def _all_green(cube):
    g = lights.green_light
    _corners(cube, [g, g, g, g])


def _all_blank(cube):
    try:
        cube.set_lights_off()
    except Exception:  # noqa: BLE001
        pass


def _cycle_outcome_lights(cube, lit, stop_event):
    """3 corners lit + 1 rotating blank, at a moderate speed, until stopped."""
    pos = 0
    while not stop_event.is_set():
        _corners(cube, three_plus_blank(pos, lit, lights.off_light))
        pos += 1
        t0 = time.monotonic()
        while time.monotonic() - t0 < CYCLE_STEP_S:
            if stop_event.is_set():
                return
            time.sleep(0.02)


class SeenLight:
    """Background thread: cube is GREEN while Vector can see it (ToF), BLANK
    when he can't. Paused while a round-outcome animation owns the lights."""

    def __init__(self, robot, cube):
        self.robot = robot
        self.cube = cube
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._last = None
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._t.start()

    def set_cube(self, cube):
        self.cube = cube
        self._last = None          # force a redraw on the new cube object

    def _run(self):
        while not self._stop.is_set():
            if not self._paused.is_set() and self.cube is not None:
                seen = _tof_within(self.robot, TOF_LOST_MM)
                if seen != self._last:
                    _all_green(self.cube) if seen else _all_blank(self.cube)
                    self._last = seen
            time.sleep(0.1)

    def pause(self):
        self._paused.set()

    def resume(self):
        # force a redraw — the pounce impact can reset the cube's lights
        self._last = None
        self._paused.clear()

    def shutdown(self):
        self._stop.set()
        self._t.join(timeout=1)


# ============================================================
# CUBE CONNECTION
# ============================================================

def _connect_cube(robot):
    print("Connecting to the cube...")
    try:
        robot.world.connect_cube()
    except Exception as exc:  # noqa: BLE001
        print(f"  (connect_cube: {exc})")
    deadline = time.monotonic() + CONNECT_TIMEOUT_S
    while time.monotonic() < deadline:
        cube = robot.world.connected_light_cube
        if cube is not None:
            time.sleep(0.3)        # let Vector's own connect-lights settle
            _all_blank(cube)       # then clear them
            print("Cube connected.")
            return cube
        time.sleep(0.5)
    return None


def _reconnect_between_rounds(robot, cube):
    """Recover a dropped cube. Called ONLY between rounds — never mid-round."""
    try:
        if cube is not None and cube.is_connected:
            return cube
    except Exception:  # noqa: BLE001
        pass
    print("  [cube] disconnected — reconnecting before the next round...")
    new = _connect_cube(robot)
    return new if new is not None else cube


# ============================================================
# WAITING (pet-interruptible in infinite mode)
# ============================================================

def _pet_quit_window(robot, seconds):
    """Watch the back sensor for `seconds`. True if petted (= quit the game).
    Gives a clear, deterministic chance to end an infinite game between rounds."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if _touching(robot):
            _wait_pet_release(robot)
            return True
        time.sleep(0.05)
    return False


def _wait_until(robot, cond, allow_pet_quit, label=None, timeout=None):
    """Block until cond() is true -> None. In infinite mode a pet breaks out and
    returns 'QUIT', so you can always end the game even while Vector is waiting.
    With a timeout, gives up and returns 'TIMEOUT'."""
    if label:
        print(f"  [round] {label}")
    start = time.monotonic()
    while True:
        if allow_pet_quit and _touching(robot):
            _wait_pet_release(robot)
            return "QUIT"
        if cond():
            return None
        if timeout is not None and time.monotonic() - start >= timeout:
            return "TIMEOUT"
        time.sleep(0.1)


# ============================================================
# ROUND
# ============================================================

def _ensure_ready(robot):
    """Guarantee the pounce stance. Get-ready raises the lift to ~93mm and the
    pounce slams down from there — a pounce with the lift already down doesn't
    work. Called BEFORE the fakeout-or-pounce decision, so the get-ready can
    never telegraph that a real pounce is coming.
    winhand/losehand leave Vector in the ready stance, so most rounds skip it."""
    lift = _lift_mm(robot)
    if lift is not None and lift >= READY_LIFT_MM:
        return
    _play_clip(robot, random.choice(GETREADY), wait=True)


def _pounce_and_detect(robot, cube):
    """Fire the pounce and watch for the catch-tap. Returns True if Vector caught
    it. A successful pull-away registers no tap at all (verified on hardware)."""
    baseline = _tap_time(cube)
    fut = _play_clip(robot, random.choice(POUNCE), wait=False)

    fire = time.monotonic()
    deadline = fire + TAP_WINDOW_S
    caught = False
    while time.monotonic() < deadline:
        if _tap_time(cube) > baseline:
            dt = time.monotonic() - fire
            d, _ = _tof(robot)
            print(f"  [round] TAP at +{dt:.2f}s (cube ~{d}mm) -> Vector caught it")
            caught = True
            break
        time.sleep(POLL_S)
    if not caught:
        print("  [round] no tap -> you pulled it away in time")

    _wait(fut, timeout=10)          # let the pounce animation finish
    return caught


def _play_outcome(robot, cube, vector_won):
    """Round-outcome animation + the cube's 3-lit/1-rotating-blank indicator.
    winhand/losehand leave Vector back in the ready stance for the next round."""
    lit = lights.red_light if vector_won else lights.green_light
    clip = random.choice(WINHAND if vector_won else LOSEHAND)
    stop = threading.Event()
    t = threading.Thread(target=_cycle_outcome_lights, args=(cube, lit, stop),
                         daemon=True)
    t.start()
    _play_clip(robot, clip, wait=True)
    stop.set()
    t.join(timeout=2)


def _play_round(robot, cube, seenlight, infinite):
    """One round. True = Vector's point, False = player's point, 'QUIT' = pet."""
    if _wait_until(robot, lambda: _tof_within(robot, TOF_ROUND_START_MM),
                   infinite, label="waiting for the cube...") == "QUIT":
        return "QUIT"

    fakeouts = 0
    while True:
        # Ready stance FIRST, before the decision -> can never telegraph which
        # is coming (a fakeout or the real thing).
        _ensure_ready(robot)

        # The get-ready (or the previous fakeout) takes time, and you may have
        # pulled the cube away while it played. Re-confirm the cube is actually
        # in front of him BEFORE deciding, so he never fakes out or pounces at
        # empty air.
        if _wait_until(robot, lambda: _tof_within(robot, TOF_ROUND_START_MM),
                       infinite) == "QUIT":
            return "QUIT"

        if decide_fakeout(fakeouts, random.random()):
            # A bait only works when the cube is right in his face (25-30mm).
            # If you keep it out at arm's length he has nothing to bait.
            r = _wait_until(robot, lambda: _tof_within(robot, TOF_FAKEOUT_MM),
                            infinite, timeout=FAKEOUT_WAIT_S,
                            label="waiting for you to tease it closer...")
            if r == "QUIT":
                return "QUIT"
            if r is None:
                fakeouts += 1
                print(f"  [round] fakeout #{fakeouts}")
                _play_clip(robot, FAKEOUT, wait=True)
                continue  # back to the top: re-ready, re-confirm the cube
            # TIMEOUT — you never brought it close enough to bait. Rather than
            # stall, drop the fakeout and go for the real pounce.
            print("  [round] never came close enough to bait — going for it")

        # REAL POUNCE — only commit once the cube is inside the strike zone
        if _wait_until(robot, lambda: _tof_within(robot, TOF_STRIKE_MM),
                       infinite, label="lining up the pounce...") == "QUIT":
            return "QUIT"
        # the ceiling is HIS, learned across games — the better he gets, the less
        # time you have to snatch the cube away
        time.sleep(random.uniform(POUNCE_HESITATE_MIN_S, _hesitate_ceiling))

        seenlight.pause()
        vector_won = _pounce_and_detect(robot, cube)
        _play_outcome(robot, cube, vector_won)
        seenlight.resume()
        return vector_won


# ============================================================
# GAME
# ============================================================

def _select_mode(robot):
    """Pet cycles the announced mode. Whatever it lands on is the mode. Only at
    the very start — after this, a pet means QUIT (infinite mode)."""
    modes = ["First to 5", "Infinite"]
    idx = 0
    _say(robot, "Pet me to change the mode.")
    _say(robot, modes[idx])
    last = 0.0
    started = time.monotonic()
    while True:
        if _touching(robot):
            idx = (idx + 1) % len(modes)
            _say(robot, modes[idx])
            _wait_pet_release(robot)
            last = time.monotonic()
        ref = last if last else started
        if time.monotonic() - ref >= MODE_SETTLE_S:
            break
        time.sleep(0.05)
    print(f"Mode: {modes[idx]}")
    return modes[idx]


def _clamp_ceiling(c):
    return max(SKILL_BEST, min(SKILL_WORST, c))


def _default_skill():
    return {"ceiling": SKILL_WORST, "wins": 0, "losses": 0}


def _load_skill():
    """His accumulated skill, carried between sessions. A missing or corrupt file
    just means a fresh Vector who's never played."""
    try:
        with open(SKILL_FILE, "r", encoding="utf-8") as fh:
            s = json.load(fh)
        return {
            "ceiling": _clamp_ceiling(float(s.get("ceiling", SKILL_WORST))),
            "wins": int(s.get("wins", 0)),
            "losses": int(s.get("losses", 0)),
        }
    except Exception:  # noqa: BLE001
        return _default_skill()


def _save_skill(skill):
    try:
        with open(SKILL_FILE, "w", encoding="utf-8") as fh:
            json.dump(skill, fh, indent=2)
    except Exception as exc:  # noqa: BLE001
        print(f"  (couldn't save his skill: {exc})")


def _update_skill(skill, vector_won):
    """PER GAME, not per round. He loses -> he practises. He wins -> he gets cocky."""
    before = skill["ceiling"]
    if vector_won:
        skill["ceiling"] = _clamp_ceiling(before * SKILL_WIN_FACTOR)
        skill["wins"] += 1
    else:
        skill["ceiling"] = _clamp_ceiling(before * SKILL_LOSS_FACTOR)
        skill["losses"] += 1
    verdict = "cocky" if vector_won else "sharper"
    print(f"  [skill] {verdict}: hesitation ceiling {before:.3f}s -> "
          f"{skill['ceiling']:.3f}s   ({skill['wins']}W / {skill['losses']}L)")
    return skill


def _skill_line(skill):
    """What he says about himself before a game, if he has anything to say."""
    if skill["wins"] + skill["losses"] == 0:
        return None                        # never played — no history to boast about
    c = skill["ceiling"]
    if c <= SKILL_PRACTISED_AT:
        return "I have been practising. I am much faster now."
    if c >= SKILL_COCKY_AT:
        return "I have been winning a lot. This should be easy."
    return None


def _narrate_instructions(robot, infinite, skill=None):
    """Vector explains the rules himself, in first person. PET HIM AT ANY POINT
    to skip the rest — the pet is then consumed, so it can't leak through and
    instantly quit an infinite game."""
    lines = []
    if skill:
        boast = _skill_line(skill)
        if boast:
            lines.append(boast)
    lines += [
        "Let me explain the game.",
        "Bait me with the cube.",
        "Bring it in slowly. That is how you tempt me.",
        "Then pull it away when I pounce!",
        "If I catch it, I get a point.",
        "If you pull it away in time, you get a point.",
        "Sometimes I will fake you out, so watch me closely.",
    ]
    if infinite:
        lines.append("We play until you stop me.")
        lines.append("Pet my back between rounds to end the game.")
    else:
        lines.append("First to five points wins.")

    for line in lines:
        if _say_skippable(robot, line):
            print("  [game] narration skipped.")
            _wait_pet_release(robot)     # consume it — a pet means QUIT in-game
            _say(robot, "Okay, let's play!")
            return

    _say(robot, "Let's play keepaway!")


def _end_game(robot, cube, vector_score, player_score):
    """The win/lose GAME animation plays FIRST, then the score announcement."""
    _all_blank(cube)
    if vector_wins(vector_score, player_score):
        _play_clip(robot, random.choice(WINGAME), wait=True)   # he won the game
    else:
        _play_clip(robot, random.choice(LOSEGAME), wait=True)   # angry/sad
    _say(robot, end_line(vector_score, player_score))


def play_keepaway(serial=None):
    global lights, protocol, _hesitate_ceiling

    skill = _load_skill()
    _hesitate_ceiling = skill["ceiling"]
    played = skill["wins"] + skill["losses"]
    if played:
        print(f"[skill] {played} games played "
              f"({skill['wins']}W / {skill['losses']}L). "
              f"He hesitates {POUNCE_HESITATE_MIN_S:.2f}-{_hesitate_ceiling:.2f}s.")
    else:
        print("[skill] he's never played before.")

    import anki_vector
    from anki_vector import lights as _lights
    from anki_vector.connection import ControlPriorityLevel
    from anki_vector.messaging import protocol as _protocol
    from anki_vector.util import degrees
    lights = _lights
    protocol = _protocol

    print("Connecting to Vector...")
    with anki_vector.AsyncRobot(
            serial=serial,
            cache_animation_lists=False,     # never fetch the raw clip list
            behavior_activation_timeout=BEHAVIOR_TIMEOUT_S,
            # Vector's autonomous behaviors were stealing control mid-pounce:
            behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
    ) as robot:
        print("Connected.")

        # animation CLIPS require Vector to be off the charger
        try:
            if robot.status.is_on_charger:
                print("On charger — driving off first.")
                _wait(robot.behavior.drive_off_charger())
        except Exception:  # noqa: BLE001
            pass

        cube = _connect_cube(robot)
        if cube is None:
            _say(robot, "I can't find my cube. Check its battery and try again.")
            return 1

        # head down slightly so the cube sits in the ToF's field of view
        try:
            _wait(robot.behavior.set_head_angle(degrees(-5.0)))
        except Exception:  # noqa: BLE001
            pass

        infinite = (_select_mode(robot) == "Infinite")
        _narrate_instructions(robot, infinite, skill)

        seenlight = SeenLight(robot, cube)
        seenlight.start()

        vector_score = 0
        player_score = 0
        try:
            while True:
                if infinite and _touching(robot):
                    _wait_pet_release(robot)
                    break

                result = _play_round(robot, cube, seenlight, infinite)
                if result == "QUIT":
                    print("  [game] pet — ending the game.")
                    break

                if result:
                    vector_score += 1
                else:
                    player_score += 1
                print(f"SCORE — Vector {vector_score} | You {player_score}")

                if game_over(vector_score, player_score, infinite):
                    break

                # The round is over as soon as the cube's outcome LED animation
                # finishes. No "pull the cube away" gate — just keep playing.
                # Between rounds ONLY (never mid-round): recover a dropped cube.
                cube = _reconnect_between_rounds(robot, cube)
                seenlight.set_cube(cube)

                # Infinite mode: a clear window between rounds to pet-to-quit.
                # (Petting also quits while he's waiting for the cube, but this
                #  guarantees a reliable chance right after each round.)
                if infinite and _pet_quit_window(robot, BETWEEN_ROUND_QUIT_S):
                    print("  [game] pet — ending the game.")
                    break
        finally:
            seenlight.shutdown()

        _end_game(robot, cube, vector_score, player_score)
        _all_blank(cube)
        print(f"\nFinal — Vector {vector_score} | You {player_score}")

        # HE LEARNS. Per game, not per round: a loss sharpens him, a win makes him
        # cocky. Persisted, so he carries it into the next session.
        _save_skill(_update_skill(skill, vector_wins(vector_score, player_score)))
        return 0


# ============================================================
# SELF-TEST (offline)
# ============================================================

def _selftest():
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    print("keepaway.py self-test (no robot required)\n")

    # fakeouts: always 0, 1 or 2 — never 3+
    counts = set()
    for _ in range(5000):
        n = 0
        while decide_fakeout(n, random.random()):
            n += 1
        counts.add(n)
    check("fakeouts only ever 0/1/2", counts == {0, 1, 2})

    # --- skill ---
    s = _default_skill()
    check("a beginner starts at the worst ceiling", s["ceiling"] == SKILL_WORST)

    # losing streak -> he sharpens, and stops at the floor (never below)
    s = _default_skill()
    for _ in range(200):
        s["ceiling"] = _clamp_ceiling(s["ceiling"] * SKILL_LOSS_FACTOR)
    check("a long losing streak bottoms out AT the floor, not below",
          abs(s["ceiling"] - SKILL_BEST) < 1e-9)

    # winning streak -> cocky, capped at where he started
    s = {"ceiling": SKILL_BEST, "wins": 0, "losses": 0}
    for _ in range(200):
        s["ceiling"] = _clamp_ceiling(s["ceiling"] * SKILL_WIN_FACTOR)
    check("a long winning streak caps at the beginner ceiling",
          abs(s["ceiling"] - SKILL_WORST) < 1e-9)

    # a loss makes him faster, a win makes him slower
    a = _update_skill({"ceiling": 0.8, "wins": 0, "losses": 0}, vector_won=False)
    check("losing sharpens him", a["ceiling"] < 0.8 and a["losses"] == 1)
    b = _update_skill({"ceiling": 0.8, "wins": 0, "losses": 0}, vector_won=True)
    check("winning makes him cocky", b["ceiling"] > 0.8 and b["wins"] == 1)

    # the hesitation range is always valid
    for c in (SKILL_BEST, 0.5, SKILL_WORST):
        check(f"hesitation range valid at ceiling {c}",
              POUNCE_HESITATE_MIN_S <= c)

    # he only talks about his form when he has form
    check("a never-played Vector doesn't boast",
          _skill_line(_default_skill()) is None)
    check("a practised Vector says so",
          _skill_line({"ceiling": 0.5, "wins": 1, "losses": 9}) is not None)
    check("a cocky Vector says so",
          _skill_line({"ceiling": 1.2, "wins": 9, "losses": 1}) is not None)
    check("forced pounce at the cap", decide_fakeout(MAX_FAKEOUTS, 0.0) is False)
    check("roll above chance -> pounce", decide_fakeout(0, 0.99) is False)
    check("roll below chance -> fakeout", decide_fakeout(0, 0.0) is True)

    check("tap -> Vector's point", score_round(True) == "vector")
    check("no tap -> player's point", score_round(False) == "player")

    check("first-to-5 ends at 5", game_over(5, 2, False) is True)
    check("first-to-5 continues at 4", game_over(4, 4, False) is False)
    check("infinite never auto-ends", game_over(9, 9, True) is False)

    check("Vector ahead -> Vector wins", vector_wins(3, 1) is True)
    check("player ahead -> Vector loses", vector_wins(1, 3) is False)
    check("TIE -> Vector wins", vector_wins(4, 4) is True)

    check("end line reports both scores", "You got 2" in end_line(5, 2)
          and "I got 5" in end_line(5, 2) and "I won" in end_line(5, 2))
    check("end line on a loss", "I lost" in end_line(2, 5))

    # pluralisation: 1 is singular, 0 and 2+ are plural
    check("points(0) -> '0 points'", points(0) == "0 points")
    check("points(1) -> '1 point'", points(1) == "1 point")
    check("points(2) -> '2 points'", points(2) == "2 points")
    check("points(5) -> '5 points'", points(5) == "5 points")
    check("end line singular player", "You got 1 point." in end_line(5, 1))
    check("end line singular vector", "I got 1 point." in end_line(1, 5))
    check("end line zero player", "You got 0 points." in end_line(5, 0))
    check("no '1 points' anywhere", "1 points" not in end_line(1, 1))

    frame = three_plus_blank(1, "L", "O")
    check("3 lit + 1 blank", frame == ["L", "O", "L", "L"])
    check("blank rotates", three_plus_blank(2, "L", "O") == ["L", "L", "O", "L"])
    check("blank wraps", three_plus_blank(4, "L", "O") == three_plus_blank(0, "L", "O"))
    check("always exactly one blank",
          all(three_plus_blank(i, "L", "O").count("O") == 1 for i in range(12)))

    check("strike zone tighter than round-start", TOF_STRIKE_MM < TOF_ROUND_START_MM)
    check("fakeout zone tighter than strike zone", TOF_FAKEOUT_MM < TOF_STRIKE_MM)
    check("clips are clip names", all(c.startswith("anim_") for c in
                                      GETREADY + POUNCE + WINHAND + LOSEHAND
                                      + LOSEGAME + WINGAME + [FAKEOUT]))

    print()
    if fails:
        print(f"SELF-TEST FAILED: {fails}")
        return 1
    print("ALL SELF-TESTS PASSED.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Vector Keepaway")
    ap.add_argument("--serial", default=None)
    ap.add_argument("--selftest", action="store_true",
                    help="offline logic self-test, no robot needed")
    ap.add_argument("--reset-skill", action="store_true",
                    help="wipe what he's learned and make him a beginner again")
    ap.add_argument("--skill", action="store_true",
                    help="show how good he's got, and exit")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if args.reset_skill:
        _save_skill(_default_skill())
        print(f"Skill reset — he's a beginner again. ({SKILL_FILE})")
        return 0

    if args.skill:
        s = _load_skill()
        played = s["wins"] + s["losses"]
        print(f"Games:     {played}  ({s['wins']}W / {s['losses']}L)")
        print(f"Hesitates: {POUNCE_HESITATE_MIN_S:.2f}-{s['ceiling']:.2f}s")
        print(f"           (best possible {SKILL_BEST:.2f}s, "
              f"beginner {SKILL_WORST:.2f}s)")
        print(f"File:      {SKILL_FILE}")
        return 0
    try:
        return play_keepaway(serial=args.serial)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 0
    except Exception:  # noqa: BLE001
        print("[FATAL]")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
