import random

import time

import anki_vector

from anki_vector import behavior



BASE_REACTION_TIME = 5.0   # starting reaction time in seconds

MIN_REACTION_TIME = 1.5    # minimum reaction time

REACTION_DECREASE = 0.35    # decrease per round

LIFT_MOVE_THRESHOLD = 5     # mm

HEAD_MOVE_THRESHOLD = 0.0773  # radians (~5 degrees)



def detect_head_move(robot, start_angle):

    current_angle = robot.head_angle_rad

    return abs(current_angle - start_angle) > HEAD_MOVE_THRESHOLD



def detect_pet(robot):

    head_sensor = robot.touch.last_sensor_reading

    return head_sensor and head_sensor.is_being_touched



def detect_lift(robot, start_lift_height):

    current_height = robot.lift_height_mm

    return abs(current_height - start_lift_height) > LIFT_MOVE_THRESHOLD



def play_round(robot, reaction_time):

    command = random.choice(["head", "pet", "lift"])

    start_lift_height = robot.lift_height_mm

    start_head_angle = robot.head_angle_rad



    # Announce command

    if command == "head":

        robot.behavior.say_text("Move my head!")

    elif command == "pet":

        robot.behavior.say_text("Pet me!")

    else:

        robot.behavior.say_text("Move my lift!")



    start_time = time.time()

    success = False

    while time.time() - start_time < reaction_time:

        if command == "head" and detect_head_move(robot, start_head_angle):

            success = True

            break

        elif command == "pet" and detect_pet(robot):

            success = True

            break

        elif command == "lift" and detect_lift(robot, start_lift_height):

            success = True

            break

        time.sleep(0.05)



    if success:

        robot.behavior.say_text("Good job!")

        return True

    else:

        robot.behavior.say_text("Oops! Game over!")

        return False



def main():

    with anki_vector.Robot() as robot:

        # Pause roaming/idle behaviors for the entire game

        with behavior.ReserveBehaviorControl():

            robot.behavior.say_text("Vector Challenge! Follow my commands.")



            round_num = 0

            reaction_time = BASE_REACTION_TIME



            while True:

                round_num += 1

                success = play_round(robot, reaction_time)

                time.sleep(1.0)

                if not success:

                    break  # end game on first failure



                # Increase difficulty: decrease reaction time each round

                reaction_time = max(MIN_REACTION_TIME, reaction_time - REACTION_DECREASE)



            robot.behavior.say_text(f"You survived {round_num - 1} rounds!")



if __name__ == "__main__":

    main()
