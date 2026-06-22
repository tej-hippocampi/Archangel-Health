# Arena Archers 🏹

A small top-down arena **fighting game** built as a side project. Pure
HTML5 Canvas + vanilla JavaScript — no build step, no dependencies.

> This folder is intentionally **separate** from the Archangel-Health
> application. It shares nothing with the clinical codebase.

## Run it

Just open `index.html` in any modern browser. That's it.

Or serve it locally (avoids any future file:// quirks):

```bash
cd game
python3 -m http.server 8000
# then visit http://localhost:8000
```

## How to play

| Action | Control |
| --- | --- |
| Move | `W` `A` `S` `D` or arrow keys |
| Aim | Mouse |
| Shoot arrow | Left click (hold to **charge** for more damage & range) |
| Melee slash | `Space` |
| Dash / dodge | `Shift` |

Drain the CPU opponent's health bar to win.

## Features

- **8 unlockable characters**, each with distinct speed / damage / fire-rate
  / health stats and play styles (all-rounder, glass cannon, tank, assassin…).
- **6 unlockable arenas** with different layouts, obstacles, and themes.
- **Unlock system + lock UI** — locked items show on the select screens with
  their unlock requirement. Progress (wins) is saved in `localStorage`, so
  unlocks persist between sessions.
- Charged bow shots, melee slashes, dashes, particle effects, and a
  range-aware CPU opponent.

## Project layout

| File | Purpose |
| --- | --- |
| `index.html` | Screens / markup (menu, character select, level select, game) |
| `style.css` | All styling |
| `data.js` | Character and level definitions — **edit here to add content** |
| `game.js` | Engine: input, physics, AI, rendering, unlock logic |

## Adding a character or level

Open `data.js` and add an entry to the `CHARACTERS` or `LEVELS` array.
Set `locked: false` to make it available immediately, or `locked: true`
plus an `unlockHint` and a matching entry in `CHAR_UNLOCK` / `LEVEL_UNLOCK`
(in `game.js`) to gate it behind a number of wins.

## Reset progress

Open the browser console and run:

```js
localStorage.removeItem("arenaArchers.save.v1");
```
