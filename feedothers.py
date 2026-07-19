#!/usr/bin/env python3
"""
feedothers.py — feed Vector's friends.

Vector doesn't eat this time and he doesn't move. He sits and watches while you
feed everyone else: grab a soft toy, tap the cube with its mouth to charge it,
then tap-tap-tap to drain it back out again. Vector reacts to all of it.

THE LOOP
  spinner (empty)  ->  charge it (shake or tap)  ->  all four lights up, solid  ->
  7 taps to discharge it  ->  Vector reacts  ->  back to the spinner, next toy.

  Pet Vector while the cube is sitting empty and waiting, and the session ends.

HOW THIS DIFFERS FROM feeding.py
  * VECTOR NEVER DRIVES. No ToF, no camera confirm, no dead reckoning, no nesting.
    He stays where you put him. Everything physical happens in your hands.
  * CHARGING IS UNLIMITED. feeding.py had two stages that got harder (all-at-once,
    then corner-by-corner) and then ended. Here you can charge and discharge as
    many times as you like — one round per toy, for as many toys as you have.
  * THE LIGHTS ARE ALWAYS UNIFORM. All four corners brighten together, every time.
    There is no corner-by-corner fill stage — that was feeding.py's stage 2 and
    it's deliberately not here. Full charge = all four solid at FULL.
  * DISCHARGE IS A FIXED 7 TAPS, not a mirror of how you charged it. Each tap
    knocks the lights down one step of seven. It's a countdown you can see.

HARD-WON DETAILS INHERITED FROM feeding.py (measured on hardware — don't "fix")
  * ANIMATIONS ARE CLIPS. Play them with protocol.Animation(name=...), never a
    bare string: a string makes the SDK fetch the raw 589-clip list, which TIMES
    OUT on the Pi, and the animation then silently never plays.
  * set_light_corners NEEDS Light objects, not raw Colors. Raw Colors fail
    SILENTLY.
  * BLE cube-light writes are rate limited. ~5-7 writes/sec is proven safe
    (0.18s/step). Going faster backs the queue up and the cube keeps drawing
    stale frames after you've told it to stop.

  python3 feedothers.py --serial yourserial
  python3 feedothers.py --serial yourserial --green    # green lights, like his eyes
  python3 feedothers.py --serial yourserial --tap      # tap to charge, not shake
  python3 feedothers.py --serial yourserial --taps 5   # discharge in 5 taps, not 7
  python3 feedothers.py --selftest                     # logic only, no robot
"""

import argparse
import random
import sys
import time
import traceback

# anki_vector is imported lazily inside play() so --selftest runs without the SDK.
lights = None
protocol = None
Color = None
Light = None
ControlPriorityLevel = None


# ============================================================
# CONFIG
# ============================================================

DIM, FULL = 40, 255
SPINNER_STEP_S = 0.18        # ~5.5 writes/sec — the proven-safe BLE rate
POLL_S = 0.05

CHARGE_TAPS = 5              # taps to fill it, when charging by tap
CHARGE_SECONDS = 5.0         # seconds of shaking to fill it, when charging by shake

DISCHARGE_TAPS = 7           # taps to empty it again — the fixed countdown
DISCHARGE_SETTLE_S = 0.25    # ignore re-triggers this soon after a counted tap,
                             # so one enthusiastic prod doesn't register twice

FULL_HOLD_S = 0.6            # a beat on full before discharge taps start counting

PET_QUIT_HOLD_S = 0.4        # hold his back this long, while the cube is empty and
                             # waiting, to end the session

BEHAVIOR_TIMEOUT_S = 25


# Reactions. Vector is a spectator here, not the one being fed — so the register
# is INTEREST and DELIGHT, not the contentment of feeding.py's finale. He's
# watching someone else get a snack.
CHARGE_DONE_CLIPS = [
    "anim_reacttoblock_success_01",
    "anim_feedback_goodrobot_01",
]
DISCHARGE_DONE_CLIPS = [
    "anim_keepaway_getout_satisfied_01",
    "anim_feedback_iloveyou_02",
    "anim_feedback_goodrobot_02",
    "anim_launch_reacttoputdown",
]
IDLE_WATCH_CLIPS = [
    "anim_rtpickup_loop_01",
]


# ============================================================
# PURE LOGIC  (unit-tested offline via --selftest)
# ============================================================

def uniform_levels(progress):
    """All four corners brighten together. This is the ONLY light pattern the
    game uses — charging, full, and discharging all read from it. progress is
    0.0 (empty) to 1.0 (full)."""
    progress = max(0.0, min(1.0, progress))
    lvl = int(DIM + (FULL - DIM) * progress)
    return [lvl] * 4


def discharge_progress(taps_done, taps_needed=DISCHARGE_TAPS):
    """Remaining charge after N taps. 0 taps -> 1.0 (full), taps_needed -> 0.0."""
    if taps_needed <= 0:
        return 0.0
    taps_done = max(0, min(taps_needed, taps_done))
    return 1.0 - (taps_done / taps_needed)


def is_discharged(taps_done, taps_needed=DISCHARGE_TAPS):
    return taps_done >= taps_needed


def charge_step(progress, taps_needed=CHARGE_TAPS):
    """One tap's worth of charge, clamped at full."""
    if taps_needed <= 0:
        return 1.0
    return min(1.0, progress + 1.0 / taps_needed)


def creatures_line(n):
    """Singular/plural, same convention as points() in keepaway/reaction."""
    return f"{n} friend" if n == 1 else f"{n} friends"


def end_line(n):
    if n == 0:
        return "Nobody wanted a snack. Maybe next time."
    return f"That's {creatures_line(n)} fed. Good job."


# ============================================================
# ROBOT HELPERS  (same shapes as keepaway.py / feeding.py)
# ============================================================

def _wait(fut, timeout=BEHAVIOR_TIMEOUT_S):
    if hasattr(fut, "result"):
        try:
            fut.result(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass


def _clip(robot, name, wait=True):
    """A CLIP, by name. The Animation OBJECT is constructed directly so the SDK
    skips _ensure_loaded() — a bare string makes it fetch all 589 clips, which
    times out on the Pi and the animation silently never plays."""
    try:
        fut = robot.anim.play_animation(protocol.Animation(name=name))
        if wait:
            _wait(fut)
        return fut
    except Exception as exc:  # noqa: BLE001
        print(f"  [anim failed] {name}: {exc}")
        return None


def _say(robot, text):
    try:
        _wait(robot.behavior.say_text(text), timeout=15)
    except Exception:  # noqa: BLE001
        pass


def _say_skippable(robot, text):
    """Say one line while watching the back sensor. Returns True if petted during
    it (= skip the rest of the narration). The current line finishes speaking —
    audio can't be cut mid-word — but the rest is dropped."""
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


def _held_for(robot, seconds):
    """True if the back sensor stays held for the full duration."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not _touching(robot):
            return False
        time.sleep(0.03)
    return True


# ============================================================
# CUBE
#   set_light_corners needs Light OBJECTS. Raw Color objects fail SILENTLY —
#   that bug cost hours once already.
# ============================================================

def _light(colour, level):
    rgb = (0, 0, level) if colour == "blue" else (0, level, 0)
    return Light(on_color=Color(rgb=rgb))


def _corners(cube, levels, colour):
    cols = [_light(colour, v) if v > 0 else lights.off_light for v in levels]
    try:
        cube.set_light_corners(*cols)
    except Exception as exc:  # noqa: BLE001
        print(f"  (light err: {exc})")


def _blank(cube):
    try:
        cube.set_lights_off()
    except Exception:  # noqa: BLE001
        pass


def _cube_ok(cube):
    try:
        return bool(cube.is_connected)
    except Exception:  # noqa: BLE001
        return False


def _reconnect_cube(robot, cube):
    """A dropped cube stops taking light commands, which looks exactly like the
    lights 'not responding'. Worth checking before blaming anything else."""
    if _cube_ok(cube):
        return cube
    print("  [cube] DISCONNECTED — reconnecting...")
    try:
        robot.world.connect_cube()
    except Exception:  # noqa: BLE001
        pass
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        c = robot.world.connected_light_cube
        if c is not None:
            print("  [cube] reconnected.")
            return c
        time.sleep(0.5)
    print("  [cube] couldn't reconnect.")
    return cube


def _tap_time(cube):
    try:
        return cube.last_tapped_time or 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _tapped(cube, since):
    t = _tap_time(cube)
    return t > since


def _moving(cube):
    try:
        return bool(cube.is_moving)
    except Exception:  # noqa: BLE001
        return False


# ============================================================
# PHASES
# ============================================================

def _spinner_until_input(robot, cube, mode, colour):
    """EMPTY: one light chases round until someone starts feeding, OR until
    Vector gets petted — which ends the session. Returns "charge" or "quit"."""
    pos = 0
    since = _tap_time(cube)
    while True:
        levels = [0, 0, 0, 0]
        levels[pos % 4] = FULL
        _corners(cube, levels, colour)
        t0 = time.monotonic()
        while time.monotonic() - t0 < SPINNER_STEP_S:
            # The quit gesture only exists HERE — while the cube sits empty and
            # waiting. Mid-charge or mid-discharge a pet does nothing, so you
            # can't accidentally end the session by steadying him.
            if _touching(robot):
                if _held_for(robot, PET_QUIT_HOLD_S):
                    _pet_release(robot)
                    return "quit"
            if mode == "tap" and _tapped(cube, since):
                return "charge"
            if mode == "shake" and _moving(cube):
                return "charge"
            time.sleep(0.02)
        pos += 1


def _charge(cube, mode, colour, taps_needed=CHARGE_TAPS,
            shake_seconds=CHARGE_SECONDS):
    """Fill it. Advances ONLY on real input, pauses the moment you stop. The
    lights are uniform the whole way up — all four corners together, no
    corner-by-corner fill."""
    progress = 0.0
    last_tap = _tap_time(cube)

    _corners(cube, uniform_levels(progress), colour)

    if mode == "tap":
        # the tap that ended the spinner counts as the first charge tap
        progress = charge_step(progress, taps_needed)
        _corners(cube, uniform_levels(progress), colour)
        while progress < 1.0:
            if _tapped(cube, last_tap):
                last_tap = _tap_time(cube)
                progress = charge_step(progress, taps_needed)
                _corners(cube, uniform_levels(progress), colour)
            time.sleep(POLL_S)
    else:
        last_drawn = -1.0
        while progress < 1.0:
            if _moving(cube):
                progress = min(1.0, progress + POLL_S / shake_seconds)
                if abs(progress - last_drawn) >= 0.02:
                    _corners(cube, uniform_levels(progress), colour)
                    last_drawn = progress
            time.sleep(POLL_S)

    # FULL: all four solid. No pulse, no breathing — feeding.py pulsed because it
    # was asking to be carried over to Vector. Nothing has to be carried here, so
    # it just sits there lit.
    _corners(cube, [FULL] * 4, colour)
    return progress


def _discharge(robot, cube, colour, taps_needed=DISCHARGE_TAPS):
    """Empty it again, one visible step per tap. Fixed count — this does NOT
    mirror how long the charge took."""
    time.sleep(FULL_HOLD_S)
    last_tap = _tap_time(cube)
    taps_done = 0
    print(f"  [discharge] {taps_needed} taps to empty it")

    while not is_discharged(taps_done, taps_needed):
        if _tapped(cube, last_tap):
            last_tap = _tap_time(cube)
            taps_done += 1
            _corners(cube, uniform_levels(
                discharge_progress(taps_done, taps_needed)), colour)
            print(f"  [discharge] {taps_done}/{taps_needed}")
            # swallow the tail of the same physical prod
            time.sleep(DISCHARGE_SETTLE_S)
            last_tap = _tap_time(cube)
        time.sleep(POLL_S)

    _blank(cube)
    return taps_done


# ============================================================
# MAIN
# ============================================================

def play(serial, colour, mode, discharge_taps):
    global lights, protocol, Color, Light, ControlPriorityLevel

    import anki_vector
    from anki_vector import lights as _lights
    from anki_vector.color import Color as _Color
    from anki_vector.connection import ControlPriorityLevel as _CPL
    from anki_vector.lights import Light as _Light
    from anki_vector.messaging import protocol as _protocol

    lights = _lights
    protocol = _protocol
    Color = _Color
    Light = _Light
    ControlPriorityLevel = _CPL

    fed = 0

    with anki_vector.Robot(
        serial,
        behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
    ) as robot:
        try:
            robot.anim.load_animation_trigger_list()
        except Exception:  # noqa: BLE001
            pass

        print("[cube] connecting...")
        try:
            robot.world.connect_cube()
        except Exception:  # noqa: BLE001
            pass
        cube = robot.world.connected_light_cube
        deadline = time.monotonic() + 20
        while cube is None and time.monotonic() < deadline:
            time.sleep(0.5)
            cube = robot.world.connected_light_cube
        if cube is None:
            print("[cube] no cube. Can't play without one.")
            return
        print("[cube] connected.")

        skipped = _say_skippable(
            robot, "Bring me your friends. Tap the cube with them to fill it up.")
        if not skipped:
            _say_skippable(
                robot,
                f"Then tap it {discharge_taps} more times to feed them.")
            _say_skippable(robot, "Pet my back when the cube is empty to stop.")
        _pet_release(robot)

        while True:
            cube = _reconnect_cube(robot, cube)

            what = _spinner_until_input(robot, cube, mode, colour)
            if what == "quit":
                break

            print("[charge] filling...")
            _charge(cube, mode, colour)
            _clip(robot, random.choice(CHARGE_DONE_CLIPS))

            _discharge(robot, cube, colour, discharge_taps)
            fed += 1
            print(f"[fed] {fed}")
            _clip(robot, random.choice(DISCHARGE_DONE_CLIPS))

        _blank(cube)
        _say(robot, end_line(fed))
        print(end_line(fed))


# ============================================================
# SELF-TEST
# ============================================================

def selftest():
    checks = []

    def ok(label, cond):
        checks.append((label, bool(cond)))

    # uniform_levels
    ok("empty is all four at DIM", uniform_levels(0.0) == [DIM] * 4)
    ok("full is all four at FULL", uniform_levels(1.0) == [FULL] * 4)
    ok("uniform: all four always equal", len(set(uniform_levels(0.37))) == 1)
    ok("uniform: always four corners", len(uniform_levels(0.5)) == 4)
    ok("uniform clamps low", uniform_levels(-1.0) == [DIM] * 4)
    ok("uniform clamps high", uniform_levels(2.0) == [FULL] * 4)
    ok("uniform is monotonic",
       uniform_levels(0.2)[0] < uniform_levels(0.6)[0] < uniform_levels(0.9)[0])
    # the whole point of this game vs feeding.py stage 2: never a partial corner
    ok("no corner-by-corner fill ever",
       all(len(set(uniform_levels(p / 20))) == 1 for p in range(21)))

    # discharge_progress
    ok("0 taps -> still full", discharge_progress(0) == 1.0)
    ok("7 taps -> empty", discharge_progress(7) == 0.0)
    ok("halfway-ish", 0.4 < discharge_progress(3) < 0.7)
    ok("discharge is monotonic down",
       discharge_progress(1) > discharge_progress(4) > discharge_progress(6))
    ok("discharge clamps over", discharge_progress(99) == 0.0)
    ok("discharge clamps under", discharge_progress(-5) == 1.0)
    ok("custom tap count", discharge_progress(2, 4) == 0.5)
    ok("zero taps_needed is safe", discharge_progress(0, 0) == 0.0)

    # is_discharged
    ok("not done at 6", not is_discharged(6))
    ok("done at 7", is_discharged(7))
    ok("done past 7", is_discharged(12))
    ok("custom: done at 5 of 5", is_discharged(5, 5))
    ok("custom: not done at 4 of 5", not is_discharged(4, 5))

    # charge_step
    ok("charge accumulates", charge_step(0.0) > 0.0)
    ok("charge clamps at 1", charge_step(0.99) == 1.0)
    ok("five taps fills it",
       abs(charge_step(charge_step(charge_step(charge_step(
           charge_step(0.0))))) - 1.0) < 1e-9)
    ok("zero taps_needed is safe", charge_step(0.0, 0) == 1.0)

    # lines
    ok("singular friend", creatures_line(1) == "1 friend")
    ok("plural friends", creatures_line(2) == "2 friends")
    ok("zero takes plural", creatures_line(0) == "0 friends")
    ok("end line names the count", "3 friends" in end_line(3))
    ok("end line handles one", "1 friend" in end_line(1))
    ok("end line handles none", "Nobody" in end_line(0))
    ok("end line never says 1 friends", "1 friends" not in end_line(1))

    passed = sum(1 for _, c in checks if c)
    for label, c in checks:
        print(f"  [{'ok' if c else 'FAIL'}] {label}")
    print(f"\n{passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


def main():
    ap = argparse.ArgumentParser(description="Feed Vector's friends.")
    ap.add_argument("--serial", help="Vector's serial number")
    ap.add_argument("--green", action="store_true",
                    help="green cube lights instead of blue")
    ap.add_argument("--tap", action="store_true",
                    help="tap the cube to charge it (shake is the default)")
    ap.add_argument("--taps", type=int, default=DISCHARGE_TAPS,
                    help=f"taps needed to discharge (default {DISCHARGE_TAPS})")
    ap.add_argument("--selftest", action="store_true",
                    help="run the offline logic checks and exit")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())

    if args.taps < 1:
        ap.error("--taps must be at least 1")

    colour = "green" if args.green else "blue"
    mode = "tap" if args.tap else "shake"

    try:
        play(args.serial, colour, mode, args.taps)
    except KeyboardInterrupt:
        print("\nstopped.")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
