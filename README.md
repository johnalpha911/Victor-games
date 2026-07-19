# Victor+ Games

A collection of games for Anki Vector, built against the Vector Python SDK
and run against a Vector served by WirePod — production firmware, or any
WirePod-compatible CFW. Part of the Victor+ project — this is the games
corner of it. (Victor+ itself isn't public yet; this repo stands on its
own in the meantime, and a link will go here once it is.)

No jailbreak or OSKR unlock needed — the SDK works on stock Vectors too.
WirePod comes into it because Anki's own cloud backend is gone, so a
self-hosted server is what actually lets Vector (and the SDK) work at all
today. See below.

Every game here talks to Vector directly over the SDK — no cloud, no app,
just Python on a machine that can reach your robot.

## The one thing worth knowing before you read any of this code

Vector's animations live in **two separate namespaces**, and mixing them up
is the single most common way to make an animation silently do nothing:

- **Triggers** — CamelCase (`ConnectToCubeGetIn`, `BlackJack_VictorBlackJackWin`).
  Played with `play_animation_trigger()`.
- **Clips** — lowercase, `anim_`-prefixed (`anim_keepaway_pounce_04`,
  `anim_launch_reacttoputdown`). Played with `play_animation()`.

Passing a bare **string** to either method makes the SDK call
`_ensure_loaded()` internally, which tries to fetch the *entire* list of
animations of that kind. On a Raspberry Pi 3B+ (and probably anything
similarly modest), that fetch can time out — and when it does, the
animation just never plays. No error, no exception. It just doesn't happen.

The fix used throughout this repo: **construct the object directly** and
pass that instead of a string.

```python
from anki_vector.messaging import protocol

# a CLIP — skips _ensure_loaded() entirely
robot.anim.play_animation(protocol.Animation(name="anim_keepaway_pounce_04"))

# a TRIGGER — look it up in the pre-loaded dict
robot.anim.load_animation_trigger_list()          # do this ONCE, at startup
trig = robot.anim._anim_trigger_dict.get("ConnectToCubeGetIn")
robot.anim.play_animation_trigger(trig)
```

The trigger *list* loads fine and fast — it's a few hundred entries. It's
the raw *clip* list (589 entries, at least on this Vector) that's the
problem. `load_animation_trigger_list()` is cheap and safe to call at
startup in every game; never call anything that fetches the full clip list
unless you're prepared to wait up to two minutes for it.

See [`docs/HARDWARE_NOTES.md`](docs/HARDWARE_NOTES.md) for the rest of what
we learned the hard way — sensor blind spots, motor control quirks, BLE
rate limits, and the physics behind a couple of things Vector does that
aren't in any official animation.

## Requirements

- A Vector robot served by WirePod — running on a Pi (or other machine)
  alongside it, not on Vector itself. Vector can be on stock production
  firmware or any WirePod-compatible CFW (e.g. 1.6 rebuild {my personal choice where everything was tested}); no
  OSKR/jailbreak is required either way. Vector's own firmware just needs
  to be 1.8 or 2.0.1 — older firmware isn't supported by WirePod.
- Python 3, with the **WirePod fork of the Vector SDK** installed. The
  original `anki_vector` package on PyPI talks to Anki's own cloud
  backend, which has been shut down — it will not authenticate against a
  self-hosted robot. Use the WirePod-compatible fork instead:
  [kercre123/wirepod-vector-python-sdk](https://github.com/kercre123/wirepod-vector-python-sdk)
  (itself built on cyb3rdog's earlier fork, with 3.11 support added by
  MoonDog83). Install docs and a full function reference are hosted at
  https://keriganc.com/sdkdocs — worth a look regardless of how you install
  it. It's a drop-in replacement — the import stays `anki_vector`, and
  every script in this repo works unmodified against it.
- Your robot's serial number, and SDK credentials set up per WirePod's own
  onboarding docs
- A Vector cube, for every game except `vector_challenge.py`

Every script takes `--serial YOUR_SERIAL`. If you only have one Vector
paired, you can usually omit it.

```bash
git clone https://github.com/kercre123/wirepod-vector-python-sdk
cd wirepod-vector-python-sdk
pip install .
python3 keepaway.py --serial YOUR_SERIAL
```

Several games support `--selftest`, which runs the pure game-logic checks
with no robot connection at all — useful for confirming your environment is
sane, or for hacking on the logic without waking the robot up:

```bash
python3 keepaway.py --selftest
```

---

## The games

### `keepaway.py` — Cozmo Keepaway

The classic. Bait Vector with his cube; he'll try to pounce on it. Pull it
away in time and you get the point; let him catch it and he gets one.
First to five, or play an open-ended session and pet his back between
rounds to end it.

**He learns.** Every *game* he loses sharpens his reflexes (shorter
hesitation before he strikes); every game he wins makes him cocky (longer
hesitation). This persists across sessions in a small JSON file, so he
genuinely gets harder to beat with practice — and, unlike the original
Cozmo version, he can also get worse again if you keep winning. It's
self-balancing rather than one-directional.

```bash
python3 keepaway.py --serial YOUR_SERIAL
python3 keepaway.py --skill            # see how good he's got, no game played
python3 keepaway.py --reset-skill      # make him a beginner again
python3 keepaway.py --selftest
```

Skill file lives at `~/.vector_keepaway.json` (works the same on Linux,
macOS, and Windows via `Path.home()`). It's plain JSON — feel free to
inspect or reset it by hand, but see the honest note in
[HARDWARE_NOTES.md](docs/HARDWARE_NOTES.md#the-skill-file-isnt-locked-down-on-purpose)
about why it isn't locked down.

### `feeding.py` — feed him

Shake (or tap) his cube to charge it with "food." When it's full, he
notices, trudges over, nests his lift on it, and eats — the cube
discharging in exactly the reverse of however you charged it, his head
rising as a progress bar while he swallows. Then he wants more. Feed him
again and it gets harder (corner-by-corner instead of all-at-once)... and
the second time, he overdoes it.

```bash
python3 feeding.py --serial YOUR_SERIAL
python3 feeding.py --serial YOUR_SERIAL --green      # green cube lights, not blue
python3 feeding.py --serial YOUR_SERIAL --tap        # tap to charge; shake is default
python3 feeding.py --serial YOUR_SERIAL -w           # always finish with the WHEELIE
python3 feeding.py --serial YOUR_SERIAL -o           # always finish with the OVERPUSH
python3 feeding.py --serial YOUR_SERIAL -r           # always finish with the HEAD ROCK
```

Without `-w`/`-o`/`-r`, he picks one of the three at random once he's eaten
too much. Each is a genuinely physical reaction — see
[HARDWARE_NOTES.md](docs/HARDWARE_NOTES.md#the-wheelie) for how the wheelie
in particular works.

### `feedothers.py` — feed his friends

The same charge-the-cube mechanic as `feeding.py`, but Vector isn't the one
eating and he never moves. Grab a soft toy, tap the cube with its mouth to
charge it up, then tap it seven more times to drain it back out — one visible
step per tap. Vector sits and watches, reacting to each one. Then it resets
and you can do it again with the next toy, as many times as you like.

Three deliberate differences from `feeding.py`:

- **He doesn't drive.** No approach, no nesting, no ToF or camera work — all
  the physical business happens in your hands, so it works anywhere you can
  sit him down.
- **Unlimited rounds.** `feeding.py` has two stages and then it's over.
  This just keeps going.
- **The lights are always uniform.** All four corners brighten together every
  time — there's no corner-by-corner fill stage, and a full cube is simply
  all four solid.

To stop, pet his back while the cube is sitting empty and waiting for the next
charge. Petting him mid-charge or mid-discharge does nothing, so you can't end
the session by accident while steadying him.

```bash
python3 feedothers.py --serial YOUR_SERIAL
python3 feedothers.py --serial YOUR_SERIAL --green    # green cube lights, not blue
python3 feedothers.py --serial YOUR_SERIAL --tap      # tap to charge; shake is default
python3 feedothers.py --serial YOUR_SERIAL --taps 5   # discharge in 5 taps instead of 7
python3 feedothers.py --selftest
```

### `hot_potato.py` — party game, 2+ players

Vector is a timer with a personality, not a referee. He doesn't know how
many players there are unless you tell him, doesn't know who's holding the
cube, and doesn't know who's out — that's on the players to sort out
between themselves.

Pet his back to add players (starts at 2); nudge his lift, then nudge it
again to confirm, and the game begins. Each round the cube cycles colours
for a hidden 25–60 second timer. He gets bored and idles while it's being
passed around. For the last ten seconds he goes completely still and
counts down out loud — no animations, just the countdown. Then the cube
flashes red and whoever's holding it is out. Last player standing wins.

```bash
python3 hot_potato.py --serial YOUR_SERIAL
python3 hot_potato.py --serial YOUR_SERIAL --players 4     # skip the count-in
python3 hot_potato.py --serial YOUR_SERIAL --min-s 20 --max-s 45
```

### `reaction_game.py` — tap when the colours match

The cube flashes mismatched colours on each corner, then all four snap to
the same colour — that's your cue to tap. Fast enough and it's your point;
too slow, no tap, or tapping before the match, and it's his. At the end he
plays a red spinner or a rainbow flash depending on who won, then reads out
the final score.

**Two modes, pet-cycled at the start** (same as keepaway and sleepy vector):

- **First to 10** — the default. The game ends on score.
- **Infinite** — runs until you stop it. **Hold his back for 1–5 seconds**
  to end it, and he'll tot up the score.

The back sensor is a quit button **in infinite mode only**. In first-to-N it
isn't polled at all, so holding his back does nothing — the game ends when
someone hits the target, not when you say so.

Vector narrates the rules himself once the mode is locked in, and the last
line changes with the mode: in first-to-N he names the target score; in
infinite he explains how to quit, since that's the only way it ever ends.
Pet him during the narration to skip the rest of it.

```bash
python3 reaction_game.py --serial YOUR_SERIAL
python3 reaction_game.py --serial YOUR_SERIAL --win-score 5   # first to 5 instead
python3 reaction_game.py --selftest
```

### `8_ball.py` — Magic 8-Ball

Ask him a question out loud, then pet his back to make him "roll." By
default he shakes his own head rapidly (motor-driven); `--safemode` swaps
that for the built-in shake animations instead, if you'd rather not run
his head motor hard.

```bash
python3 8_ball.py --serial YOUR_SERIAL
python3 8_ball.py --serial YOUR_SERIAL --safemode
```

### `fortune_teller.py` — ask the spirits

Similar shape to the 8-ball, dressed up as a fortune-telling ritual: tap
his cube once to begin, then don't tap it too many more times or you'll
anger the spirits (there's a real consequence for overdoing it — a
motor-driven "possessed" shake and an angry reaction). Otherwise he "searches"
for a while and gives you a fortune, weighted from good to absurd, with the
cube colour matching the tone of the answer.

```bash
python3 fortune_teller.py --serial YOUR_SERIAL
```

### `sleepy_vector.py` — don't wake him up

He falls asleep. Pet him and he stirs physically (head and lift lift up and
settle back down) without actually waking — his eyes stay shut throughout.
There's a secret threshold, 3–15 pets, hidden per session; cross it and he
wakes with a shocked jolt, then gets properly furious at you for waking
him. Solo mode just counts how many pets you survived; multiplayer calls
out whoever landed the fatal pet. Toggle between modes by petting during
the initial mode-select window.

The shocked-then-furious wake-up is a nod to "Angry EMO," a mode on
[Living AI's Emo](https://living.ai/emo/) — another small desktop robot
with a similar move: disturb it while it's resting and it reacts with real
irritation rather than just waking up neutrally. A demonstration of that
behaviour is here: https://youtu.be/sCq4RrATedI. Vector doesn't have
anything built-in like it, so this recreates the beat using his own
animation set — the shock clip, the furious clip, then a scolding line.

```bash
python3 sleepy_vector.py --serial YOUR_SERIAL
python3 sleepy_vector.py --selftest
```

### `vector_challenge.py` — Simon-says-adjacent reflex test

The simplest game here, and the origin of a couple of the mechanics the
others rely on more carefully (notably that `lift_height_mm` and
`head_angle_rad` can be read live and compared against a baseline to detect
a human moving the arm or head by hand). He calls out a random command —
move my head, pet me, or move my lift — and you have a shrinking window to
comply. Miss one and it's over; survive rounds and the window keeps
tightening.

```bash
python3 vector_challenge.py
```

This one predates the serial/argparse conventions of the others and is
kept closer to its original form deliberately — it's the rough prototype
that led to the more careful lift/head detection used elsewhere in this
repo, and it's honestly a little rough around the edges. Worth knowing
that going in.

---

## Running these through WirePod

Nothing here needs WirePod at all — every script is a standalone SDK
program you run from a terminal, and it'll work against any properly
paired Vector regardless of what's serving voice commands. WirePod is just
what's running voice/intents on this particular robot day-to-day, and it's
worth showing how the two coexist.

### As a WirePod custom intent

WirePod's custom intents let a voice phrase run an arbitrary command. Each
game becomes: *"Hey Vector, let's play keepaway"* → WirePod shells out to
the script.

The easiest way to add one is through WirePod's own web UI rather than
hand-editing JSON: go to `http://<your-wirepod-host>:8080`, find the
**Custom Intents** section, and add a new intent there. It'll ask for the
same fields shown below and writes them into
`~/wire-pod/chipper/customIntents.json` for you.

If you'd rather edit that file directly, an entry looks like this:

```json
{
  "name": "play_keepaway",
  "utterances": ["let's play keepaway", "play keepaway"],
  "intent": "intent_custom_playkeepaway",
  "exec": "path/to/python",
  "execargs": ["path/to/script.py", "--serial", "!botSerial"]
}
```

`path/to/python` and `path/to/script.py` are exactly that — fill in wherever
Python and your copy of this repo actually live. `!botSerial` is WirePod's
own substitution: it fills in the serial of whichever Vector triggered the
intent, so this works unmodified even with more than one bot registered, and
you never have to put a real serial into a config file.

**Watch the `execargs` strings for leading spaces.** WirePod passes each
array element straight through as an argv entry, and a stray `" --serial"`
(space at the front) breaks argument parsing silently — the script just
falls back to `--serial None`, or argparse rejects it outright and WirePod
falls back to a built-in intent as if the custom one didn't exist at all.
Keep them exactly `"--serial"`, no leading whitespace.

**If WirePod runs as root** (common on a from-scratch install) but your
Vector SDK credentials were generated as your normal user, the script will
fail to authenticate — `VectorUnauthenticatedException: 401` — because
root has its own empty `~/.anki_vector/`. Fix:

```bash
sudo cp -r ~/.anki_vector/* /root/.anki_vector/
sudo systemctl restart wire-pod
```

Redo this after any reflash or IP change; it's not persistent.

### Standalone

Just run it. WirePod doesn't need to be involved, and won't fight over the
connection as long as it isn't actively holding behavior control when the
script starts — if the robot seems unresponsive to the script's commands,
stop WirePod first (`sudo systemctl stop wire-pod`) and try again.

---

## A note on why some of these files look different from each other

This repo grew over several sessions, and it shows — `vector_challenge.py`
still uses `robot.behavior.say_text` calls with no error handling at all,
while `keepaway.py` has a full offline self-test suite and a persistent
learning system. That's left deliberately unpolished rather than smoothed
over, partly because a rough first draft next to a refined one is honestly
a useful before/after for anyone learning the SDK themselves.

If you fork this and clean one of the older ones up, a PR would be
welcome.
