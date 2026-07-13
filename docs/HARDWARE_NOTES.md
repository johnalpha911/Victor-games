# Hardware notes

Everything below was measured on a real Vector, served
by WirePod on a Raspberry Pi 3B+, over the course of building the games in
this repo. Some of it is touched on in the official
SDK docs or scattered across forum threads and wikis, but we couldn't find
most of it written down anywhere in one place — it was mainly arrived at
by probing the robot directly and watching what actually happened, often
after an assumption that seemed reasonable turned out to be wrong.

If you're building your own Vector project and something in here saves you
a day, that's exactly what it's for.

## Animations: two namespaces, and a fetch that times out

Covered in the README, but the short version bears repeating here because
it's the single most common failure mode across every game in this repo.

**The trigger/clip split itself is documented by Anki, not something we
found by probing.** Their own SDK docs describe both methods and
explicitly recommend triggers over clips, precisely because clip names
can be renamed or removed between firmware versions:

> "We advise you to use `play_animation_trigger` instead of
> `play_animation`, since individual animations can be deleted between
> Vector OS versions."
> — [`anki_vector.animation` docs](https://developer.anki.com/vector/docs/generated/anki_vector.animation.html)

- **Triggers** are CamelCase (`ConnectToCubeGetIn`) and play with
  `play_animation_trigger()`. Vector may pick from several underlying
  animations for the same trigger.
- **Clips** are lowercase `anim_*` (`anim_keepaway_pounce_04`) and play
  with `play_animation()` — one exact animation, no substitution.

**What we did have to find out ourselves is what happens when it times
out.** Passing a bare **string** to either method makes the SDK call
`_ensure_loaded()` internally, which — if the corresponding list hasn't
already been loaded — fetches the *entire* list of that type before the
animation can play. This is genuine SDK behaviour, confirmed directly in
[Anki's own source](https://github.com/anki/vector-python-sdk/blob/master/anki_vector/animation.py):

```python
async def play_animation_trigger(self, anim_trigger, ...):
    animation_trigger = anim_trigger
    if not isinstance(anim_trigger, protocol.AnimationTrigger):
        await self._ensure_loaded()          # <- the fetch we're avoiding
        if anim_trigger not in self.anim_trigger_list:
            raise exceptions.VectorException(...)
        animation_trigger = self._anim_trigger_dict[anim_trigger]
    ...
```

Pass the object instead of a string and that `isinstance` check is true,
so `_ensure_loaded()` is skipped entirely.

The trigger list is small and loads fast. The raw clip list — 589 entries
on this Vector — does not; on a Pi 3B+ it can time out even given as long
as 120 seconds, and **this part isn't just us** — it's a long-standing,
independently reported problem on Anki's own SDK repo:
[`List animations frequently causes a timeout` — Issue #51](https://github.com/anki/vector-python-sdk/issues/51).
When the fetch times out, the animation call doesn't raise an exception up
through the wrapper we were using — it just silently never plays. This
cost the most debugging time of anything in this whole project, because
everything else about the call looked correct.

**The fix:** construct the object directly, which is also the pattern
Anki's own connection-level docs use for exactly this reason:

```python
from anki_vector.messaging import protocol
robot.anim.play_animation(protocol.Animation(name="anim_keepaway_pounce_04"))
```

For triggers, load the trigger list once at startup
(`robot.anim.load_animation_trigger_list()` — cheap, fast, safe every
time), then look the object up in `robot.anim._anim_trigger_dict` and pass
that instead of the bare name. If the dict lookup misses for some reason,
falling back to the string is safe for *triggers specifically*, since it's
only the raw clip list that's the problem — the trigger fetch it would
retrigger is the cheap one.

**A caveat we hit once and never fully resolved:** the 589-clip list we
built by walking `all_anim_clips.txt` turned out to be *incomplete* — a
clip a user had verified working on hardware (`anim_rtpickup_putdown_03`)
wasn't in that list at all. Treat any such list as useful for finding
candidate names, not as authoritative for ruling a clip *out*.

## The front ToF sensor floors at 30mm

Vector's proximity sensor cannot report a distance closer than
approximately 30mm — verified by sliding a cube in by hand while polling
continuously (see the pattern in `feeding.py`'s approach logic) and
watching the reading flatten out and refuse to go lower, however close the
object actually got.

Practically: any behaviour that needs Vector to close the final gap onto
an object (nesting his lift onto a cube, for instance) cannot rely on the
sensor for that last stretch. It has to be driven **blind**, on dead
reckoning, for a fixed short distance past wherever the sensor bottoms
out.

## The camera can only identify the cube at a distance

The flip side of the above: `cube.is_visible` (which depends on Vector's
camera recognising the cube's marker) only works when the cube is far
enough away that the marker fits cleanly in frame. Up close, the marker
falls out of frame edges and `is_visible` never goes true — a cube placed
right in front of him can go completely unrecognised.

This mattered because we initially used the camera to confirm "is this
really the cube, not a hand or a wall" before committing to an approach
driven by the ToF. That's the right idea, but if the cube starts out too
close, the confirmation step will simply hang until it times out. The
robust version: if something is within the camera's blind range and still
unconfirmed after a few seconds, back up until it's far enough away to
identify, then look again.

The two sensors are genuinely complementary, not redundant — camera for
*identity* at range, ToF for *proximity* up close — but neither covers the
full distance range on its own.

`cube.is_visible` also flickers in normal use and shouldn't be treated as
a stable continuous signal; poll it, don't assume one true reading means
it'll stay true next frame.

## Hand-moving the lift only gets you a few millimetres — that's firmware, not gearing

It's tempting to assume the lift resists being moved by hand because it's
mechanically geared, but that's not what's actually going on: the
resistance is the firmware limiting how far the arm can be displaced by an
outside force, not a physical gear ratio stopping your hand. In practice
you can nudge it a small number of millimetres before it holds firm, and
it doesn't stay wherever you push it to.

This matters for any "human moves the lift as a gesture" design (used in
`hot_potato.py` and `vector_challenge.py`). The gesture can't be "raise it
and lower it" — it has to be "nudge it, at all, twice," with each nudge
detected as a small delta against a re-established baseline, not an
absolute position.

`lift_height_mm` itself is a precise, live encoder reading in general —
Anki built it for things like `set_lift_height()` and animation playback,
not with hand-nudging in mind, but it's accurate regardless of what's
moving the arm. It's specifically the *firmware's willingness to let a
human displace it by hand* that's small, not any limit on how precisely
the reading tracks that displacement. That precision is what makes the
nudge-based gesture detection work: even a few millimetres of movement
shows up cleanly in the number.

## `set_lift_height()` cannot deliver force; `set_lift_motor()` can

`set_lift_height()` (and `set_head_angle()`) are **position controllers**.
Given an obstruction, they plan a smooth trajectory towards the target and,
on meeting resistance, essentially give up gracefully rather than pushing
through. Cranking `accel`/`max_speed` barely changes this — going from 10
to 20 shaved barely a tenth of a second off an otherwise-identical motion.

`robot.motors.set_lift_motor(speed)` (and the equivalent for the head) is
**direct and open-loop**: positive is up, negative is down, in rad/s, and
it keeps applying torque continuously until you explicitly set it back to
zero. It has no target and no sense of "there" — which is exactly what
lets it keep pushing against an obstruction with real, continuous force,
but also means anywhere you need it to stop at a specific position, you
have to close the loop yourself: poll the live angle/height and cut the
motor the instant it crosses your target. Left uncorrected, an open-loop
return stroke will happily sail straight past where you meant it to stop.

### The wheelie

There is no animation clip anywhere on this Vector for a wheelie. It's
built entirely from `set_lift_motor()`: wind the lift up briefly, then
slam it down hard and hold. Because the lift can't physically go any
lower than the cube beneath it, the downward force has nowhere to go
except into the chassis — and the chassis lifts instead. Verified 7/7
successful attempts using the motor-driven version; the position-controller
version needed several failed attempts first, because a stall-based
"try harder" escalation had to kick in before it built enough force.

**The overpush and the wheelie are the same motion at different
intensities.** Discovered by accident: an "overpush" (lift jabbing down
into a cube to indicate something like disgust) driven at 10 rad/s was
forceful enough to tip the whole robot into a wheelie. Dialling it back to
roughly 3 rad/s keeps it a push rather than a flip. If you're building
something similar, that threshold — somewhere between roughly 3 and 10
rad/s on this chassis — is worth knowing about in advance rather than
discovering by watching your robot nearly topple.

**The wheelie needs a genuinely charged battery.** On a low charge it
silently fails to generate enough torque — no error, it simply doesn't
tip.

## Cube light writes over BLE are rate-limited

Somewhere around 5–7 writes per second is reliably safe (roughly a
0.14–0.18s step between updates). Push meaningfully faster than that and
writes visibly queue up and lag behind — you tell the cube to stop and it
keeps changing colour for a moment afterwards, because there's a backlog
still draining. If you see a light effect that won't stop when you expect
it to, check your write rate before assuming the stop logic itself is
broken.

`cube.set_light_corners()` also requires **`Light` objects**, not raw
`Color` objects — passing a raw `Color` fails completely silently, no
exception, the cube simply doesn't respond. This one is easy to get right
once you know it and maddening to debug if you don't.

## Animations leave the lift and head wherever they finished

Playing an animation clip or trigger that involves the lift or head does
not automatically release those motors afterwards — they can be left
actively held at whatever position the animation ended on.
`set_lift_motor(0)` alone does not clear that hold; you need
`robot.motors.stop_all_motors()` to genuinely let go of everything. This
matters anywhere you need a human to be able to move the arm right after
an animation has played — check it's actually released before waiting on
a gesture. (And remember the arm only ever accepts a small manual
displacement in the first place — see above.)

## The skill file isn't locked down on purpose

`keepaway.py` writes a small, plainly-readable JSON file
(`~/.vector_keepaway.json`) tracking Vector's learned skill level, and it
would take about ten seconds to hand-edit it to make him unbeatable or
trivial. That's deliberate, not an oversight: this runs on your own robot
from a script you already have full source access to, so there's no
meaningful adversary to defend against — anyone who could edit the file
could just as easily edit a constant in the script itself. Treat it as
save-game state, not a leaderboard.

## Reused, verified logic worth keeping if you build a new game

A few small patterns proved themselves solid enough across multiple games
that they're worth lifting wholesale into anything new:

- **Charging via shake or tap**, from `feeding.py`: progress accumulates
  only while genuine input is happening (time-based for shake, using
  `cube.is_moving`; event-based for tap, using `cube.last_tapped_time`) and
  *pauses* the instant the person stops. Simple, and it reads as
  responsive in a way a fixed-duration timer never quite does.
- **Baseline-delta gesture detection**, from `vector_challenge.py` via
  `hot_potato.py`: capture `lift_height_mm` or `head_angle_rad` at the
  start of a waiting period, then watch for the live reading to diverge
  from that baseline by more than a small threshold. Works well for both
  the head, and the lift (bearing in mind it only ever accepts a small
  displacement — see above).
- **Skippable narration**, from `keepaway.py`/`feeding.py`: speak
  instructions line by line, polling the back touch sensor between (and
  during) lines, and if a pet is detected, stop narrating immediately and
  *consume* the pet (wait for release) so it can't leak through and get
  misread as an in-game action a moment later.
