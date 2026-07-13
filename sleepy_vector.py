#!/usr/bin/env python3
"""Sleepy Vector game. Falls asleep (GoToSleepGetIn), physical stir on each
pet (head+lift raise/lower, eyes stay asleep), at a secret 3-15 pet threshold
wakes shocked (anim_launch_wakeup_05) then furious (anim_rtpickup_putdown_03).
Solo or multiplayer mode select by petting. Self-test: --selftest"""

import argparse
import random
import sys
import time

# ---------------- PURE LOGIC ----------------

def toggle_mode(mode):
    return "multiplayer" if mode == "solo" else "solo"

def is_wake_pet(pet_count, threshold):
    return pet_count >= threshold

def choose_threshold(rng, low, high):
    return rng.randint(low, high)

def pick_trigger(available, keyword_groups, fallback=None):
    lowered = [(t, t.lower()) for t in available]
    for group in keyword_groups:
        for original, low in lowered:
            if any(k in low for k in group):
                return original
    if fallback and fallback in available:
        return fallback
    return None

def end_message(mode, pet_count):
    if mode == "solo":
        return f"You got {pet_count} pets before waking me up!"
    return f"Pet number {pet_count} woke me up! Whoever just did that, you lose!"

# ---------------- SELF-TEST ----------------

def _run_selftest():
    failures = []
    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)
    print("Running sleepy_vector.py self-test (no robot required)...\n")
    check("toggle solo->multi", toggle_mode("solo") == "multiplayer")
    check("toggle multi->solo", toggle_mode("multiplayer") == "solo")
    check("double toggle identity", toggle_mode(toggle_mode("solo")) == "solo")
    check("wake at threshold", is_wake_pet(5, 5) is True)
    check("wake beyond", is_wake_pet(6, 5) is True)
    check("no wake below", is_wake_pet(4, 5) is False)
    rng = random.Random(1)
    vals = [choose_threshold(rng, 3, 15) for _ in range(500)]
    check("threshold within 3..15", all(3 <= v <= 15 for v in vals))
    check("threshold varies", len(set(vals)) > 1)
    avail = ["GoToSleepGetIn", "NothingToDoBoredIdle"]
    check("pick sleep getin", pick_trigger(avail, [["gotosleepgetin"]]) == "GoToSleepGetIn")
    check("pick fallback", pick_trigger(avail, [["nope"]], fallback="NothingToDoBoredIdle") == "NothingToDoBoredIdle")
    check("pick None", pick_trigger(avail, [["nope"]]) is None)
    check("end solo", "5 pets" in end_message("solo", 5))
    check("end multi", "number 7" in end_message("multiplayer", 7))
    print()
    if failures:
        print(f"SELF-TEST FAILED: {failures}")
        return 1
    print("ALL SELF-TESTS PASSED.")
    return 0

# ---------------- HARDWARE GAME ----------------

MODE_LOCK_WINDOW_S = 4.0
WAKE_MIN, WAKE_MAX = 3, 15
PET_DEBOUNCE_S = 0.6
STIR_SETTLE_S = 0.15
ANIM_TIMEOUT_S = 20.0
POLL_S = 0.03

SLEEP_HEAD_DEG = -22.0
SLEEP_LIFT = 0.0
STIR_HEAD_UP_DEG = -12.0
STIR_LIFT_UP = 0.25

SHOCK_CLIP = "anim_launch_wakeup_05"       # shocked jolt awake, plays first
ANGRY_CLIP = "anim_rtpickup_putdown_03"    # then furious
CLIP_LIST_TIMEOUT_S = 120

SCOLD_LINES = [
    "I was SLEEPING! What is wrong with you?!",
    "How DARE you wake me up!",
    "That is the LAST time I sleep near you!",
    "Rude! I was having a wonderful dream!",
    "You woke me up! I am NOT happy!",
]

ANIM_CANDIDATES = {
    "sleep_getin": ([["gotosleepgetin"]], "GoToSleepGetIn"),
}

def _wait_future(result, timeout=ANIM_TIMEOUT_S):
    if hasattr(result, "result"):
        try:
            result.result(timeout=timeout)
        except Exception:
            pass

def _preload_clip_list(robot, timeout_s=CLIP_LIST_TIMEOUT_S):
    """Force-load the raw clip list with a long timeout so play_animation()
    recognizes specific clip names (default 10s times out on a slow Pi)."""
    from anki_vector.messaging import protocol
    async def _load():
        req = protocol.ListAnimationsRequest()
        result = await robot.anim.grpc_interface.ListAnimations(req, timeout=timeout_s)
        robot.anim._anim_dict = {a.name: a for a in result.animation_names}
        return len(result.animation_names)
    try:
        count = robot.conn.run_coroutine(_load()).result()
        print(f"Preloaded {count} animation clips.")
        return True
    except Exception as exc:
        print(f"  (clip list preload failed: {exc})")
        return False

def _play_clip(robot, clip_name):
    """Play a specific animation clip by exact name (deterministic)."""
    try:
        _wait_future(robot.anim.play_animation(clip_name))
    except Exception as exc:
        print(f"  (clip '{clip_name}' didn't play: {exc})")

def _drive_off_charger_if_needed(robot):
    """play_animation clips require Vector OFF the charger, so drive off first."""
    try:
        if robot.status.is_on_charger:
            print("On charger — driving off first.")
            _wait_future(robot.behavior.drive_off_charger())
    except Exception as exc:
        print(f"  (drive_off_charger had trouble: {exc})")

def _resolve_anims(robot):
    for loader in ("load_animation_trigger_list", "load_animation_list"):
        try:
            _wait_future(getattr(robot.anim, loader)())
        except Exception as exc:
            print(f"  (couldn't {loader}: {exc})")
    try:
        available = robot.anim.anim_trigger_list
    except Exception as exc:
        print(f"  (couldn't read trigger list: {exc})")
        available = []
    print(f"Vector reports {len(available)} animation triggers; resolving...")
    resolved = {}
    for key, (groups, fallback) in ANIM_CANDIDATES.items():
        resolved[key] = pick_trigger(available, groups, fallback)
        print(f"    {key:>11} -> {resolved[key] or '(none)'}")
    return resolved

def _play(robot, anims, key, wait=True):
    name = anims.get(key)
    if not name:
        return
    try:
        result = robot.anim.play_animation_trigger(name)
        if wait:
            _wait_future(result)
    except Exception as exc:
        print(f"  (animation '{name}' [{key}] didn't play: {exc})")

def _say(robot, text):
    try:
        _wait_future(robot.behavior.say_text(text))
    except Exception as exc:
        print(f"  (say_text failed: {exc})")

def _touched(robot):
    r = robot.touch.last_sensor_reading
    return r is not None and r.is_being_touched

def _wait_release(robot):
    while _touched(robot):
        time.sleep(0.05)

def _wait_for_pet(robot, timeout=None):
    deadline = (time.monotonic() + timeout) if timeout is not None else None
    _wait_release(robot)
    while True:
        if _touched(robot):
            _wait_release(robot)
            time.sleep(PET_DEBOUNCE_S)
            return True
        if deadline is not None and time.monotonic() > deadline:
            return False
        time.sleep(POLL_S)

def _stir(robot):
    """Physical stir: raise head + lift TOGETHER, then lower both together.
    Eyes stay in the sleep face so he still looks asleep."""
    from anki_vector.util import degrees
    try:
        h = robot.behavior.set_head_angle(degrees(STIR_HEAD_UP_DEG))
        l = robot.behavior.set_lift_height(STIR_LIFT_UP)
        _wait_future(h)
        _wait_future(l)
        time.sleep(STIR_SETTLE_S)
        h = robot.behavior.set_head_angle(degrees(SLEEP_HEAD_DEG))
        l = robot.behavior.set_lift_height(SLEEP_LIFT)
        _wait_future(h)
        _wait_future(l)
    except Exception as exc:
        print(f"  (stir movement failed: {exc})")

def _select_mode(robot):
    mode = "solo"
    _say(robot, "Solo mode.")
    while True:
        if not _wait_for_pet(robot, timeout=MODE_LOCK_WINDOW_S):
            return mode
        mode = toggle_mode(mode)
        _say(robot, "Multiplayer mode." if mode == "multiplayer" else "Solo mode.")

def play_sleepy_vector(serial=None):
    import anki_vector
    rng = random.Random()
    print("Connecting to Vector...")
    with anki_vector.AsyncRobot(serial=serial) as robot:
        print("Connected.\n")
        anims = _resolve_anims(robot)
       	_say(robot, "Give me a moment to get ready.")
        print("Preloading clip list (long timeout, be patient)...")
        _preload_clip_list(robot)
        _say(robot, "Okay, I am ready!")
        _drive_off_charger_if_needed(robot)
        _say(robot, "Pet me to change mode.")
        mode = _select_mode(robot)
        print(f"Mode locked: {mode}")
        threshold = choose_threshold(rng, WAKE_MIN, WAKE_MAX)
        print(f"(secret wake threshold: {threshold} pets)")
        _say(robot, "Okay, going to sleep now. Shhh.")
        _play(robot, anims, "sleep_getin", wait=True)
        pet_count = 0
        try:
            while True:
                # hold the sleep pose (no sleeping-loop replay — it moves head/lift);
                # just wait for the next pet
                if not _wait_for_pet(robot, timeout=None):
                    continue
                pet_count += 1
                print(f"Pet #{pet_count}")
                if is_wake_pet(pet_count, threshold):
                    _play_clip(robot, SHOCK_CLIP)   # shocked jolt awake
                    _play_clip(robot, ANGRY_CLIP)   # then furious
                    _say(robot, rng.choice(SCOLD_LINES))
                    time.sleep(0.3)
                    _say(robot, end_message(mode, pet_count))
                    break
                else:
                    _stir(robot)                    # physical stir only
            print(f"\nGame over. Woke at pet #{pet_count} (threshold was {threshold}).\n")
        except KeyboardInterrupt:
            print("\nInterrupted — cleaning up.")
    return 0

def main():
    parser = argparse.ArgumentParser(description="Sleepy Vector game.")
    parser.add_argument("--selftest", action="store_true", help="Logic self-test, no robot.")
    parser.add_argument("--serial", default=None, help="Vector serial.")
    args = parser.parse_args()
    if args.selftest:
        sys.exit(_run_selftest())
    sys.exit(play_sleepy_vector(serial=args.serial))

if __name__ == "__main__":
    main()
