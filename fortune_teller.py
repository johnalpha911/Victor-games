#!/usr/bin/env python3

import argparse
import random
import time
import threading
import traceback

import anki_vector
from anki_vector import lights
from anki_vector.messaging import protocol


# ============================================================
# FORTUNES
# ============================================================

GOOD_FORTUNES = [
    "A great opportunity is approaching.",
    "Good luck is following you.",
    "A pleasant surprise is coming soon.",
    "Your future looks bright.",
    "A new adventure awaits you.",
    "Your hard work will pay off.",
    "The love of your life will soon appear"
]


NEUTRAL_FORTUNES = [
    "The future is still being written.",
    "Many paths are possible.",
    "The spirits are unsure.",
    "Your choices will shape your destiny.",
    "The future remains a mystery."
]


BAD_FORTUNES = [
    "A small inconvenience is approaching.",
    "Your luck will soon be tested.",
    "Something annoying may happen.",
    "A problem may appear unexpectedly."
]


VERY_BAD_FORTUNES = [
    "Your battery will reach one percent at the worst time.",
    "The universe has scheduled an inconvenience.",
    "A tiny disaster is on the schedule.",
    "Google said, you will die."
]


ABSURD_FORTUNES = [
    "A confused duck will influence your destiny.",
    "A mysterious potato is watching your journey.",
    "A chair will become important soon."
]



# ============================================================
# ANIMATIONS
# ============================================================

ANIMATIONS = {

    "connect_getin":
        "ConnectToCubeGetIn",

    "connect_loop":
        "ConnectToCubeLoop",

    "connect_success":
        "ConnectToCubeSuccess",

    "connect_failure":
        "ConnectToCubeFailure",


    "search_getin":
        "KnowledgeGraphSearchingGetIn",

    "search_loop":
        "KnowledgeGraphSearching",

    "search_getout":
        "KnowledgeGraphSearchingGetOutSuccess"
}



ANIM_TIMEOUT = 20



def wait_future(result, timeout=ANIM_TIMEOUT):

    if hasattr(result, "result"):

        try:
            result.result(timeout=timeout)

        except Exception as e:

            print("[FUTURE ERROR]", e)

CLIP_LIST_TIMEOUT_S = 120


def preload_clip_list(robot, timeout_s=CLIP_LIST_TIMEOUT_S):

    print("[ANIM] Preloading raw clip list...")

    async def _load():

        req = protocol.ListAnimationsRequest()

        result = await robot.anim.grpc_interface.ListAnimations(
            req,
            timeout=timeout_s
        )

        robot.anim._anim_dict = {
            a.name: a for a in result.animation_names
        }

        return len(result.animation_names)


    try:

        count = robot.conn.run_coroutine(_load()).result()

        print(
            "[ANIM] Preloaded clips:",
            count
        )

        return True


    except Exception:

        print("[ANIM] Clip preload failed")

        traceback.print_exc()

        return False



# ============================================================
# ANIMATION CACHE
# ============================================================

def preload_animations(robot):

    print("[ANIM] Loading trigger list...")


    try:

        wait_future(
            robot.anim.load_animation_trigger_list()
        )


        print(
            "[ANIM] Trigger list loaded:",
            len(robot.anim.anim_trigger_list)
        )


    except Exception:

        traceback.print_exc()



    preload_clip_list(robot)



def play_anim(robot, key, wait=True):

    name = ANIMATIONS[key]

    print("[ANIM PLAY]", name)


    try:

        trigger = None


        try:

            trigger = (
                robot.anim
                ._anim_trigger_dict
                .get(name)
            )


        except Exception:

            pass



        result = robot.anim.play_animation_trigger(
            trigger if trigger else name
        )


        if wait:

            wait_future(result)



    except Exception:

        print("[ANIM FAILED]", name)
        traceback.print_exc()
# ============================================================
# CUBE CONNECTION
# ============================================================

def cube_status(cube):

    if cube is None:
        return "None"

    try:
        return (
            f"connected={cube.is_connected}, "
            f"visible={cube.is_visible}, "
            f"tap={cube.last_tapped_time}"
        )

    except Exception:
        return "unknown"



def connect_cube(robot):

    print("[CUBE] Starting connection")


    play_anim(
        robot,
        "connect_getin"
    )


    try:

        future = robot.world.connect_cube()

        if hasattr(future, "result"):
            future.result(timeout=10)


    except Exception as e:

        print(
            "[CUBE CONNECT ERROR]",
            e
        )



    timeout = time.time() + 20


    while time.time() < timeout:


        cube = robot.world.connected_light_cube


        print(
            "[CUBE]",
            cube_status(cube)
        )


        if cube is not None:

            print("[CUBE] Connected")


            play_anim(
                robot,
                "connect_success"
            )


            return cube



        play_anim(
            robot,
            "connect_loop"
        )


        time.sleep(0.5)



    print("[CUBE] Connection failed")

    play_anim(
              robot,
              "connect_failure"
)

    say(
        robot,
        "I cannot find my magic cube. Check the battery and try again"
)

    return None




# ============================================================
# CUBE LIGHT CONTROL
# ============================================================


def set_cube_color(cube, colour):

    print(
        "[LIGHT SET]",
        colour
    )


    try:

        if colour == "red":

            cube.set_lights(
                lights.red_light
            )


        elif colour == "green":

            cube.set_lights(
                lights.green_light
            )


        elif colour == "blue":

            cube.set_lights(
                lights.blue_light
            )


        elif colour == "off":

            cube.set_lights_off()


    except Exception:

        traceback.print_exc()




# ============================================================
# SEARCHING RGB LIGHT EFFECT
# ============================================================


def searching_lights(cube, stop_event):

    print("[LIGHT] Searching effect started")


    colours = [

        lights.red_light,
        lights.blue_light,
        lights.green_light

    ]


    index = 0


    while not stop_event.is_set():


        try:

            cube.set_lights(
                colours[index]
            )


        except Exception:

            traceback.print_exc()



        index += 1


        if index >= len(colours):
            index = 0



        time.sleep(0.25)



    print("[LIGHT] Searching effect stopped")




# ============================================================
# ANGRY RED FLASH
# ============================================================


def angry_lights(cube, stop_event):

    print("[LIGHT] Angry flash started")


    while not stop_event.is_set():


        try:

            cube.set_lights(
                lights.red_light
            )

            time.sleep(0.15)


            cube.set_lights_off()

            time.sleep(0.15)


        except Exception:

            traceback.print_exc()



    print("[LIGHT] Angry flash stopped")
# ============================================================
# TAP SYSTEM
# ============================================================

def tap_time(cube):

    try:
        return cube.last_tapped_time or 0

    except Exception:
        return 0



def wait_for_first_tap(cube, robot):

    print("[TAP] Waiting for first tap")

    last = tap_time(cube)

    reminder_time = time.time() + 8

    reminders = [
        "The spirits are growing impatient.",
        "Place your hand upon the magic cube.",
        "The spirits await your touch.",
        "Do not keep the spirits waiting.",
    ]

    reminder_index = 0

    while True:

        current = tap_time(cube)

        if current > last:
            print("[TAP] First tap")
            return current

        if time.time() >= reminder_time:

            say(robot, reminders[reminder_index])

            reminder_index = (reminder_index + 1) % len(reminders)

            reminder_time = time.time() + 8

        time.sleep(0.05)




def wait_for_extra_taps(cube, duration=8):

    print("[TAP] Extra tap window")


    start = time.time()

    last = tap_time(cube)

    taps = 0

    reminded = False



    while time.time() - start < duration:


        current = tap_time(cube)


        if current > last:

            taps += 1

            last = current


            print(
                "[TAP] Extra:",
                taps
            )


            if taps >= 5:

                return taps



        if (
            not reminded
            and time.time() - start > 4
        ):

            reminded = True

            print(
                "[TAP] Reminder needed"
            )


        time.sleep(0.05)



    return taps




# ============================================================
# SEARCHING SEQUENCE
# ============================================================

def run_search(robot, cube):


    print("[SEARCH] Starting")


    set_cube_color(
        cube,
        "blue"
    )



    # Enter animation

    play_anim(
        robot,
        "search_getin"
    )



    stop_anim = threading.Event()

    stop_lights = threading.Event()



    anim_thread = threading.Thread(
        target=search_animation_loop,
        args=(robot, stop_anim),
        daemon=True
    )



    light_thread = threading.Thread(
        target=searching_lights,
        args=(cube, stop_lights),
        daemon=True
    )



    anim_thread.start()

    light_thread.start()



    # Placeholder answer calculation time
    # This is where the "spirits search"

    time.sleep(6)



    stop_anim.set()

    stop_lights.set()



    anim_thread.join(
        timeout=3
    )

    light_thread.join(
        timeout=3
    )



    print("[SEARCH] Found")


    play_anim(
        robot,
        "search_getout"
    )



    return True





def search_animation_loop(robot, stop_event):

    print("[ANIM] Searching loop")


    while not stop_event.is_set():


        play_anim(
            robot,
            "search_loop",
            wait=True
        )


    print("[ANIM] Searching loop stopped")
# ============================================================
# SPEECH
# ============================================================

def say(robot, text):

    print("[SAY]", text)

    try:

        wait_future(
            robot.behavior.say_text(text)
        )

    except Exception:

        traceback.print_exc()



# ============================================================
# FORTUNE
# ============================================================

def choose_fortune():

    roll = random.randint(1,100)

    print("[FORTUNE ROLL]", roll)


    if roll <= 30:
        return random.choice(GOOD_FORTUNES), "good"

    elif roll <= 60:
        return random.choice(NEUTRAL_FORTUNES), "neutral"

    elif roll <= 85:
        return random.choice(BAD_FORTUNES), "bad"

    elif roll <= 95:
        return random.choice(VERY_BAD_FORTUNES), "very_bad"

    else:
        return random.choice(ABSURD_FORTUNES), "absurd"




# ============================================================
# POSSESSED MOTION
# ============================================================

def possessed_motion(robot):

    print("[POSSESSED] Starting fast motor movement")

    try:

        for i in range(30):

            print("[POSSESSED] SNAP UP", i)

            robot.motors.set_head_motor(5.0)
            robot.motors.set_lift_motor(5.0)

            time.sleep(0.15)


            print("[POSSESSED] SNAP DOWN")

            robot.motors.set_head_motor(-5.0)
            robot.motors.set_lift_motor(-5.0)

            time.sleep(0.15)


        print("[POSSESSED] Finished")


    except Exception:

        print("[POSSESSED ERROR]")
        traceback.print_exc()


    finally:

        try:

            robot.motors.stop_all_motors()

            print("[MOTORS] stopped")


        except Exception:

            traceback.print_exc()




# ============================================================
# ANGRY SPIRITS
# ============================================================

def angry_spirits(robot, cube):


    say(
        robot,
        "You have angered the spirits."
    )


    stop = threading.Event()


    light_thread = threading.Thread(
        target=angry_lights,
        args=(cube, stop),
        daemon=True
    )


    light_thread.start()



    possessed_motion(robot)



    stop.set()


    light_thread.join(
        timeout=2
    )


    set_cube_color(
        cube,
        "green"
    )


    say(
        robot,
        "What have you done?"
    )





# ============================================================
# MAIN GAME
# ============================================================

def fortune_teller(robot):

    cube = connect_cube(robot)

    if cube is None:
        return



    say(
        robot,
        "Place your hand on my magic cube."
    )


    time.sleep(1)



    say(
        robot,
        "Tap once when you are ready."
    )



    wait_for_first_tap(cube, robot)



    time.sleep(1)



    say(
        robot,
        "Do not tap too many times. The spirits are sensitive."
    )



    taps = wait_for_extra_taps(
        cube,
        8
    )



    if taps >= 5:

        angry_spirits(
            robot,
            cube
        )

        return



    if taps <= 4:

        say(
            robot,
            "I know you want your answer quickly, but wait."
        )



    run_search(
        robot,
        cube
    )



    time.sleep(2)



    fortune, category = choose_fortune()


    print(
        "[CATEGORY]",
        category
    )


    if category == "good":

        colour = "green"

    elif category in (
        "bad",
        "very_bad"
    ):

        colour = "red"

    else:

        colour = "blue"



    set_cube_color(
        cube,
        colour
    )



    say(
        robot,
        fortune
    )


    time.sleep(3)


    set_cube_color(
        cube,
        "off"
    )





# ============================================================
# STARTUP
# ============================================================

def main():


    parser = argparse.ArgumentParser(
        description="Vector Fortune Teller"
    )


    parser.add_argument(
        "--serial",
        default=None
    )


    args = parser.parse_args()



    try:

        with anki_vector.AsyncRobot(
            serial=args.serial
        ) as robot:


            print(
                "[ROBOT] Connected"
            )


            preload_animations(
                robot
            )


            fortune_teller(
                robot
            )



    except Exception:

        print(
            "[FATAL]"
        )

        traceback.print_exc()



if __name__ == "__main__":

    main()
