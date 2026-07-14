#!/usr/bin/env python3
"""
reaction_game.py — Vector cube "tap when they match" reaction game.

FLOW
  1. Drive off the charger if docked.
  2. Ask you to place the cube in front of him, then WATCH via camera until he
     actually sees the cube in front of him.
  3. Once seen, begin the Bluetooth connection: ConnectToCubeGetIn plays as the
     process starts, ConnectToCubeLoop repeats while connecting, then
     ConnectToCubeSuccess (connected) or ConnectToCubeFailure (+abort).
  4. "Game start."
  5. Each round: cube shows random DIFFERENT corner colors, then all four snap
     to the SAME color = GO. Tap the cube:
       * in time (<= REACTION_LIMIT s)      -> YOUR point,   cube flashes GREEN,
                                               Vector plays BlackJack_VictorBlackJackLose
       * too slow / no tap                  -> VECTOR's point, cube flashes RED,
                                               Vector plays BlackJack_VictorBlackJackWin
       * tap TOO EARLY (before match)       -> VECTOR's point (jumped the gun)
  6. Final effect (once), then he announces the score:
       * Vector wins the game  -> RED SPINNER  (one corner chases around, red)
       * Player wins the game  -> RAINBOW FLASH (all four flash rainbow together)
       * Tie -> treated as Vector's win (red spinner)

MODES (pet-cycle at the very start, like keepaway / sleepy vector):
  "First to 10" (default)  |  "Infinite"

  The BACK SENSOR IS A QUIT BUTTON IN INFINITE MODE ONLY. In first-to-N the game
  ends on score, so holding his back does nothing — the sensor isn't polled.
  Override the target with --win-score N.

RULES: after the mode is picked, Vector narrates the rules himself, and the LAST
  LINE DEPENDS ON THE MODE — in first-to-N he names the target; in infinite he
  explains how to quit, since that's the only way the game ever ends. Pet him at
  any point to skip the rest of the explanation.

SELF-TEST (no robot):  python3 reaction_game.py --selftest
"""

import argparse
import random
import sys
import time

# ============================================================================
# PURE GAME LOGIC (SDK-free, unit-tested via --selftest)
# ============================================================================


def judge_round(tapped, reaction_time, reaction_limit, tapped_too_early):
    if tapped_too_early:
        return "vector"
    if tapped and reaction_time <= reaction_limit:
        return "player"
    return "vector"


def session_winner(player_score, vector_score):
    return "player" if player_score > vector_score else "vector"


def game_over(player_score, vector_score, win_score):
    """First to win_score wins. In infinite mode (win_score None) the game only
    ends on a back-sensor hold."""
    if win_score is None:
        return False
    return player_score >= win_score or vector_score >= win_score


def points(n):
    """English pluralisation: 1 -> 'point', everything else (including 0) ->
    'points'. Stops Vector saying 'You got 1 points'."""
    return "1 point" if n == 1 else f"{n} points"


def toggle_mode(mode):
    return "infinite" if mode == "first_to" else "first_to"


def mode_name(mode, win_score):
    return f"First to {win_score}" if mode == "first_to" else "Infinite"


def end_line(player_score, vector_score):
    result = "I won" if session_winner(player_score, vector_score) == "vector" \
        else "I lost"
    return (f"You got {points(player_score)}. "
            f"I got {points(vector_score)}. {result}.")


def rules_lines(mode, win_score, reaction_limit=1.0):
    """The rules Vector narrates. The last line DEPENDS ON THE MODE — in
    first-to-N he says the target; in infinite he explains how to quit, since
    that's the only way the game ever ends."""
    lines = [
        "Watch my cube.",
        "When all four corners turn the same color, tap it.",
        "Tap fast. You only get one second.",
        "Do not tap early. If you tap before they match, I get the point.",
    ]
    if mode == "first_to":
        lines.append(f"First to {win_score} wins.")
    else:
        lines.append("This game goes forever. "
                     "Hold my back to stop, and I will count up the score.")
    return lines


def pick_trigger(available, candidates):
    s = set(available)
    for name in candidates:
        if name in s:
            return name
    return None


def different_corner_colors(palette, rng):
    while True:
        choice = [rng.choice(palette) for _ in range(4)]
        if len({id(c) for c in choice}) > 1:
            return choice


def hold_qualifies_as_stop(hold_duration, min_hold, max_hold):
    """A back-sensor hold ends the game if it lasted at least min_hold and no
    more than max_hold seconds (a deliberate 1-5s press-and-release)."""
    return min_hold <= hold_duration <= max_hold


def spinner_frame(step, num_corners, lit_color, off_color):
    """Return the 4 corner colors for one frame of a chasing 'spinner': exactly
    one corner lit (lit_color), the rest off_color, advancing by step."""
    idx = step % num_corners
    return [lit_color if i == idx else off_color for i in range(num_corners)]


# ============================================================================
# SELF-TEST
# ============================================================================


def _run_selftest():
    failures = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    print("Running reaction_game.py self-test (no robot required)...\n")

    check("judge in time -> player", judge_round(True, 0.5, 1.0, False) == "player")
    check("judge boundary -> player", judge_round(True, 1.0, 1.0, False) == "player")
    check("judge too slow -> vector", judge_round(True, 1.2, 1.0, False) == "vector")
    check("judge no tap -> vector", judge_round(False, 0.0, 1.0, False) == "vector")
    check("judge too early -> vector", judge_round(True, 0.3, 1.0, True) == "vector")

    check("session player ahead", session_winner(5, 3) == "player")
    check("session vector ahead", session_winner(2, 6) == "vector")
    check("session tie -> vector", session_winner(4, 4) == "vector")

    check("pick first-available", pick_trigger(["A", "B"], ["X", "B"]) == "B")
    check("pick none -> None", pick_trigger(["A"], ["X"]) is None)

    # hold timing
    check("hold 0.5s too short -> no stop", hold_qualifies_as_stop(0.5, 1.0, 5.0) is False)
    check("hold 1.0s -> stop", hold_qualifies_as_stop(1.0, 1.0, 5.0) is True)
    check("hold 3s -> stop", hold_qualifies_as_stop(3.0, 1.0, 5.0) is True)
    check("hold 5.0s boundary -> stop", hold_qualifies_as_stop(5.0, 1.0, 5.0) is True)
    check("hold 6s too long -> no stop", hold_qualifies_as_stop(6.0, 1.0, 5.0) is False)

    # spinner frames: exactly one lit, advances, wraps
    f0 = spinner_frame(0, 4, "R", "O")
    f1 = spinner_frame(1, 4, "R", "O")
    f4 = spinner_frame(4, 4, "R", "O")
    check("spinner frame0 lights corner 0 only", f0 == ["R", "O", "O", "O"])
    check("spinner frame1 lights corner 1 only", f1 == ["O", "R", "O", "O"])
    check("spinner wraps (frame4 == frame0)", f4 == f0)
    check("spinner always exactly one lit", all(
        spinner_frame(s, 4, "R", "O").count("R") == 1 for s in range(12)))

    # different_corner_colors never all-identical
    class C:
        pass
    palette = [C() for _ in range(6)]
    rng = random.Random(42)
    ok = all(len({id(c) for c in different_corner_colors(palette, rng)}) > 1 for _ in range(200))
    check("corner colors never all-identical (200x)", ok)

    # first-to-N termination
    check("game over: player hits 10", game_over(10, 3, 10) is True)
    check("game over: vector hits 10", game_over(4, 10, 10) is True)
    check("game not over at 9-9", game_over(9, 9, 10) is False)
    check("game not over at 0-0", game_over(0, 0, 10) is False)
    check("infinite never ends on score", game_over(99, 99, None) is False)

    # pluralisation: 1 singular, 0 and 2+ plural
    check("points(0) -> '0 points'", points(0) == "0 points")
    check("points(1) -> '1 point'", points(1) == "1 point")
    check("points(2) -> '2 points'", points(2) == "2 points")
    check("end line singular", "You got 1 point." in end_line(1, 10))
    check("end line zero", "You got 0 points." in end_line(0, 10))
    check("no '1 points' anywhere", "1 points" not in end_line(1, 1))
    check("end line win/lose", "I won" in end_line(3, 10)
          and "I lost" in end_line(10, 3))

    # mode toggle
    check("toggle first_to -> infinite", toggle_mode("first_to") == "infinite")
    check("toggle infinite -> first_to", toggle_mode("infinite") == "first_to")
    check("double toggle identity", toggle_mode(toggle_mode("first_to")) == "first_to")
    check("mode name first_to", mode_name("first_to", 10) == "First to 10")
    check("mode name infinite", mode_name("infinite", 10) == "Infinite")

    # rules are MODE-DEPENDENT: first_to names the target, infinite explains quit
    ft = rules_lines("first_to", 10)
    inf = rules_lines("infinite", 10)
    check("first_to rules name the target", any("First to 10" in ln for ln in ft))
    check("first_to rules never mention quitting",
          not any("stop" in ln.lower() for ln in ft))
    check("infinite rules explain the quit", any("Hold my back" in ln for ln in inf))
    check("infinite rules never name a target",
          not any("First to" in ln for ln in inf))
    check("both modes share the core rules", ft[:4] == inf[:4])

    print()
    if failures:
        print(f"SELF-TEST FAILED: {failures}")
        return 1
    print("ALL SELF-TESTS PASSED.")
    return 0


# ============================================================================
# HARDWARE-FACING GAME
# ============================================================================

REACTION_LIMIT = 1.0
MIN_WAIT_BEFORE_MATCH = 1.2
MAX_WAIT_BEFORE_MATCH = 3.5
FLASH_INTERVAL = 0.18
ROUND_FLASH_TIME = 0.9        # how long the per-round red/green cube flash lasts
ROUND_PAUSE = 0.5
ANIM_TIMEOUT_S = 20.0
POLL_S = 0.02

# scoring
WIN_SCORE = 10                # first-to-10 (default mode)

# mode select (pet-cycle at the very start, like keepaway / sleepy vector)
MODE_SETTLE_S = 3.0           # no pet for this long -> the mode locks in
MODE_PET_MAX_S = 5.0

# stop-hold window — INFINITE MODE ONLY. In first-to-N the game ends on score,
# so the back sensor is not a quit button and holding it does nothing.
STOP_MIN_HOLD_S = 1.0
STOP_MAX_HOLD_S = 5.0

# camera-detect-before-connect
CAMERA_WAIT_TIMEOUT_S = 30.0   # how long to wait to SEE the cube before giving up
CONNECT_LOOP_TIMEOUT_S = 15.0  # how long to keep trying the BT connection

# end-game effect timing
SPINNER_STEP_S = 0.08
SPINNER_DURATION_S = 3.0
RAINBOW_FLASH_TIME = 0.25
RAINBOW_CYCLES = 6

ANIM_CANDIDATES = {
    "drive_off_charger": ["DriveOffChargerStraight", "DriveStartHappy"],
    "connect_getin":     ["ConnectToCubeGetIn"],
    "connect_loop":      ["ConnectToCubeLoop"],
    "connect_success":   ["ConnectToCubeSuccess"],
    "connect_failure":   ["ConnectToCubeFailure"],
    "vector_win_round":  ["BlackJack_VictorBlackJackWin"],
    "vector_lose_round": ["BlackJack_VictorBlackJackLose"],
    "session_player_win": ["CubePounceLoseSession"],   # player won -> Vector lost the game
    "session_vector_win": ["CubePounceWinSession"],    # Vector won the game
}


def _wait_future(result, timeout=ANIM_TIMEOUT_S):
    if hasattr(result, "result"):
        try:
            result.result(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass


CLIP_LIST_TIMEOUT_S = 120


def _preload_clip_list(robot, timeout_s=CLIP_LIST_TIMEOUT_S):
    """Force-load the raw animation clip list with a long timeout. The SDK's
    default 10s load times out on a slow Pi, which caused the anim list to
    lazy-load MID-GAME and stall say_text (dropping point announcements)."""
    from anki_vector.messaging import protocol

    async def _load():
        req = protocol.ListAnimationsRequest()
        result = await robot.anim.grpc_interface.ListAnimations(req, timeout=timeout_s)
        robot.anim._anim_dict = {a.name: a for a in result.animation_names}
        return len(result.animation_names)

    for attempt in range(1, 4):
        try:
            count = robot.conn.run_coroutine(_load()).result()
            print(f"Preloaded {count} animation clips (attempt {attempt}).")
            if count > 0:
                return True
        except Exception as exc:  # noqa: BLE001
            print(f"  (clip list preload attempt {attempt} failed: {exc})")
        time.sleep(2)
    print("  WARNING: clip list never preloaded; animations may stall.")
    return False


def _resolve_anims(robot):
    # Preload BOTH lists up front. There are two separate lists: the trigger
    # list AND the raw anim list. If either lazy-loads mid-game it stalls calls
    # (including say_text), which was causing point announcements to be dropped.
    try:
        _wait_future(robot.anim.load_animation_trigger_list())
    except Exception as exc:  # noqa: BLE001
        print(f"  (couldn't force-load trigger list: {exc})")
    try:
        available = robot.anim.anim_trigger_list
    except Exception as exc:  # noqa: BLE001
        print(f"  (couldn't read trigger list: {exc})")
        available = []
    print(f"Vector reports {len(available)} animation triggers; resolving reactions...")
    resolved = {k: pick_trigger(available, c) for k, c in ANIM_CANDIDATES.items()}
    for k, v in resolved.items():
        print(f"    {k:>19} -> {v or '(none)'}")
    return resolved


def _play(robot, anims, key, wait=True):
    """Play a trigger by our logical key. Passes the resolved AnimationTrigger
    OBJECT (from the trigger dict) instead of a string, which makes the SDK
    skip _ensure_loaded() entirely — so it never tries to lazy-load the huge raw
    anim list (which times out on a slow Pi). Triggers only need the trigger
    list, which loads fine."""
    name = anims.get(key)
    if not name:
        return
    try:
        # look up the trigger OBJECT so play skips the raw-anim-list check
        trig_obj = None
        try:
            trig_obj = robot.anim._anim_trigger_dict.get(name)
        except Exception:  # noqa: BLE001
            trig_obj = None
        result = robot.anim.play_animation_trigger(trig_obj if trig_obj is not None else name)
        if wait:
            _wait_future(result)
    except Exception as exc:  # noqa: BLE001
        print(f"  (animation '{name}' [{key}] didn't play: {exc})")


def _play_async(robot, anims, key):
    """Fire an animation and return its future without waiting (for the loop)."""
    name = anims.get(key)
    if not name:
        return None
    try:
        return robot.anim.play_animation_trigger(name)
    except Exception as exc:  # noqa: BLE001
        print(f"  (animation '{name}' [{key}] didn't start: {exc})")
        return None


def _say(robot, text):
    try:
        _wait_future(robot.behavior.say_text(text))
    except Exception as exc:  # noqa: BLE001
        print(f"  (say_text failed: {exc})")


def _touch_held(robot):
    r = robot.touch.last_sensor_reading
    return r is not None and r.is_being_touched


def _check_stop_hold(robot):
    """If the back sensor is held for at least STOP_MIN_HOLD_S seconds, that's a
    stop. Fires as soon as the threshold is crossed (no need to release within a
    window), then waits for release so it doesn't double-trigger. Reliable.

    INFINITE MODE ONLY — callers must not call this in first-to-N, where the
    game ends on score and the back sensor is not a quit button."""
    if not _touch_held(robot):
        return False
    start = time.monotonic()
    while _touch_held(robot):
        if time.monotonic() - start >= STOP_MIN_HOLD_S:
            # qualified as a stop; wait for release so we don't re-fire
            while _touch_held(robot):
                time.sleep(0.05)
            return True
        time.sleep(0.03)
    return False  # released before reaching the minimum hold


def _wait_pet_release(robot, timeout=MODE_PET_MAX_S):
    """Consume a pet — wait for the finger to come off. Without this the same
    touch leaks into the next poll and reads as a second, phantom pet."""
    end = time.monotonic() + timeout
    while _touch_held(robot) and time.monotonic() < end:
        time.sleep(0.05)


def _say_skippable(robot, text):
    """Say one line while watching the back sensor. Returns True if the player
    petted during it (= skip the rest of the narration). The current line
    finishes speaking — audio can't be cut mid-word — but the rest is dropped."""
    try:
        fut = robot.behavior.say_text(text)
    except Exception:  # noqa: BLE001
        return _touch_held(robot)
    petted = False
    end = time.monotonic() + 15
    while time.monotonic() < end:
        if _touch_held(robot):
            petted = True
        if hasattr(fut, "done") and fut.done():
            break
        time.sleep(0.05)
    _wait_future(fut, timeout=5)
    return petted


def _narrate_rules(robot, mode, win_score):
    """Read the rules out, pet to skip. Mode-dependent: in infinite he explains
    how to quit instead of naming a target score."""
    for line in rules_lines(mode, win_score, REACTION_LIMIT):
        if _say_skippable(robot, line):
            _wait_pet_release(robot)     # CONSUME it, or it leaks into the game
            print("  (rules skipped)")
            return


def _select_mode(robot, win_score=WIN_SCORE):
    """Pet-cycle the mode, exactly like keepaway. Locks in after MODE_SETTLE_S
    with no pet. Default is first-to-N."""
    mode = "first_to"
    _say(robot, f"{mode_name(mode, win_score)}.")
    while True:
        end = time.monotonic() + MODE_SETTLE_S
        petted = False
        while time.monotonic() < end:
            if _touch_held(robot):
                _wait_pet_release(robot)
                petted = True
                break
            time.sleep(0.05)
        if not petted:
            return mode
        mode = toggle_mode(mode)
        _say(robot, f"{mode_name(mode, win_score)}.")


def _drive_off_charger_if_needed(robot, anims):
    try:
        on_charger = robot.status.is_on_charger
    except Exception:  # noqa: BLE001
        on_charger = False
    if not on_charger:
        return
    print("On charger — driving off first.")
    _play(robot, anims, "drive_off_charger", wait=True)
    try:
        _wait_future(robot.behavior.drive_off_charger())
    except Exception as exc:  # noqa: BLE001
        print(f"  (drive_off_charger had trouble: {exc})")


def _connect_with_animations(robot, anims):
    """Connect to the cube over Bluetooth FIRST (GetIn -> Loop -> Success/Failure),
    THEN verify Vector's camera can see it. This order matches the diagnostic that
    works on 1.6 Rebuild: on that firmware the cube's marker is only tracked in the
    world model AFTER the BLE connection is established, so we must connect before
    checking camera visibility (the old 'see it via camera first' order failed
    there with 'object not currently tracked by the world')."""
    from anki_vector.util import degrees

    # 1) Start the Bluetooth connection.
    print("Connecting to the cube over Bluetooth...")
    try:
        robot.world.connect_cube()
    except Exception as exc:  # noqa: BLE001
        print(f"  (connect_cube start failed: {exc})")

    # GetIn plays as the connection process starts.
    _play(robot, anims, "connect_getin", wait=True)

    # Loop the connecting animation until BLE-connected or timeout.
    cube = None
    deadline = time.monotonic() + CONNECT_LOOP_TIMEOUT_S
    while time.monotonic() < deadline:
        cube = robot.world.connected_light_cube
        if cube is not None:
            break
        _play(robot, anims, "connect_loop", wait=True)
    if cube is None:
        cube = robot.world.connected_light_cube

    if cube is None:
        print("Couldn't connect to the cube over Bluetooth.")
        _play(robot, anims, "connect_failure", wait=True)
        return None

    print("Cube connected over Bluetooth.")

    # 2) Now that it's BLE-connected, verify the camera can SEE it (this is the
    #    step that works on 1.6 Rebuild only in this order). Enable the camera
    #    feed and look down slightly so the cube is in view.
    try:
        robot.camera.init_camera_feed()
    except Exception:  # noqa: BLE001
        pass
    try:
        _wait_future(robot.behavior.set_head_angle(degrees(-5.0)))
    except Exception:  # noqa: BLE001
        pass

    print("Waiting to see the cube via camera...")
    deadline = time.monotonic() + CAMERA_WAIT_TIMEOUT_S
    seen = False
    while time.monotonic() < deadline:
        try:
            if cube.is_visible:
                seen = True
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)

    if not seen:
        print("Never saw the cube in front of me.")
        _play(robot, anims, "connect_failure", wait=True)
        return None

    print("Cube seen — ready to play.")
    _play(robot, anims, "connect_success", wait=True)
    return cube


def _flash_cube(cube, light, duration):
    """Solid flash of one color for `duration` seconds, then off."""
    try:
        cube.set_lights(light)
        time.sleep(duration)
        cube.set_lights_off()
    except Exception:  # noqa: BLE001
        pass


def _red_spinner(cube, lights_mod):
    """Red 'spinner': one corner lit red chasing around the four corners."""
    end = time.monotonic() + SPINNER_DURATION_S
    step = 0
    while time.monotonic() < end:
        frame = spinner_frame(step, 4, lights_mod.red_light, lights_mod.off_light)
        try:
            cube.set_light_corners(*frame)
        except Exception:  # noqa: BLE001
            pass
        step += 1
        time.sleep(SPINNER_STEP_S)
    try:
        cube.set_lights_off()
    except Exception:  # noqa: BLE001
        pass


def _rainbow_flash(cube, lights_mod):
    """All four corners flash together, cycling rainbow colors."""
    rainbow = [lights_mod.red_light, lights_mod.yellow_light, lights_mod.green_light,
               lights_mod.cyan_light, lights_mod.blue_light, lights_mod.magenta_light]
    for i in range(RAINBOW_CYCLES):
        col = rainbow[i % len(rainbow)]
        try:
            cube.set_lights(col)
            time.sleep(RAINBOW_FLASH_TIME)
            cube.set_lights_off()
            time.sleep(RAINBOW_FLASH_TIME / 2)
        except Exception:  # noqa: BLE001
            pass


def _ensure_cube_connected(robot, cube):
    """If the cube dropped its BT connection mid-game, try to reconnect so the
    game recovers instead of breaking. Returns the (possibly new) cube object."""
    try:
        if cube is not None and cube.is_connected:
            return cube
    except Exception:  # noqa: BLE001
        pass
    print("  (cube disconnected — attempting to reconnect...)")
    try:
        _wait_future(robot.world.connect_cube())
        newcube = robot.world.connected_light_cube
        if newcube is not None:
            print("  (cube reconnected)")
            return newcube
    except Exception as exc:  # noqa: BLE001
        print(f"  (cube reconnect failed: {exc})")
    return cube


def _run_round(robot, cube, anims, palette, rng, infinite):
    """One round. Returns 'player', 'vector', or 'quit'.

    `infinite` gates the quit-hold: in first-to-N the game ends on score, so the
    back sensor does nothing and holding it must NOT abort the round."""
    tap_marker = cube.last_tapped_time or 0.0
    wait_before = rng.uniform(MIN_WAIT_BEFORE_MATCH, MAX_WAIT_BEFORE_MATCH)
    phase_end = time.monotonic() + wait_before
    next_flash = 0.0

    # pre-match: mismatched colors; tapping now = too early
    while time.monotonic() < phase_end:
        if infinite and _check_stop_hold(robot):
            return "quit"
        now = time.monotonic()
        if now >= next_flash:
            try:
                cube.set_light_corners(*different_corner_colors(palette, rng))
            except Exception:  # noqa: BLE001
                pass
            next_flash = now + FLASH_INTERVAL
        if (cube.last_tapped_time or 0.0) > tap_marker:
            return "vector"  # too early
        time.sleep(POLL_S)

    # MATCH -> GO
    match_light = rng.choice(palette)
    try:
        cube.set_lights(match_light)
    except Exception:  # noqa: BLE001
        pass

    tap_marker = cube.last_tapped_time or 0.0
    start = time.monotonic()
    deadline = start + REACTION_LIMIT
    while time.monotonic() < deadline:
        if infinite and _check_stop_hold(robot):
            return "quit"
        if (cube.last_tapped_time or 0.0) > tap_marker:
            return "player" if (time.monotonic() - start) <= REACTION_LIMIT else "vector"
        time.sleep(POLL_S)
    return "vector"  # too slow


def play_reaction_game(serial=None, win_score=WIN_SCORE):
    import anki_vector
    from anki_vector import lights

    palette = [lights.red_light, lights.green_light, lights.blue_light,
               lights.yellow_light, lights.cyan_light, lights.magenta_light]
    rng = random.Random()

    print("Connecting to Vector...")
    with anki_vector.AsyncRobot(serial=serial, cache_animation_lists=False,
                                behavior_activation_timeout=30) as robot:
        print("Connected.\n")
        anims = _resolve_anims(robot)

        _drive_off_charger_if_needed(robot, anims)

        # --- MODE SELECT (pet-cycle, exactly like keepaway / sleepy vector) ---
        _say(robot, "Pet my back to change the mode.")
        mode = _select_mode(robot, win_score)
        infinite = (mode == "infinite")
        target = win_score
        win_score_or_none = None if infinite else target
        print(f"Mode locked: {mode_name(mode, target)}\n")

        # --- RULES (mode-dependent, pet to skip) ---
        _narrate_rules(robot, mode, target)

        _say(robot, "Please place my cube in front of me.")
        cube = _connect_with_animations(robot, anims)
        if cube is None:
            print("Couldn't connect to a cube — aborting.")
            return 1
        print("Cube connected. Game on!\n")

        _say(robot, "Game start.")

        player_score, vector_score = 0, 0
        round_index = 0

        try:
            while True:
                # The back sensor is a quit button in INFINITE ONLY. In
                # first-to-N the game ends on score, so we never poll it here.
                if infinite and _check_stop_hold(robot):
                    break

                cube = _ensure_cube_connected(robot, cube)
                round_index += 1
                result = _run_round(robot, cube, anims, palette, rng, infinite)

                if result == "quit":
                    break
                elif result == "player":
                    player_score += 1
                    print(f"Round {round_index}: You tapped in time! "
                          f"(You {player_score} / Vector {vector_score})")
                    _flash_cube(cube, lights.green_light, ROUND_FLASH_TIME)
                    _play(robot, anims, "vector_lose_round", wait=True)
                else:
                    vector_score += 1
                    print(f"Round {round_index}: Vector's point! "
                          f"(You {player_score} / Vector {vector_score})")
                    _flash_cube(cube, lights.red_light, ROUND_FLASH_TIME)
                    _play(robot, anims, "vector_win_round", wait=True)

                if game_over(player_score, vector_score, win_score_or_none):
                    break

                time.sleep(ROUND_PAUSE)

        except KeyboardInterrupt:
            print("\nInterrupted.")

        winner = session_winner(player_score, vector_score)
        print(f"\nFinal — You {player_score} | Vector {vector_score} "
              f"-> {winner.upper()} wins the game.")

        # The GAME animation plays FIRST, then he announces the score — same
        # order as keepaway.
        if winner == "vector":
            _play(robot, anims, "session_vector_win", wait=True)
            _red_spinner(cube, lights)
        else:
            _play(robot, anims, "session_player_win", wait=True)
            _rainbow_flash(cube, lights)

        _say(robot, end_line(player_score, vector_score))

        try:
            cube.set_lights_off()
        except Exception:  # noqa: BLE001
            pass

    return 0


def main():
    parser = argparse.ArgumentParser(description="Vector cube reaction game")
    parser.add_argument("--selftest", action="store_true",
                        help="Offline logic self-test, no robot needed")
    parser.add_argument("--serial", default=None)
    parser.add_argument("--win-score", type=int, default=WIN_SCORE,
                        help=f"points needed to win first-to-N mode "
                             f"(default {WIN_SCORE})")
    args = parser.parse_args()

    if args.selftest:
        return _run_selftest()

    try:
        return play_reaction_game(serial=args.serial, win_score=args.win_score)
    except Exception:  # noqa: BLE001
        import traceback
        print("[FATAL]")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
