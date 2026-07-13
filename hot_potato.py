#!/usr/bin/env python3
"""
hot_potato.py — Vector runs a game of hot potato.

He is a TIMER WITH A PERSONALITY, not a referee. He does not know who is holding
the cube, who went out, or whether anyone cheated. He counts, he announces, and he
enjoys himself. The players police each other — want to cheat? Fine. The others
will point you out.

HOW IT GOES
  He explains the rules (pet him to skip).
  Then you COUNT THE PLAYERS IN:
      pet him          -> +1 player (he starts at 2 and says each number)
      move his arm     -> "Are you sure?"
      move it again    -> confirmed, and the game starts
      do nothing       -> he takes that as a no and waits for another go
  Rounds = players - 1, so exactly one person is left standing at the end.

EACH ROUND
  The cube cycles through colours — never red. Red means one thing here.
  A hidden 25-60s timer runs. Nobody knows how long.
  He gets bored while you pass it around, and idles.
  For the last TEN SECONDS he goes completely still and counts aloud. No
  animations. The stillness is the point.
  Then the cube flashes RED, and whoever is holding it is out.
  He is delighted about this.
  Ten second breather, then the next round.

  Last player standing: he says YOU WIN and does something big.

THE LIFT GESTURE
  The arm is GEARED. It does NOT swing freely by hand — you can only nudge it a few
  millimetres, and it won't stay where you put it. So the gesture can't be "raise it
  and lower it"; it's simply MOVE IT AT ALL, twice. Any delta past a few mm, in
  either direction, counts.

  And the motor has to be genuinely let go of first: set_lift_height() is a POSITION
  CONTROLLER that actively holds the arm, and animations leave it held too.
  set_lift_motor(0) does NOT clear that — stop_all_motors() does. lift_height_mm
  still reads fine while the arm is loose.

  python3 hot_potato.py --serial yourserial
  python3 hot_potato.py --serial yourserial --players 5   # skip the count-in
"""

import argparse
import math
import random
import sys
import threading
import time
import traceback

import anki_vector
from anki_vector.color import Color
from anki_vector.connection import ControlPriorityLevel
from anki_vector.lights import Light
from anki_vector.messaging import protocol


# ============================================================================
# THE GAME
# ============================================================================
MIN_PLAYERS = 2
MAX_PLAYERS = 12             # a sanity cap, not a real limit
ROUND_MIN_S, ROUND_MAX_S = 25.0, 60.0
COUNTDOWN_S = 10.0           # he counts the last ten aloud
BREATHER_S = 10.0            # a breather between rounds

# --- the lift gesture ---
# Vector's lift is GEARED. It does NOT swing freely by hand — you can only nudge it
# a few mm, and it won't stay wherever you put it. So the gesture can't be "raise
# the arm and lower it"; it has to be "MOVE IT AT ALL", twice:
#     nudge  -> "Are you sure?"
#     nudge  -> yes, start the game
#     silence -> he decides you weren't sure, and goes back to waiting
LIFT_NUDGE_MM = 5.0          # any movement bigger than this counts, either direction
CONFIRM_TIMEOUT_S = 6.0      # no second nudge within this = he takes it as a no

# --- the cube ---
# NEVER RED. Red means one thing in this game and it has to stay special.
POTATO_COLOURS = [
    (0, 0, 255),      # blue
    (0, 255, 0),      # green
    (0, 255, 255),    # cyan
    (255, 255, 0),    # yellow
    (255, 0, 255),    # magenta
    (255, 255, 255),  # white
]
CYCLE_S = 0.45               # normal pace
CYCLE_FAST_S = 0.20          # during the countdown — it speeds up. BLE writes are
                             # rate-limited to ~5-7/sec, so don't go below ~0.18.
FLASH_S = 0.20
FLASH_COUNT = 6

# --- idling while they pass it around ---
IDLE_MIN_S, IDLE_MAX_S = 7.0, 13.0

# ============================================================================
# CLIPS  (lowercase anim_* names -> protocol.Animation objects)
# ============================================================================
IDLE_CLIPS = [
    "anim_keepaway_bored_idle_01",
    "anim_keepaway_bored_idle_02",
]
# Someone just went OUT and he is very pleased about it. Round-level wins: smug,
# not a full celebration — he hasn't won anything, he just enjoyed the moment.
#
# NO KEEPAWAY WINHAND CLIPS. They RAISE THE LIFT — which makes sense, since a won
# hand in keepaway IS a lift catch — but it's completely the wrong body language
# for "someone is out", and it leaves the arm stuck up. The blackjack wins are pure
# smugness with no lift involved.
ELIMINATION_CLIPS = [
    "anim_blackjack_victorwin_01",       # he won a hand
    "anim_blackjack_victorbjackwin_01",  # he got BLACKJACK — bigger
]
# the LAST player standing. Big. One or the other, never both.
WINNER_CLIPS = [
    "anim_launch_reacttoputdown",
    "anim_keepaway_wingame_02",
]

# ============================================================================
# TRIGGERS  (CamelCase — a DIFFERENT namespace from clips)
# ============================================================================
CUBE_GETIN = "ConnectToCubeGetIn"
CUBE_LOOP = "ConnectToCubeLoop"        # LOOPS while it's trying
CUBE_SUCCESS = "ConnectToCubeSuccess"
CUBE_FAILURE = "ConnectToCubeFailure"
CUBE_CONNECT_TIMEOUT_S = 20.0


# ============================================================================
# HELPERS
# ============================================================================

def _wait(fut, timeout=25):
    if hasattr(fut, "result"):
        try:
            fut.result(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass


def _clip(robot, name, wait=True):
    """A CLIP. The Animation OBJECT is built directly — passing a bare string makes
    the SDK fetch the whole 589-clip list, which TIMES OUT on the Pi, and then the
    animation silently never plays."""
    try:
        fut = robot.anim.play_animation(protocol.Animation(name=name))
        if wait:
            _wait(fut)
        return fut
    except Exception as exc:  # noqa: BLE001
        print(f"  [clip failed] {name}: {exc}")
        return None


def _load_triggers(robot):
    """POPULATE THE TRIGGER DICT. Without this _anim_trigger_dict is EMPTY and every
    single trigger silently does nothing — which is exactly what happened. Both
    reaction_game and fortune_teller call this first; so do we now.

    NOTE: the TRIGGER list loads fine. It's the raw 589-CLIP list that times out on
    the Pi — a completely different list, and we never fetch it."""
    try:
        _wait(robot.anim.load_animation_trigger_list())
        n = len(robot.anim.anim_trigger_list)
        print(f"  [anim] {n} triggers loaded")
        return n > 0
    except Exception as exc:  # noqa: BLE001
        print(f"  [anim] couldn't load the trigger list: {exc}")
        return False


def _trigger(robot, name, wait=True):
    """A TRIGGER (CamelCase) — a DIFFERENT namespace from clips.

    Look the OBJECT up in the trigger dict and pass that, so the SDK skips
    _ensure_loaded(). Falling back to the bare name is safe here (unlike with
    clips): a trigger string only needs the TRIGGER list, which loads fine."""
    try:
        trig = None
        try:
            trig = robot.anim._anim_trigger_dict.get(name)   # noqa: SLF001
        except Exception:  # noqa: BLE001
            trig = None
        fut = robot.anim.play_animation_trigger(trig if trig is not None else name)
        if wait:
            _wait(fut)
        return fut
    except Exception as exc:  # noqa: BLE001
        print(f"  [trigger failed] {name}: {exc}")
        return None


def _say(robot, text):
    try:
        _wait(robot.behavior.say_text(text), timeout=20)
    except Exception:  # noqa: BLE001
        pass


def _say_async(robot, text):
    """Fire and forget. The countdown is driven by the CLOCK, not by how long the
    speech takes — otherwise 'ten... nine... eight' drifts and the last ten seconds
    stop being ten seconds."""
    try:
        return robot.behavior.say_text(text)
    except Exception:  # noqa: BLE001
        return None


def _touching(robot):
    try:
        t = robot.touch.last_sensor_reading
        return t is not None and t.is_being_touched
    except Exception:  # noqa: BLE001
        return False


def _pet_release(robot, timeout=5.0):
    end = time.monotonic() + timeout
    while _touching(robot) and time.monotonic() < end:
        time.sleep(0.05)
    time.sleep(0.2)


def _lift_mm(robot):
    try:
        return robot.lift_height_mm
    except Exception:  # noqa: BLE001
        return None


def _release_lift(robot):
    """LET GO of the arm, so a human can actually move it.

    Two things hold it, and BOTH have to be dealt with:
      * set_lift_height() is a POSITION CONTROLLER — it actively holds the arm at
        its target and you'd be fighting the motor.
      * ANIMATIONS also leave the lift held wherever they finished.
    set_lift_motor(0) alone does NOT clear that. stop_all_motors() is the SDK's
    explicit 'let go of everything' — it's what fortune_teller uses for exactly
    this. lift_height_mm still reads fine while the arm is loose."""
    try:
        robot.motors.stop_all_motors()
    except Exception as exc:  # noqa: BLE001
        print(f"  (stop_all_motors: {exc})")
    try:
        robot.motors.set_lift_motor(0)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.4)


def _lift_down(robot):
    try:
        _wait(robot.behavior.set_lift_height(0.0, accel=10.0, max_speed=10.0),
              timeout=8)
    except Exception:  # noqa: BLE001
        pass


def _lift_nudged(robot, base):
    """Did a human MOVE the lift at all?

    The arm is geared and can only be shifted a few mm by hand — it won't travel far
    and it won't stay put. So we don't look for a raise, or a direction: any delta
    against the baseline, either way, is the gesture."""
    cur = _lift_mm(robot)
    if cur is None or base is None:
        return False
    return abs(cur - base) > LIFT_NUDGE_MM


# ============================================================================
# CUBE LIGHTS
# ============================================================================

def _light(rgb):
    return Light(on_color=Color(rgb=rgb))


def _all(cube, rgb):
    try:
        cube.set_light_corners(*([_light(rgb)] * 4))
    except Exception:  # noqa: BLE001
        pass


def _blank(cube):
    try:
        cube.set_lights_off()
    except Exception:  # noqa: BLE001
        pass


def _cycle_cube(cube, stop, fast):
    """The potato is hot. All four corners change colour together — visible from
    any angle, which matters when it's being flung around a table. Never red."""
    i = 0
    while not stop.is_set():
        _all(cube, POTATO_COLOURS[i % len(POTATO_COLOURS)])
        i += 1
        step = CYCLE_FAST_S if fast.is_set() else CYCLE_S
        t0 = time.monotonic()
        while time.monotonic() - t0 < step:
            if stop.is_set():
                return
            time.sleep(0.02)


def _flash_red(cube):
    """BANG. Whoever's holding it is out."""
    for _ in range(FLASH_COUNT):
        _all(cube, (255, 0, 0))
        time.sleep(FLASH_S)
        _blank(cube)
        time.sleep(FLASH_S)
    _all(cube, (255, 0, 0))
    time.sleep(0.8)
    _blank(cube)


# ============================================================================
# CUBE CONNECTION  —  GetIn -> Loop -> Success/Failure
# ============================================================================

def _connect_cube(robot):
    """He shouldn't just sit there frozen for ten seconds looking like he's crashed.
    GetIn as it starts, Loop on repeat while it tries, then Success or Failure."""
    print("Connecting the cube...")
    _trigger(robot, CUBE_GETIN, wait=True)

    try:
        fut = robot.world.connect_cube()
        if hasattr(fut, "result"):
            fut.result(timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"  [cube] connect error: {exc}")

    deadline = time.monotonic() + CUBE_CONNECT_TIMEOUT_S
    while time.monotonic() < deadline:
        cube = robot.world.connected_light_cube
        if cube is not None:
            print("  [cube] connected")
            _trigger(robot, CUBE_SUCCESS, wait=True)
            return cube
        _trigger(robot, CUBE_LOOP, wait=True)     # keep looping while it tries
        time.sleep(0.5)

    print("  [cube] connection failed")
    _trigger(robot, CUBE_FAILURE, wait=True)
    return None


# ============================================================================
# SETUP — counting the players in
# ============================================================================

def _confirm_start(robot, players):
    """He felt the arm move. 'Are you sure?' — MOVE IT AGAIN to confirm. Do nothing
    and he decides you weren't sure, and goes back to waiting for pets."""
    _say(robot, f"{players} players. Are you sure?")
    _release_lift(robot)
    base = _lift_mm(robot)                 # re-baseline AFTER the first nudge
    deadline = time.monotonic() + CONFIRM_TIMEOUT_S

    while time.monotonic() < deadline:
        if _lift_nudged(robot, base):
            return True
        time.sleep(0.05)

    _say(robot, "Okay. Not yet.")
    return False


def _count_players(robot):
    """Pet to add a player. Raise the arm to lock it in."""
    _release_lift(robot)                      # let go, so they can move it
    base = _lift_mm(robot)

    players = MIN_PLAYERS
    _say(robot, f"{players} players. Pet my back to add more.")
    _say(robot, "Then move my arm when you are ready.")
    print(f"  [setup] {players} players (pet to add, lift the arm to start)")
    print(f"  [setup] arm released — baseline {base}mm; "
          f"move it more than {LIFT_NUDGE_MM:.0f}mm to start")

    next_report = time.monotonic() + 2.0

    while True:
        # LIVE READOUT. If this number never budges when you physically move the
        # arm, the motor is still being held and it isn't a threshold problem.
        cur = _lift_mm(robot)
        if cur is not None and time.monotonic() >= next_report:
            print(f"  [setup] lift {cur:5.1f}mm   (baseline {base:5.1f}mm, "
                  f"delta {abs(cur - base):4.1f}mm)")
            next_report = time.monotonic() + 2.0

        # PET -> another player
        if _touching(robot):
            if players < MAX_PLAYERS:
                players += 1
                print(f"  [setup] {players} players")
                _say(robot, str(players))
            _pet_release(robot)
            _release_lift(robot)
            base = _lift_mm(robot)            # re-baseline; petting jostles him
            continue

        # NUDGE THE ARM -> "are you sure?"
        if _lift_nudged(robot, base):
            print("  [setup] arm moved — asking if they're sure")
            if _confirm_start(robot, players):
                return players
            _release_lift(robot)
            base = _lift_mm(robot)            # they backed out — wait again
            continue

        time.sleep(0.05)


# ============================================================================
# A ROUND
# ============================================================================

def _round(robot, cube, lo, hi):
    """A hidden timer. He idles while they pass it about, then goes completely
    still for the last ten seconds and counts. Then the cube goes red."""
    total = random.uniform(lo, hi)
    end = time.monotonic() + total
    print(f"  [round] hidden timer: {total:.1f}s")

    stop = threading.Event()
    fast = threading.Event()
    cycler = threading.Thread(target=_cycle_cube, args=(cube, stop, fast),
                              daemon=True)
    cycler.start()

    last_said = None
    next_idle = time.monotonic() + random.uniform(IDLE_MIN_S, IDLE_MAX_S)

    try:
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break

            if left <= COUNTDOWN_S:
                # THE COUNTDOWN. He stops moving entirely — no idles, no
                # animations, nothing. The stillness IS the tension.
                if not fast.is_set():
                    fast.set()                # and the cube starts racing
                    print("  [round] countdown")
                n = int(math.ceil(left))
                if n != last_said and n >= 1:
                    _say_async(robot, str(n))  # clock-driven, so it can't drift
                    last_said = n
            else:
                # still being passed around, and he's getting bored.
                # (never start an idle that could run into the countdown)
                if (time.monotonic() >= next_idle
                        and left > COUNTDOWN_S + 4.0):
                    _clip(robot, random.choice(IDLE_CLIPS), wait=False)
                    next_idle = time.monotonic() + random.uniform(IDLE_MIN_S,
                                                                 IDLE_MAX_S)
            time.sleep(0.05)
    finally:
        stop.set()
        cycler.join(timeout=2)

    _flash_red(cube)             # BANG


# ============================================================================
# NARRATION
# ============================================================================

def _say_skippable(robot, text):
    try:
        fut = robot.behavior.say_text(text)
    except Exception:  # noqa: BLE001
        return _touching(robot)
    petted = False
    end = time.monotonic() + 20
    while time.monotonic() < end:
        if _touching(robot):
            petted = True
        if hasattr(fut, "done") and fut.done():
            break
        time.sleep(0.05)
    _wait(fut, timeout=5)
    return petted


def _narrate(robot):
    lines = [
        "Let's play hot potato!",
        "Pass the cube around while it changes colour.",
        "I have a secret timer, and you cannot see it.",
        "When it runs out, the cube turns red.",
        "Whoever is holding it then is out!",
        "I will count down the last ten seconds.",
    ]
    for line in lines:
        if _say_skippable(robot, line):
            print("  [narration skipped]")
            _pet_release(robot)     # CONSUME it — a pet means +1 player next
            return


# ============================================================================
# THE GAME
# ============================================================================

def hot_potato(serial, players_arg, lo, hi):
    with anki_vector.AsyncRobot(
            serial=serial,
            cache_animation_lists=False,
            behavior_activation_timeout=30,
            behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
    ) as robot:
        print("Connected.")
        cube = None
        try:
            try:
                if robot.status.is_on_charger:
                    _wait(robot.behavior.drive_off_charger())
            except Exception:  # noqa: BLE001
                pass

            _load_triggers(robot)        # or every trigger below does NOTHING

            cube = _connect_cube(robot)
            if cube is None:
                print("No cube.")
                return 1
            _blank(cube)

            _narrate(robot)

            players = players_arg or _count_players(robot)
            rounds = players - 1
            print(f"\n{players} players -> {rounds} rounds\n")
            _say(robot, f"{players} players. Here we go!")
            _lift_down(robot)

            for r in range(1, rounds + 1):
                still_in = players - r + 1
                print(f"======== ROUND {r}/{rounds}  ({still_in} still in) ========")
                _say(robot, "Start passing!")

                _round(robot, cube, lo, hi)

                # someone is OUT. He does not know who, and does not care.
                left = still_in - 1
                _say(robot, "You're out!")
                _clip(robot, random.choice(ELIMINATION_CLIPS), wait=True)

                if left > 1:
                    _say(robot, f"{left} players left. Take a breather.")
                    print(f"  [break] {BREATHER_S:.0f}s")
                    time.sleep(BREATHER_S)

            _say(robot, "You win!")
            _clip(robot, random.choice(WINNER_CLIPS), wait=True)
            print("\nWe have a winner.")
            return 0

        finally:
            if cube is not None:
                _blank(cube)
            try:
                robot.motors.set_lift_motor(0)
                robot.motors.set_head_motor(0)
            except Exception:  # noqa: BLE001
                pass


def main():
    ap = argparse.ArgumentParser(description="Vector hot potato")
    ap.add_argument("--serial", default=None)
    ap.add_argument("--players", type=int, default=None,
                    help="skip the count-in and just use this many")
    ap.add_argument("--min-s", type=float, default=ROUND_MIN_S,
                    help=f"shortest possible round (default {ROUND_MIN_S:.0f})")
    ap.add_argument("--max-s", type=float, default=ROUND_MAX_S,
                    help=f"longest possible round (default {ROUND_MAX_S:.0f})")
    args = ap.parse_args()

    if args.players is not None and args.players < 2:
        print("Need at least 2 players.")
        return 1
    if args.min_s >= args.max_s:
        print("--min-s must be less than --max-s")
        return 1
    if args.min_s <= COUNTDOWN_S:
        print(f"--min-s must be longer than the {COUNTDOWN_S:.0f}s countdown")
        return 1

    try:
        return hot_potato(args.serial, args.players, args.min_s, args.max_s)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 0
    except Exception:  # noqa: BLE001
        print("[FATAL]")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
