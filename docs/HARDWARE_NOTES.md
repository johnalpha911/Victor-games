# Hardware notes

Everything below was measured on a real Vector, served by WirePod on a
Raspberry Pi 3B+, over the course of building the games in this repo — it
was arrived at by probing the robot directly and watching what actually
happened, often after an assumption that seemed reasonable turned out to
be wrong.

After the fact, every claim here was **cross-checked against Randall Maas's
[Anki Vector Technical Reference Manual](https://randym32.github.io/Vector-TRM.pdf)**
(2021-02-14, 543 pages) — an extraordinary reverse-engineering effort, known
in the community as the "Vector Bible," that documents Vector's electronics,
firmware, and protocols in far more depth than anything Anki published. There's
a companion wiki at
[Anki.Vector.Documentation](https://randym32.github.io/Anki.Vector.Documentation/).

Where the TRM confirms a finding, it's noted. Where it *corrected* us, that's
noted too, because a couple of things we were confident about turned out to be
wrong. If you're doing serious Vector work, read the TRM first — it would have
saved us most of a day.

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

## The front ToF sensor floors at 30mm — and the lift can block it

Vector's proximity sensor cannot report a distance closer than
approximately 30mm — verified by sliding a cube in by hand while polling
continuously and watching the reading flatten out and refuse to go lower,
however close the object actually got.

**The TRM confirms this exactly, and explains why.** The sensor is an
STMicroelectronics **VL53L0x**, and per the TRM it "has a usable range 30mm
to 1200mm away (max useful range closer to 300mm for Vector) with a field
of view of 25 degrees." The mechanism:

> "Items too close return the pulse faster than the sensor can measure."

So this is a genuine hardware floor, not a firmware filter — the photons
come back before the timing circuit can resolve them. There is no
workaround.

Practically: any behaviour that needs Vector to close the final gap onto
an object (nesting his lift onto a cube, for instance) cannot rely on the
sensor for that last stretch. It has to be driven **blind**, on dead
reckoning, for a fixed short distance past wherever the sensor bottoms
out.

### `is_lift_in_fov` — the flag you should be checking

This one we missed entirely, and the TRM caught it. **The lift can block
the ToF sensor**, and the SDK tells you when it does. From `ProxData`:

| Field | Meaning |
|---|---|
| `distance_mm` | the distance to the object |
| `found_object` | the sensor detected something in valid range |
| `is_lift_in_fov` | **the lift (or a carried object) is blocking the sensor** |
| `signal_quality` | likelihood the reading is a real solid surface |
| `unobstructed` | nothing detected all the way to max range |

The TRM is explicit that this is not a hypothetical:

> "The sensor can be blocked by the arms, if they are in just the right
> lowered position — such as approaching an object and docking with it."

Which is *precisely* what `feeding.py` does: drive toward the cube with the
lift down, reading the ToF, then dock onto it. If your approach logic reads
`distance_mm` without checking `is_lift_in_fov`, you may be steering off a
reading of your own arm. The TRM's own advice is to track "the most recent
proximity data which did not have the lift blocking."

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

## Why the lift fights you when you move it by hand

Hand-move the lift and you get a few millimetres before it holds firm, and
it won't stay where you put it. We first assumed mechanical gearing, then
assumed a firmware limit on displacement. **Both were wrong**, and the TRM
has the real answer — it's the motor control loop actively resisting you.

Two things are happening at once:

**The PID controller fights back.** The TRM is blunt about it:

> "the PID controller violently fights your attempt to pull the lift,
> smacking your fingers and oscillating and otherwise causing trouble. The
> PID controller is pretty feisty, because it has to operate across a huge
> range of forces — between flipping or lifting the robot's entire weight
> and delicately setting down or lifting cubes without flinging them."

**And the stall protection never fully lets go.** When the body-board
detects a stall it throttles the current to protect the motor — but
crucially, it keeps pushing:

> "It never turns the power down to 0, since it doesn't have to. All 4
> motors can push continuously (gently) without stalling. So if you drive a
> motor toward the limit but someone is pulling on it the other way, it
> might push hard at first, then quickly 'relax' to a voltage that's safe
> for continuous use, but never stop pushing just in case you let go."

So the resistance you feel is a live control loop, not a mechanism. That
also explains why `stop_all_motors()` is what actually frees the arm (see
below) — you're not releasing a brake, you're cancelling a target.

**The motors *can* be genuinely unlocked.** This is the part we never
found: the TRM says the firmware supports it, and even uses it as an input
method.

> "The motors can also be 'unlocked' — allowed to be spun by external
> forces. This allows a person to raise and lower the lift, as well as raise
> and lower the head. Both of these are used as inputs to enter diagnostic
> modes. The software control loops can also detect when a person is playing
> with Vector's lift (or head or tracks), and then unlock the motors."

We never worked out how to trigger that unlock from the SDK, so
`hot_potato.py` works *with* the resistance rather than removing it: the
gesture is "nudge it, at all, twice" — each nudge a small delta against a
re-established baseline, never an absolute position, and never a
raise-and-hold. If you find the unlock, a much nicer gesture becomes
possible.

`lift_height_mm` itself is a precise, live encoder reading regardless of
what's moving the arm — that precision is what makes nudge detection work
at all, since even a couple of millimetres shows up cleanly.

## `set_lift_height()` cannot deliver force; `set_lift_motor()` can

`set_lift_height()` (and `set_head_angle()`) are **position controllers** —
confirmed by the TRM: "The lift and head motors are position-controlled. The
motors can be commanded to travel to an encoder position at a speed (given in
radians/sec)." Given an obstruction, they plan a smooth trajectory towards the
target and, on meeting resistance, essentially give up gracefully rather than
pushing through. Cranking `accel`/`max_speed` barely changes this — going from
10 to 20 shaved barely a tenth of a second off an otherwise-identical motion.

`robot.motors.set_lift_motor(speed)` (and the equivalent for the head) is
**direct and open-loop**: positive is up, negative is down, in rad/s, and
it keeps applying torque continuously until you explicitly set it back to
zero. It has no target and no sense of "there" — which is exactly what
lets it keep pushing against an obstruction with real, continuous force,
but also means anywhere you need it to stop at a specific position, you
have to close the loop yourself: poll the live angle/height and cut the
motor the instant it crosses your target. Left uncorrected, an open-loop
return stroke will happily sail straight past where you meant it to stop.

### Burn-out protection (and why you can stall a motor without fear)

The body-board watches the encoders on all four motors and throttles the duty
cycle on any channel that stalls. Per the TRM, you have a lot more headroom
than you'd think:

> "those motors can't overheat instantaneously — it takes at least 15 seconds
> of being stalled at full power before you risk permanent damage. The
> firmware in the body board watches the encoders on all 4 motors, and turns
> down the power on stalled channels."

So a brief hard stall — which is exactly what the wheelie and the overpush
are — is well within safe limits. Just don't hold one indefinitely.

### The wheelie

**Correction: Vector has a built-in wheelie, and we didn't know.** The TRM
documents `PopAWheelieRequest` as a first-class SDK behaviour — "Tell Vector
to 'pop a wheelie' using his cube. Vector will approach the cube, then push
down on it with his lift." It takes an approach angle, a motion profile, and
a retry count. Our note used to claim no wheelie existed anywhere on the
robot. That was simply wrong, and it's a good lesson in checking the
reference before declaring something absent.

There is still no wheelie *animation clip* — the built-in is a behaviour,
not a clip — and the mechanism we worked out from scratch turns out to be
exactly the one Anki used: push down on the cube with the lift.

**The hand-rolled version is still the right call for `feeding.py`,
though**, for one specific reason: `pop_a_wheelie()` makes Vector *approach
and dock with the cube himself* as part of the action. In feeding he's
already nested on the cube — an autonomous re-approach would be wrong, slow,
and visually jarring mid-animation. So we build it directly:

Wind the lift up briefly with `set_lift_motor()`, then slam it down hard and
hold. Because the lift can't go lower than the cube beneath it, the downward
force has nowhere to go except into the chassis — and the chassis lifts
instead. Verified 7/7 successful attempts with the motor-driven version; the
`set_lift_height()` version needed several failed attempts first, because
the stall-based torque escalation (see the burn-out protection note above)
had to kick in before it built enough force.

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
`robot.motors.stop_all_motors()` to genuinely let go of everything.

This is the same mechanism as the hand-resistance above: the motor isn't
"stuck," it's being *commanded* to a position, and the stall protection
throttles the power without ever releasing it — per the TRM, "This lets the
head and lift hold position." Setting motor speed to zero doesn't cancel a
position target; stopping all motors does.

It matters anywhere you need a human to be able to move the arm right after
an animation has played — check it's actually released before waiting on a
gesture.

## The serial number is an identifier, not a credential

Worth knowing if you're publishing Vector code, since every script here takes
a `--serial` argument and it's easy to leave a real one in an example.

Per the TRM, Vector's auth chain is: **account name/password → session token
(issued by Anki's server) → client token**, with the client token stored on
the robot at `/data/vic-gateway/token-hashes.json` and reusable indefinitely.
The real secrets are that client token plus the device certificate and private
key (`AnkiRobotDeviceCert.pem` / `AnkiRobotDeviceKeys.pem`).

The serial (ESN) is none of those. It's an identifier — it appears as the
certificate CommonName in the form `vic:<serial>`, also called the "thing id"
— and it's *printed on the underside of the robot*. It authenticates nothing
on its own.

So a leaked serial is closer to a leaked model number than a leaked password.
Still worth scrubbing from public examples out of basic hygiene (use
`YOUR_SERIAL`, or WirePod's `!botSerial` substitution), but it isn't the
emergency it feels like. The things that would genuinely matter are your certs,
keys, and `sdk_config.ini` — keep those out of git.

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
