#!/usr/bin/env python3
"""
feeding.py — Vector's feeding game.

Shake his cube to fill it with food. When it's full he trudges over, nests his
lift on it, and eats — the cube discharging exactly the way you charged it, his
head rising as he swallows. Then he wants more. Feed him again and the cube
charges a harder way... and this time he overdoes it.

THE LOOP
  spinner (empty)  ->  you charge it  ->  it PULSES: full, bring it to him  ->
  the camera confirms it's really his cube  ->  slow, tired drive up  ->  fluid
  nest  ->  he eats (the discharge is the charge, reversed)  ->  back off, angry,
  "MORE!"  ->  round 2  ->  he eats too much  ->  WITHDRAWAL  ->  the big spin.

The instructions deliberately say NOTHING about the second stage or the
withdrawal. Those are the surprise. Pet him to skip the explanation.

HARD-WON DETAILS (all measured on hardware — please don't "fix" these)
  * ANIMATIONS ARE CLIPS. Play them with protocol.Animation(name=...), never a
    bare string: a string makes the SDK fetch the raw 589-clip list, which TIMES
    OUT on the Pi, and the animation then silently never plays.
  * THE ToF FLOORS AT 30mm. It physically cannot see closer. So he halts at 40mm
    and closes the last 40mm BLIND, on dead reckoning. If the cube was put down
    nearer than 40mm he REVERSES to it first (checked ONCE, on first sighting),
    or the creep would just shove it away.
  * THE ToF SEES ANY OBJECT — a hand, a wall, the table edge. So the CAMERA
    confirms it's actually the cube (its marker) BEFORE we trust the ToF for the
    approach. Without that he charges at whatever happens to be in front of him.
  * BUT THE CAMERA ONLY WORKS AT A DISTANCE. Up close the marker falls out of
    frame, so a cube put down right under his nose is never recognised — he just
    sits there pulsing. If something's that close and unidentified, he reverses
    until he can get a proper look at it.
  * set_light_corners NEEDS Light objects, not raw Colors. Raw Colors fail
    silently.
  * OVERPUSH AT 3 rad/s. At 10 it drives so hard into the cube that it pops a
    wheelie — the overpush and the wheelie are the SAME motion at different
    intensities.
  * THE WHEELIE NEEDS set_lift_motor, NOT set_lift_height. set_lift_height is a
    position controller: it stalls politely against the cube and gives up. The
    direct motor drives open-loop and keeps applying torque, so the force goes
    into the chassis. It also needs a CHARGED BATTERY — on a low one it silently
    can't generate the torque.
  * NEST ANGLE IS 31.5 deg. Lift range is -12..45 deg (32..92mm, ratio 0..1).

  python3 feeding.py --serial yourserial
  python3 feeding.py --serial yourserial --green      # green lights, like his eyes
  python3 feeding.py --serial yourserial --tap        # tap to charge, not shake
  python3 feeding.py --serial yourserial -w           # force the wheelie   (debug)
  python3 feeding.py --serial yourserial -o           # force the overpush  (debug)
  python3 feeding.py --serial yourserial -r           # force the head rock (debug)
"""

import argparse
import random
import sys
import threading
import time
import traceback

import anki_vector
from anki_vector import lights
from anki_vector.color import Color
from anki_vector.connection import ControlPriorityLevel
from anki_vector.lights import Light
from anki_vector.messaging import protocol
from anki_vector.util import degrees, distance_mm, speed_mmps


# ============================================================================
# GEOMETRY  (CCIS shows the lift as -12..45 deg; the SDK wants a 0..1 ratio)
# ============================================================================
LIFT_DEG_MIN, LIFT_DEG_MAX = -12.0, 45.0
LIFT_MM_MIN, LIFT_MM_MAX = 32.0, 92.0
HEAD_DEG_MIN, HEAD_DEG_MAX = -22.0, 45.0

NEST_DEG = 31.5              # where the lift rests ON the cube. Measured — and it
                             # checks out: 31.5 deg = 77.8mm ~= 32mm (the height a
                             # cube on the ground engages at) + 44mm (a cube). The
                             # fork is sitting exactly one cube up.
APPROACH_LIFT_DEG = 40.0     # rises to this on the way IN. MUST BE ABOVE THE NEST:
                             # the fork has to pass OVER the cube before it can come
                             # down onto it. Anything below 31.5 ploughs into it.
# The rise and the fall need DIFFERENT speeds. The fork has to get up and over the
# cube EARLY (so it clears it), but the descent has to land LATE — right as he stops
# rolling. Bring it down too fast and it drops into the gap in FRONT of the cube and
# he drives the last stretch with the fork already low, so it never seats on top.
APPROACH_LIFT_UP_SPEED = 16.0    # up and over: quick
APPROACH_LIFT_DOWN_SPEED = 3.0   # down onto it: slow, so it lands as he halts
APPROACH_LIFT_HOLD_S = 0.5       # a beat at the top before it starts coming down
                                 # (3.0 / 0.5 -- TUNED ON HARDWARE. The drive is
                                 #  40mm at 25mm/s = 1.6s, and the fork has to seat
                                 #  right as he halts. Faster and it drops into the
                                 #  gap in FRONT of the cube and never gets on top.)
CLEAR_LIFT_DEG = 45.0        # full up, to get the arm off the cube
LOOK_HEAD_DEG = -5.0         # head angle where the camera can see the cube



# ============================================================================
# THE DRIVE UP
# ============================================================================
TOF_STOP_MM = 40.0           # he halts here. If the cube was put down CLOSER than
                             # this he BACKS UP to it first — otherwise the blind
                             # creep overshoots and shoves the cube away.
TOF_TOL_MM = 3.0
APPROACH_MM = 40.0           # then closes the last gap BLIND
TOF_MAX_MM = 300.0           # further than this and he can't work with it

# THE CAMERA ONLY RECOGNISES THE CUBE AT A DISTANCE — measured on hardware. Up
# close the marker falls out of frame, so a cube placed right under his nose is
# NEVER confirmed: he'd sit there pulsing until the timeout. (That's exactly what
# the "pulsing never stops" bug was.) So if something is sitting that close and he
# still hasn't recognised it, reverse until the camera can get a proper look.
CAMERA_MIN_MM = 80.0         # nearer than this and the camera can't identify it
CAMERA_BACKOFF_TO_MM = 160.0 # so reverse out to about here and look again
CAMERA_GRACE_S = 6.0         # give the camera this long before assuming it's stuck
DRIVE_SPEED = 30.0           # mm/s — slow. He's tired.
APPROACH_SPEED = 25.0        # mm/s for the final fluid approach
BACK_OFF_MM = 40.0           # 4cm back, so he can animate clear of the cube
BACK_OFF_SPEED = 50.0
CUBE_SEEN_TIMEOUT_S = 45.0


# ============================================================================
# CHARGING  (johnalpha's proven test_charge_modes logic)
# ============================================================================
DIM, FULL = 40, 255
SPINNER_STEP_S = 0.18
CHARGE_TAPS, CHARGE_TAPS_2 = 5, 8          # stage 2 is weightier
CHARGE_SECONDS, CHARGE_SECONDS_2 = 5.0, 8.0
POLL_S = 0.05

# the cube PULSES once it's full, and keeps pulsing until you put it in front of
# him — it's the cube asking to be delivered, not just a one-shot notification
# BLE cube-light writes have a REAL rate limit. The spinner (0.18s/step, ~5.5
# writes/sec) is proven safe. The pulse was firing 14 steps in 0.55s = ~25/sec,
# which backed up the BLE queue — so when we told it to stop, a backlog of stale
# pulse frames kept draining to the cube and it carried on pulsing. Slow it down.
PULSE_STEPS = 8              # one bright->dim->bright breath
PULSE_STEP_S = 0.14          # ~7 writes/sec — inside the safe range
PULSE_MIN, PULSE_MAX = 60, 255

# discharge mirrors how long YOU took to charge it — clamped so it can't drag
DISCHARGE_MIN_S, DISCHARGE_MAX_S = 2.5, 10.0
DISCHARGE_STEPS = 40


# ============================================================================
# THE WITHDRAWALS
# ============================================================================
PUSH_SPEED, PUSH_DOWN_S = 3.0, 0.15        # TUNED. 10 rad/s pops a wheelie.
WHEELIE_UP, WHEELIE_DOWN = 10.0, -10.0
WHEELIE_WINDUP_S, WHEELIE_HOLD_S = 0.45, 0.5
ROCK_CYCLES, ROCK_SPEED = 17, 10.0         # Cozmo did exactly 17
ROCK_FIRST_S, ROCK_DECAY = 0.30, 0.93

ANGRY_CLIP = "anim_rtpickup_putdown_03"    # sleepy_vector's furious wake

# The celebration, picked at random so it doesn't get stale — the big spin every
# single time was too much.
#
# NOTE ON WHAT ISN'T HERE: no wingame, no blackjack, no cubespinner success. Those
# are all "I BEAT YOU" clips, and feeding isn't competitive — nobody lost. He's
# been fed and he's pleased. The register we want is CONTENTMENT and AFFECTION,
# not triumph. Hence:
FINALE_CLIPS = [
    "anim_keepaway_getout_satisfied_01",   # *satisfied* — the perfect after-a-meal
    "anim_feedback_goodrobot_01",          # his reaction to being praised
    "anim_feedback_goodrobot_02",
    "anim_feedback_iloveyou_02",           # affectionate
    "anim_launch_reacttoputdown",          # the big spin, still in the mix
]


# ============================================================================
# SMALL HELPERS
# ============================================================================

def lift_ratio(deg):
    deg = max(LIFT_DEG_MIN, min(LIFT_DEG_MAX, deg))
    return (deg - LIFT_DEG_MIN) / (LIFT_DEG_MAX - LIFT_DEG_MIN)


def lift_mm(deg):
    return LIFT_MM_MIN + lift_ratio(deg) * (LIFT_MM_MAX - LIFT_MM_MIN)


def _wait(fut, timeout=25):
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


def _tof(robot):
    try:
        r = robot.proximity.last_sensor_reading
        return r.distance.distance_mm if r else None
    except Exception:  # noqa: BLE001
        return None


def _lift_to(robot, deg, speed=10.0, wait=True):
    fut = robot.behavior.set_lift_height(lift_ratio(deg), accel=speed,
                                         max_speed=speed)
    if wait:
        _wait(fut)
    return fut


def _head_to(robot, deg, speed=10.0, wait=True):
    deg = max(HEAD_DEG_MIN, min(HEAD_DEG_MAX, deg))
    fut = robot.behavior.set_head_angle(degrees(deg), accel=speed, max_speed=speed)
    if wait:
        _wait(fut)
    return fut


def _lift_deg(robot):
    """The lift's live angle, in the same degrees CCIS shows."""
    try:
        mm = robot.lift_height_mm
        r = (mm - LIFT_MM_MIN) / (LIFT_MM_MAX - LIFT_MM_MIN)
        return LIFT_DEG_MIN + r * (LIFT_DEG_MAX - LIFT_DEG_MIN)
    except Exception:  # noqa: BLE001
        return None


def _lift_motor(robot, speed):
    try:
        robot.motors.set_lift_motor(speed)
    except Exception:  # noqa: BLE001
        pass


def _head_motor(robot, speed):
    try:
        robot.motors.set_head_motor(speed)
    except Exception:  # noqa: BLE001
        pass


# ============================================================================
# CUBE LIGHTS
#   set_light_corners needs Light OBJECTS. Raw Color objects fail SILENTLY —
#   that bug cost hours once already.
# ============================================================================

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


def _uniform(progress):
    """STAGE 1: all four brighten together."""
    lvl = int(DIM + (FULL - DIM) * progress)
    return [lvl] * 4


def _fill(progress):
    """STAGE 2: corners fill one by one."""
    n = progress * 4.0
    out = []
    for i in range(4):
        if i + 1 <= n:
            out.append(FULL)
        elif i < n:
            out.append(int(DIM + (FULL - DIM) * (n - i)))
        else:
            out.append(0)
    return out


# ============================================================================
# CHARGING  —  the user feeds him
# ============================================================================

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


def _tapped(cube, since):
    try:
        t = cube.last_tapped_time
        return t is not None and t > since
    except Exception:  # noqa: BLE001
        return False


def _moving(cube):
    try:
        return bool(cube.is_moving)
    except Exception:  # noqa: BLE001
        return False


def _spinner_until_input(cube, mode, colour):
    """EMPTY: one light chases round until you start feeding him."""
    pos = 0
    try:
        since = cube.last_tapped_time or 0.0
    except Exception:  # noqa: BLE001
        since = 0.0
    while True:
        levels = [0, 0, 0, 0]
        levels[pos % 4] = FULL
        _corners(cube, levels, colour)
        t0 = time.monotonic()
        while time.monotonic() - t0 < SPINNER_STEP_S:
            if mode == "tap" and _tapped(cube, since):
                return
            if mode == "shake" and _moving(cube):
                return
            time.sleep(0.02)
        pos += 1


def _charge(cube, mode, level_fn, taps_needed, shake_seconds, colour):
    """Charge advances ONLY on real input and PAUSES the moment you stop. Starts
    immediately — no settle beat. Returns how long it took, because the discharge
    mirrors it."""
    started = time.monotonic()
    progress = 0.0
    try:
        last_tap = cube.last_tapped_time or 0.0
    except Exception:  # noqa: BLE001
        last_tap = 0.0

    _corners(cube, level_fn(progress), colour)

    if mode == "tap":
        # the tap that ended the spinner counts as the first charge tap
        progress = min(1.0, progress + 1.0 / taps_needed)
        _corners(cube, level_fn(progress), colour)
        while progress < 1.0:
            if _tapped(cube, last_tap):
                last_tap = cube.last_tapped_time
                progress = min(1.0, progress + 1.0 / taps_needed)
                _corners(cube, level_fn(progress), colour)
            time.sleep(POLL_S)
    else:
        last_drawn = -1.0
        while progress < 1.0:
            if _moving(cube):
                progress = min(1.0, progress + POLL_S / shake_seconds)
                if abs(progress - last_drawn) >= 0.02:
                    _corners(cube, level_fn(progress), colour)
                    last_drawn = progress
            time.sleep(POLL_S)

    _corners(cube, [FULL] * 4, colour)
    return time.monotonic() - started


def _pulse(cube, colour, stop):
    """FULL. All four lights breathe, and keep breathing until he actually sees the
    cube in front of him. Not a one-shot chime — it's the cube saying 'I'm ready,
    bring me over', and it holds that state until he spots it."""
    i = 0
    half = PULSE_STEPS // 2
    while not stop.is_set():
        k = i % PULSE_STEPS
        frac = (k / half) if k < half else (2.0 - k / half)   # triangle wave
        lvl = int(PULSE_MIN + (PULSE_MAX - PULSE_MIN) * frac)
        _corners(cube, [lvl] * 4, colour)
        i += 1
        t0 = time.monotonic()
        while time.monotonic() - t0 < PULSE_STEP_S:
            if stop.is_set():
                return
            time.sleep(0.01)


# ============================================================================
# FINDING THE CUBE  —  camera first, THEN the ToF
# ============================================================================

def _confirm_cube(robot, cube):
    """The ToF sees ANY object — your hand, a wall, the edge of the table. If we
    drove on the ToF alone he'd charge at whatever happened to be in front of him.
    So the CAMERA has to confirm it's really the cube (by its marker) first. Only
    then do we trust the ToF for the approach."""
    # (the camera feed is opened ONCE at startup and closed on the way out — if we
    #  re-init it here every cycle the SDK leaves streaming enabled on the robot and
    #  moans about it at shutdown, and the script hangs on exit)
    _lift_to(robot, LIFT_DEG_MIN)            # arm out of the camera's way
    _head_to(robot, LOOK_HEAD_DEG)           # look at the table

    print("  [look] confirming it's really the cube (camera)...")
    deadline = time.monotonic() + CUBE_SEEN_TIMEOUT_S
    grace_over = time.monotonic() + CAMERA_GRACE_S
    backed_off = False

    while time.monotonic() < deadline:
        seen = False
        try:
            seen = bool(cube.is_visible)
        except Exception:  # noqa: BLE001
            seen = False

        if seen:
            d = _tof(robot)
            if d is not None and d <= TOF_MAX_MM:
                print(f"  [look] cube CONFIRMED — camera has it, ToF says {d:.0f}mm")
                return True

        # The camera CANNOT identify the cube up close — the marker falls out of
        # frame. So if something's sitting right under his nose and he still hasn't
        # recognised it, reverse until he can actually look at it. Self-correcting:
        # if it turns out not to be the cube, he just keeps waiting.
        if (not backed_off) and time.monotonic() > grace_over:
            d = _tof(robot)
            if d is not None and d < CAMERA_MIN_MM:
                print(f"  [look] something at {d:.0f}mm — too close for the camera "
                      f"to make out. Backing up to get a proper look.")
                now = _reverse_to(robot, CAMERA_BACKOFF_TO_MM)
                print(f"  [look] backed off to {now}mm — looking again")
                backed_off = True

        time.sleep(0.1)

    print("  [look] never got a confirmed sighting.")
    return False


# ============================================================================
# THE DRIVE UP  —  one fluid motion
# ============================================================================

def _reverse_to(robot, target_mm, timeout=20.0):
    """Reverse until the ToF reads at least target_mm."""
    robot.motors.set_wheel_motors(-DRIVE_SPEED, -DRIVE_SPEED)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = _tof(robot)
        if d is not None and d >= target_mm:
            break
        time.sleep(0.02)
    robot.motors.set_wheel_motors(0, 0)
    return _tof(robot)


def _back_off_if_close(robot):
    """ONE-TIME, checked only when he first spots the cube: if you put it down
    NEARER than 40mm, he reverses to it. Without this, the 40mm blind creep would
    just shove the cube across the table instead of nesting on it. Not re-checked
    on retries — from here he only ever drives forwards."""
    d = _tof(robot)
    if d is None or d >= TOF_STOP_MM - TOF_TOL_MM:
        return
    print(f"  [drive] cube's at {d:.0f}mm — too close. Reversing to "
          f"{TOF_STOP_MM:.0f}mm.")
    print(f"  [drive] now at {_reverse_to(robot, TOF_STOP_MM)}mm")


def _approach_to_stop(robot):
    """Drive FORWARD until the ToF reads 40mm. (Too-close was dealt with once, on
    first sighting — here he only ever moves forwards.)"""
    d = _tof(robot)
    if d is None:
        return False
    if d > TOF_STOP_MM:
        print(f"  [drive] trudging over from {d:.0f}mm...")
        robot.motors.set_wheel_motors(DRIVE_SPEED, DRIVE_SPEED)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            d = _tof(robot)
            if d is not None and d <= TOF_STOP_MM:
                break
            time.sleep(0.02)
        robot.motors.set_wheel_motors(0, 0)
    print(f"  [drive] holding at {_tof(robot)}mm")
    return True


def _drive_up(robot, approach_mm, approach_lift_deg,
              lift_down_speed=APPROACH_LIFT_DOWN_SPEED,
              lift_hold_s=APPROACH_LIFT_HOLD_S):
    """Trudge to 40mm, a beat, then ONE FLUID MOTION: forward while the lift rises
    OVER the cube and comes back DOWN onto it BEFORE he stops moving.

    NO "did he land on it?" CHECK. We tried probing the lift below the nest to see
    if the cube blocked it — it doesn't. The lift reached ~25 deg when commanded to
    24, i.e. it wasn't obstructed at all, so the check false-positived on every
    single nest. Worse, that slow probing descent was most likely just shoving the
    cube back out from under him. (The wheelie still works because set_lift_motor
    slams down open-loop with real force from full up — a completely different
    interaction from a gentle creep.)"""
    _head_to(robot, HEAD_DEG_MIN)            # head full down — he's tired
    _lift_to(robot, LIFT_DEG_MIN)

    if not _approach_to_stop(robot):
        print("  [drive] lost the cube.")
        return False

    time.sleep(0.25)                         # the beat before he commits
    print(f"  [drive] last {approach_mm:.0f}mm blind (the ToF can't see nearer "
          f"than 30)")

    def _lift_over():
        # UP and OVER the cube — quick, and it must go ABOVE the nest angle, or the
        # fork just ploughs into the side of it.
        _lift_to(robot, approach_lift_deg, speed=APPROACH_LIFT_UP_SPEED)
        # hold it up there while he closes most of the distance...
        time.sleep(lift_hold_s)
        # ...then lower it SLOWLY, so the fork settles onto the cube just as he
        # comes to a stop, rather than dropping into the gap in front of it
        _lift_to(robot, NEST_DEG, speed=lift_down_speed)

    t = threading.Thread(target=_lift_over, daemon=True)
    t.start()
    _wait(robot.behavior.drive_straight(distance_mm(approach_mm),
                                        speed_mmps(APPROACH_SPEED)), timeout=15)
    t.join(timeout=8)

    _lift_to(robot, NEST_DEG, speed=6.0)   # settle onto it
    print(f"  [drive] nested at {NEST_DEG} deg")
    return True


# ============================================================================
# EATING  —  the discharge is the charge, backwards
# ============================================================================

def _eat(robot, cube, colour, level_fn, duration_s):
    """He empties the cube exactly the way you filled it, in reverse — and his
    head rises from full down to full up as he swallows. Ends with the lights
    completely OFF."""
    duration_s = max(DISCHARGE_MIN_S, min(DISCHARGE_MAX_S, duration_s))
    print(f"  [eat] emptying the cube over {duration_s:.1f}s")

    step_s = duration_s / DISCHARGE_STEPS
    span = HEAD_DEG_MAX - HEAD_DEG_MIN

    for i in range(DISCHARGE_STEPS, -1, -1):
        progress = i / DISCHARGE_STEPS
        _corners(cube, level_fn(progress), colour)
        # the head is the progress bar
        _head_to(robot, HEAD_DEG_MIN + span * (1.0 - progress),
                 speed=8.0, wait=False)
        time.sleep(step_s)

    _blank(cube)                             # all the way OFF
    _head_to(robot, HEAD_DEG_MAX, speed=8.0)
    print("  [eat] empty.")


# ============================================================================
# RECOIL  —  get clear of the cube before animating
# ============================================================================

def _recoil(robot):
    """Lift up, back off 4cm, lift down. The lift MUST come up first — backing
    away with the arm still at the nest angle drags it across the cube."""
    print("  [recoil] up, back, down")
    _lift_to(robot, CLEAR_LIFT_DEG, speed=20.0)
    _wait(robot.behavior.drive_straight(distance_mm(-BACK_OFF_MM),
                                        speed_mmps(BACK_OFF_SPEED)), timeout=10)
    _lift_to(robot, LIFT_DEG_MIN, speed=10.0)


# ============================================================================
# THE THREE WITHDRAWALS  —  he's had too much
# ============================================================================

def _w_overpush(robot):
    """A hard jab into the cube. 3 rad/s: at 10 this drives so hard it pops a
    wheelie instead. The return is closed-loop — the open-loop motor has no target
    and would sail straight to full up."""
    print("  [withdrawal] OVERPUSH")
    target_mm = lift_mm(NEST_DEG)
    _lift_motor(robot, -PUSH_SPEED)
    time.sleep(PUSH_DOWN_S)
    _lift_motor(robot, PUSH_SPEED)
    end = time.monotonic() + 0.6
    while time.monotonic() < end:
        try:
            if robot.lift_height_mm >= target_mm:
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0 / 60)
    _lift_motor(robot, 0)


def _w_wheelie(robot):
    """Maximum force into the cube: the lift can't go down, so the chassis goes
    up. set_lift_height CANNOT do this — it's a position controller and just
    stalls politely. Needs a charged battery."""
    print("  [withdrawal] WHEELIE")
    _lift_motor(robot, WHEELIE_UP)
    time.sleep(WHEELIE_WINDUP_S)
    _lift_motor(robot, WHEELIE_DOWN)
    time.sleep(WHEELIE_HOLD_S)
    _lift_motor(robot, 0)


def _w_headrock(robot):
    """A damped oscillation — full up, slam down, decaying over 17 cycles until it
    settles, like a struck bell. The recoil runs CONCURRENTLY inside it, so this
    one does NOT get a second recoil afterwards."""
    print("  [withdrawal] HEAD ROCK (recoils on its own)")
    _head_to(robot, HEAD_DEG_MAX, speed=20.0)

    t = threading.Thread(target=_recoil, args=(robot,), daemon=True)
    t.start()

    spd, dur = ROCK_SPEED, ROCK_FIRST_S
    for _ in range(ROCK_CYCLES):
        _head_motor(robot, -spd)
        time.sleep(dur)
        spd *= ROCK_DECAY
        dur *= ROCK_DECAY
        _head_motor(robot, spd)
        time.sleep(dur)
        spd *= ROCK_DECAY
        dur *= ROCK_DECAY
    _head_motor(robot, 0)
    t.join(timeout=12)


def _withdrawal(robot, forced):
    """Fires the INSTANT the last bite lands — no speech, no pause. Then he gets
    clear and does the big spin."""
    kind = forced or random.choice(["w", "h", "o"])
    if kind == "w":
        _w_wheelie(robot)
        _recoil(robot)
    elif kind == "o":
        _w_overpush(robot)
        _recoil(robot)
    else:
        _w_headrock(robot)      # already recoiled, concurrently — no second one

    finale = random.choice(FINALE_CLIPS)
    print(f"  [finale] {finale}")
    _clip(robot, finale, wait=True)


# ============================================================================
# NARRATION  —  skippable, and it gives NOTHING away about stage 2
# ============================================================================

def _say_skippable(robot, text):
    """Speak a line while watching his back. True if petted (skip the rest). The
    line finishes — audio can't be cut mid-word — but the rest is dropped."""
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


def _narrate(robot, mode):
    """Explains stage 1 and NOTHING else. The second stage and the withdrawal are
    the surprise — don't spoil them."""
    verb = "Shake" if mode == "shake" else "Tap"
    lines = [
        "I'm hungry!",
        f"{verb} my cube to fill it with food.",
        "All four lights glow brighter as it fills up.",
        "When it's full, I will come over and eat it.",
        "Watch my head — it rises as I swallow.",
    ]
    for line in lines:
        if _say_skippable(robot, line):
            print("  [narration skipped]")
            _pet_release(robot)          # consume it, or it leaks into the game
            _say(robot, "Okay, feed me!")
            return
    _say(robot, "Feed me!")


# ============================================================================
# THE GAME
# ============================================================================

def _cleanup(robot, cube):
    """Put everything back. The camera feed MUST be closed — leave it open and the
    SDK warns that streaming is still enabled on the robot and the script hangs
    instead of exiting."""
    try:
        robot.motors.set_wheel_motors(0, 0)
    except Exception:  # noqa: BLE001
        pass
    _lift_motor(robot, 0)
    _head_motor(robot, 0)
    if cube is not None:
        _blank(cube)
    try:
        robot.camera.close_camera_feed()
    except Exception:  # noqa: BLE001
        pass


def feeding(serial, colour, mode, approach_mm, approach_lift_deg, forced, cycles,
            lift_down_speed=APPROACH_LIFT_DOWN_SPEED,
            lift_hold_s=APPROACH_LIFT_HOLD_S):
    with anki_vector.AsyncRobot(
            serial=serial,
            cache_animation_lists=False,     # never fetch the raw clip list
            behavior_activation_timeout=30,
            # his autonomous behaviours WILL try to take the wheel mid-game
            behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
    ) as robot:
        print("Connected.")
        try:
            if robot.status.is_on_charger:
                _wait(robot.behavior.drive_off_charger())
        except Exception:  # noqa: BLE001
            pass

        # a wheelie on a flat battery silently does nothing — warn, don't abort
        try:
            b = robot.get_battery_state().result(timeout=10)
            if b.battery_level <= 1:
                print("  !! battery is LOW — the wheelie withdrawal will not fire.")
        except Exception:  # noqa: BLE001
            pass

        print("Connecting the cube...")
        try:
            robot.world.connect_cube()
        except Exception:  # noqa: BLE001
            pass
        cube = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            cube = robot.world.connected_light_cube
            if cube:
                break
            time.sleep(0.5)
        if cube is None:
            print("No cube.")
            return 1
        print("Cube connected.\n")
        time.sleep(0.3)
        _blank(cube)

        # opened ONCE here, closed in the finally — otherwise streaming stays
        # enabled on the robot and the script won't exit cleanly
        try:
            robot.camera.init_camera_feed()
        except Exception as exc:  # noqa: BLE001
            print(f"  (camera feed: {exc})")

        _narrate(robot, mode)

        try:
            for cycle in range(1, cycles + 1):
                last = (cycle == cycles)
                stage = 1 if cycle == 1 else 2
                level_fn = _uniform if stage == 1 else _fill
                taps = CHARGE_TAPS if stage == 1 else CHARGE_TAPS_2
                secs = CHARGE_SECONDS if stage == 1 else CHARGE_SECONDS_2

                print(f"\n======== FEED {cycle}/{cycles}  (stage {stage}) ========")

                # EMPTY — the spinner runs until you start feeding him
                _spinner_until_input(cube, mode, colour)

                # CHARGE — and time it: the discharge mirrors how long you took
                took = _charge(cube, mode, level_fn, taps, secs, colour)
                print(f"  [charge] FULL (took {took:.1f}s) — pulsing until you bring "
                      f"it to him")

                # FULL: the cube pulses and KEEPS pulsing until he can actually see
                # it in front of him. That's the signal it's ready, and it gives you
                # the window to put it down. It only stops when the camera confirms.
                stop_pulse = threading.Event()
                pulser = threading.Thread(target=_pulse,
                                          args=(cube, colour, stop_pulse), daemon=True)
                pulser.start()

                confirmed = _confirm_cube(robot, cube)

                stop_pulse.set()                 # he's seen it — stop asking
                pulser.join(timeout=2)
                time.sleep(0.25)                 # let any queued BLE writes drain, or a
                                                 # stale frame lands AFTER we go solid

                # a dropped cube also stops obeying light commands, which looks just
                # like the lights misbehaving
                cube = _reconnect_cube(robot, cube)

                if not confirmed:
                    _blank(cube)
                    _say(robot, "I lost my cube.")
                    break

                # solid: acknowledged, that's food. Stamped twice so it's definitely
                # the last thing the cube receives.
                _corners(cube, [FULL] * 4, colour)
                time.sleep(0.15)
                _corners(cube, [FULL] * 4, colour)
                time.sleep(0.25)                 # a beat — he's spotted it

                # ONE-TIME, on this first sighting: if you put the cube down NEARER
                # than 40mm, he reverses to it. From here on he only drives forwards.
                _back_off_if_close(robot)

                if not _drive_up(robot, approach_mm, approach_lift_deg,
                                 lift_down_speed, lift_hold_s):
                    _blank(cube)
                    _say(robot, "I can't reach my cube.")
                    break
                _eat(robot, cube, colour, level_fn, took)

                if last:
                    # THE WITHDRAWAL FIRES IMMEDIATELY. No speech, no pause, no
                    # recoil first — he's still on the cube, and the wheelie and
                    # overpush both need it there to push against.
                    _withdrawal(robot, forced)
                else:
                    _recoil(robot)               # get clear before he can animate
                    _clip(robot, ANGRY_CLIP, wait=True)
                    _say(robot, "More!")

        finally:
            _cleanup(robot, cube)

        print("\nDone.")
        return 0


def main():
    ap = argparse.ArgumentParser(description="Vector feeding game")
    ap.add_argument("--serial", default=None)
    ap.add_argument("--green", action="store_true",
                    help="green cube lights (matches his eyes); blue is default")
    ap.add_argument("--tap", action="store_true",
                    help="charge by TAPPING; shaking is the default")
    ap.add_argument("--approach-mm", type=float, default=APPROACH_MM,
                    help="how far he closes BLIND onto the cube (default 40)")
    ap.add_argument("--approach-lift-deg", type=float, default=APPROACH_LIFT_DEG,
                    help="lift angle on the way in — must be ABOVE the 31.5 nest "
                         "so the fork clears the cube (default 40)")
    ap.add_argument("--lift-down-speed", type=float, default=APPROACH_LIFT_DOWN_SPEED,
                    help="how SLOWLY the fork comes down onto the cube. Lower = it "
                         "lands later. If he stops before the fork is seated, "
                         "lower this. (default 3)")
    ap.add_argument("--lift-hold-s", type=float, default=APPROACH_LIFT_HOLD_S,
                    help="beat at the top before the fork starts descending "
                         "(default 0.5)")
    ap.add_argument("--cycles", type=int, default=2,
                    help="feeds before he overdoes it (default 2)")
    ap.add_argument("-w", "--wheelie", action="store_true",
                    help="[debug] force the WHEELIE withdrawal")
    ap.add_argument("-o", "--overpush", action="store_true",
                    help="[debug] force the OVERPUSH withdrawal")
    ap.add_argument("-r", "--headrock", action="store_true",
                    help="[debug] force the HEAD ROCK withdrawal")
    args = ap.parse_args()

    forced = ("w" if args.wheelie else
              "o" if args.overpush else
              "h" if args.headrock else None)

    try:
        return feeding(
            serial=args.serial,
            colour="green" if args.green else "blue",
            mode="tap" if args.tap else "shake",     # SHAKE IS THE DEFAULT
            approach_mm=args.approach_mm,
            approach_lift_deg=args.approach_lift_deg,
            forced=forced,
            cycles=args.cycles,
            lift_down_speed=args.lift_down_speed,
            lift_hold_s=args.lift_hold_s,
        )
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 0
    except Exception:  # noqa: BLE001
        print("[FATAL]")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
