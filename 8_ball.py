#!/usr/bin/env python3

import argparse
import random
import time
import traceback

import anki_vector
from anki_vector.util import degrees


# ============================================================
# SETTINGS
# ============================================================

QUESTION_WAIT = 5.0
PET_MIN_HOLD = 0.2
PET_MAX_HOLD = 5.0
ROLL_SECONDS = 5.0
ANIM_TIMEOUT = 20


# ============================================================
# ANSWERS
# ============================================================

ANSWERS = [
    "It is certain.",
    "Without a doubt.",
    "Yes, definitely.",
    "The spirits say yes.",
    "Ask again later.",
    "The future is unclear.",
    "Maybe.",
    "The spirits are unsure.",
    "My answer is no.",
    "Very doubtful.",
]

EASTER_EGGS = [
    "Ask mom.",
]


# ============================================================
# ANIMATIONS
# ============================================================

ANIMATIONS = {
    "shake_loop": "ReactToShake_Lvl3Loop",
    "shake_waiting": "ReactToShake_Lvl3Waiting",
    "shake_ground": "ReactToShake_Lvl3OnGround",
}


# ============================================================
# HELPERS
# ============================================================

def wait_future(result, timeout=ANIM_TIMEOUT):
    if hasattr(result, "result"):
        try:
            result.result(timeout=timeout)
        except Exception as e:
            print("[FUTURE ERROR]", e)


def preload_animations(robot):
    print("[ANIM] Loading trigger list...")

    try:
        wait_future(robot.anim.load_animation_trigger_list())
        print("[ANIM] Trigger list loaded:", len(robot.anim.anim_trigger_list))
    except Exception:
        print("[ANIM LOAD ERROR]")
        traceback.print_exc()


def play_anim(robot, key, wait=True):
    name = ANIMATIONS[key]
    print("[ANIM PLAY]", name)

    try:
        trigger = None
        try:
            trigger = robot.anim._anim_trigger_dict.get(name)
        except Exception:
            trigger = None

        if trigger is None:
            print("[ANIM MISSING]", name)
            return

        result = robot.anim.play_animation_trigger(trigger)

        if wait:
            wait_future(result)

    except Exception:
        print("[ANIM FAILED]", name)
        traceback.print_exc()


def say(robot, text):
    print("[SAY]", text)

    try:
        wait_future(robot.behavior.say_text(text), timeout=15)
    except Exception:
        traceback.print_exc()


def get_touching(robot):
    try:
        reading = robot.touch.last_sensor_reading
        return reading is not None and reading.is_being_touched
    except Exception:
        return False


def wait_for_pet(robot, min_hold=PET_MIN_HOLD, max_hold=PET_MAX_HOLD):
    print("[PET] Waiting for back touch")

    while True:
        while not get_touching(robot):
            time.sleep(0.05)

        start = time.monotonic()

        while get_touching(robot):
            held = time.monotonic() - start
            if held >= max_hold:
                print("[PET] Held too long; ignoring")
                while get_touching(robot):
                    time.sleep(0.05)
                break
            time.sleep(0.05)
        else:
            held = time.monotonic() - start
            print("[PET] Held for", round(held, 2), "seconds")
            if min_hold <= held < max_hold:
                return held

        time.sleep(0.1)


def choose_answer():
    if random.randint(1, 20) == 1:
        return "Ask mom."
    return random.choice(ANSWERS)


# ============================================================
# ROLLING MODES
# ============================================================

def shake_head(robot):
    print("[8BALL] Starting rapid head shake")

    try:
        for i in range(30):
            print("[8BALL] HEAD UP", i)
            robot.motors.set_head_motor(5.0)
            time.sleep(0.10)

            print("[8BALL] HEAD DOWN")
            robot.motors.set_head_motor(-5.0)
            time.sleep(0.10)

        print("[8BALL] Finished")

    except Exception:
        print("[8BALL ERROR]")
        traceback.print_exc()

    finally:
        try:
            robot.motors.stop_all_motors()
            print("[MOTORS] stopped")
        except Exception:
            traceback.print_exc()

        try:
            robot.behavior.set_head_angle(degrees(0))
        except Exception:
            traceback.print_exc()


def safe_roll(robot):
    print("[8BALL] Safe mode roll")

    start = time.time()

    while time.time() - start < ROLL_SECONDS:
        play_anim(robot, "shake_loop", wait=True)

    print("[8BALL] Safe roll finished")


def finish_roll(robot):
    play_anim(robot, "shake_waiting", wait=True)
    play_anim(robot, "shake_ground", wait=True)


# ============================================================
# MAIN GAME
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Vector Magic 8 Ball")
    parser.add_argument("--serial", default=None)
    parser.add_argument(
        "--safemode",
        action="store_true",
        help="Use Vector animations instead of motor shaking"
    )
    args = parser.parse_args()

    try:
        with anki_vector.AsyncRobot(
            serial=args.serial,
            cache_animation_lists=False,
            behavior_activation_timeout=30,
        ) as robot:
            print("[ROBOT] Connected")

            preload_animations(robot)

            say(robot, "What is your question?")
            time.sleep(QUESTION_WAIT)

            say(robot, "Pet me to roll.")
            wait_for_pet(robot, min_hold=PET_MIN_HOLD, max_hold=PET_MAX_HOLD)

            if args.safemode:
                safe_roll(robot)
            else:
                shake_head(robot)

            finish_roll(robot)

            answer = choose_answer()
            say(robot, answer)

            time.sleep(1)
            print("[DONE] Magic 8 Ball finished")

    except Exception as e:
        print("[FATAL]", type(e).__name__, str(e))
        traceback.print_exc()


if __name__ == "__main__":
    main()
