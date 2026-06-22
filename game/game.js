/* ============================================================
   Arena Archers — engine + UI wiring
   ============================================================ */
"use strict";

/* ---------- Persistent progress (unlocks) ---------- */
const SAVE_KEY = "arenaArchers.save.v1";
function loadSave() {
  try { return JSON.parse(localStorage.getItem(SAVE_KEY)) || {}; }
  catch { return {}; }
}
function saveSave(s) {
  try { localStorage.setItem(SAVE_KEY, JSON.stringify(s)); } catch {}
}
let save = loadSave();
if (typeof save.wins !== "number") save.wins = 0;

// Wins required to unlock each locked item (matches the unlockHint text).
const CHAR_UNLOCK = { ember: 1, frost: 3, shade: 5, titan: 8 };
const LEVEL_UNLOCK = { frostpeak: 2, volcano: 4, void: 6 };

function isCharUnlocked(c) {
  if (!c.locked) return true;
  return save.wins >= (CHAR_UNLOCK[c.id] ?? Infinity);
}
function isLevelUnlocked(l) {
  if (!l.locked) return true;
  return save.wins >= (LEVEL_UNLOCK[l.id] ?? Infinity);
}

/* ---------- Stat scaling (1–10 -> real values) ---------- */
function scaleStats(s) {
  return {
    moveSpeed: 1.6 + s.speed * 0.28,        // px/frame
    arrowDamage: 6 + s.damage * 2.2,        // base, before charge
    fireCooldown: 46 - s.fireRate * 3.6,    // frames between shots
    maxHp: 70 + s.health * 12,
    dashDist: 90 + s.speed * 8,
  };
}

/* ---------- Screen navigation ---------- */
const screens = document.querySelectorAll(".screen");
function goto(id) {
  screens.forEach(s => s.classList.toggle("active", s.id === id));
  if (id !== "game") stopGame();
  if (id === "characters") renderCharacters();
  if (id === "levels") renderLevels();
}
document.querySelectorAll("[data-goto]").forEach(el =>
  el.addEventListener("click", () => goto(el.dataset.goto))
);

/* ---------- Selection state ---------- */
let selectedChar = null;
let selectedLevel = null;

/* ---------- Character select UI ---------- */
const charGrid = document.getElementById("characterGrid");
const toLevelsBtn = document.getElementById("toLevels");

function renderCharacters() {
  charGrid.innerHTML = "";
  CHARACTERS.forEach(c => {
    const unlocked = isCharUnlocked(c);
    const card = document.createElement("div");
    card.className = "card" + (!unlocked ? " locked" : "") +
                     (selectedChar?.id === c.id ? " selected" : "");
    card.innerHTML = `
      <div class="avatar" style="background:${c.color}33;color:${c.color}">${c.emoji}</div>
      <h4>${c.name}</h4>
      <div class="role">${c.role}</div>
      ${!unlocked ? `<div class="unlock-hint">${c.unlockHint || ""}</div>` : ""}`;
    if (unlocked) {
      card.addEventListener("click", () => { selectedChar = c; renderCharacters(); showCharInfo(c); });
    }
    charGrid.appendChild(card);
  });
  toLevelsBtn.disabled = !selectedChar;
}

function showCharInfo(c) {
  document.getElementById("charPreview").textContent = c.emoji;
  document.getElementById("charPreview").style.background = c.color + "22";
  document.getElementById("charName").textContent = `${c.name} — ${c.role}`;
  document.getElementById("charDesc").textContent = c.desc;
  const order = [["speed","Speed"],["damage","Damage"],["fireRate","Fire Rate"],["health","Health"]];
  document.getElementById("charStats").innerHTML = order.map(([k,label]) =>
    `<li>${label}<span class="meter"><i style="width:${c.stats[k]*10}%;background:${c.color}"></i></span></li>`
  ).join("");
}
toLevelsBtn.addEventListener("click", () => { if (selectedChar) goto("levels"); });

/* ---------- Level select UI ---------- */
const levelGrid = document.getElementById("levelGrid");
const startBtn = document.getElementById("startMatch");

function renderLevels() {
  levelGrid.innerHTML = "";
  LEVELS.forEach(l => {
    const unlocked = isLevelUnlocked(l);
    const card = document.createElement("div");
    card.className = "card" + (!unlocked ? " locked" : "") +
                     (selectedLevel?.id === l.id ? " selected" : "");
    card.innerHTML = `
      <div class="thumb" style="background:linear-gradient(135deg,${l.accent},${l.floor})"></div>
      <h4>${l.name}</h4>
      <div class="role">${l.desc}</div>
      ${!unlocked ? `<div class="unlock-hint">${l.unlockHint || ""}</div>` : ""}`;
    if (unlocked) {
      card.addEventListener("click", () => { selectedLevel = l; renderLevels(); });
    }
    levelGrid.appendChild(card);
  });
  startBtn.disabled = !selectedLevel;
}
startBtn.addEventListener("click", () => { if (selectedLevel) startGame(); });

/* ============================================================
   GAMEPLAY
   ============================================================ */
const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
const W = canvas.width, H = canvas.height;
const PLAYER_R = 16;

let game = null;          // current game state
let rafId = null;
const keys = {};
const mouse = { x: W / 2, y: H / 2, down: false };

/* ---- Input ---- */
window.addEventListener("keydown", e => {
  keys[e.key.toLowerCase()] = true;
  if ([" ", "arrowup", "arrowdown", "arrowleft", "arrowright"].includes(e.key.toLowerCase()))
    e.preventDefault();
});
window.addEventListener("keyup", e => { keys[e.key.toLowerCase()] = false; });
canvas.addEventListener("mousemove", e => {
  const r = canvas.getBoundingClientRect();
  mouse.x = (e.clientX - r.left) * (W / r.width);
  mouse.y = (e.clientY - r.top) * (H / r.height);
});
canvas.addEventListener("mousedown", () => { mouse.down = true; });
window.addEventListener("mouseup", () => { mouse.down = false; });

function makeFighter(charDef, x, y, isCPU) {
  const st = scaleStats(charDef.stats);
  return {
    def: charDef, ...st,
    x, y, hp: st.maxHp, isCPU,
    aim: isCPU ? Math.PI : 0,
    cooldown: 0, charge: 0, charging: false,
    meleeTimer: 0, meleeCd: 0,
    dashCd: 0, dashTimer: 0, dashVx: 0, dashVy: 0,
    flash: 0,
    ai: { decision: 0, strafeDir: 1, wantDist: 220 },
  };
}

function startGame() {
  goto("game");
  const cpuPool = CHARACTERS.filter(isCharUnlocked);
  let cpuDef = cpuPool[Math.floor(Math.random() * cpuPool.length)];
  // try not to mirror the player
  if (cpuDef.id === selectedChar.id && cpuPool.length > 1) {
    cpuDef = cpuPool.find(c => c.id !== selectedChar.id) || cpuDef;
  }
  game = {
    level: selectedLevel,
    p1: makeFighter(selectedChar, 150, H / 2, false),
    p2: makeFighter(cpuDef, W - 150, H / 2, true),
    arrows: [],
    particles: [],
    slashes: [],
    over: false,
    intro: 90,
  };
  document.getElementById("hudP1").textContent = selectedChar.name;
  document.getElementById("hudP2").textContent = cpuDef.name + " (CPU)";
  document.getElementById("overlay").classList.add("hidden");
  document.getElementById("roundMsg").textContent = "Ready…";
  cancelAnimationFrame(rafId);
  loop();
}

function stopGame() {
  cancelAnimationFrame(rafId);
  rafId = null;
}

document.getElementById("rematch").addEventListener("click", startGame);

/* ---- Geometry helpers ---- */
function dist(ax, ay, bx, by) { return Math.hypot(ax - bx, ay - by); }
function clampToArena(o) {
  o.x = Math.max(PLAYER_R, Math.min(W - PLAYER_R, o.x));
  o.y = Math.max(PLAYER_R, Math.min(H - PLAYER_R, o.y));
  for (const ob of game.level.obstacles) {
    const d = dist(o.x, o.y, ob.x, ob.y);
    const min = ob.r + PLAYER_R;
    if (d < min && d > 0) {
      const nx = (o.x - ob.x) / d, ny = (o.y - ob.y) / d;
      o.x = ob.x + nx * min;
      o.y = ob.y + ny * min;
    }
  }
}
function lineHitsObstacle(x, y) {
  for (const ob of game.level.obstacles)
    if (dist(x, y, ob.x, ob.y) < ob.r) return true;
  return false;
}

/* ---- Spawning effects ---- */
function spawnArrow(f, target) {
  const charge = Math.min(f.charge, 1);
  const speed = 7.5 + charge * 6.5;
  const dmg = f.arrowDamage * (0.6 + charge * 1.1);
  game.arrows.push({
    x: f.x + Math.cos(f.aim) * (PLAYER_R + 6),
    y: f.y + Math.sin(f.aim) * (PLAYER_R + 6),
    vx: Math.cos(f.aim) * speed,
    vy: Math.sin(f.aim) * speed,
    dmg, owner: f, life: 130, charge,
  });
  f.cooldown = f.fireCooldown;
}
function burst(x, y, color, n, spd) {
  for (let i = 0; i < n; i++) {
    const a = Math.random() * Math.PI * 2;
    const s = Math.random() * spd;
    game.particles.push({ x, y, vx: Math.cos(a) * s, vy: Math.sin(a) * s, life: 24, color });
  }
}

/* ---- Per-fighter update ---- */
function updatePlayer(f) {
  let dx = 0, dy = 0;
  if (keys["w"] || keys["arrowup"]) dy--;
  if (keys["s"] || keys["arrowdown"]) dy++;
  if (keys["a"] || keys["arrowleft"]) dx--;
  if (keys["d"] || keys["arrowright"]) dx++;
  if (dx || dy) {
    const m = Math.hypot(dx, dy);
    f.x += (dx / m) * f.moveSpeed;
    f.y += (dy / m) * f.moveSpeed;
  }
  f.aim = Math.atan2(mouse.y - f.y, mouse.x - f.x);

  // Charged shooting
  if (mouse.down && f.cooldown <= 0) { f.charging = true; f.charge = Math.min(1, f.charge + 0.025); }
  else if (f.charging && !mouse.down) { spawnArrow(f); f.charging = false; f.charge = 0; }
  if (!mouse.down) f.charge = Math.max(0, f.charge - 0.05);

  // Melee
  if (keys[" "] && f.meleeCd <= 0) { f.meleeTimer = 12; f.meleeCd = 34; doMelee(f); }
  // Dash
  if (keys["shift"] && f.dashCd <= 0 && (dx || dy)) {
    const m = Math.hypot(dx, dy);
    f.dashTimer = 8; f.dashCd = 50;
    f.dashVx = (dx / m) * (f.dashDist / 8);
    f.dashVy = (dy / m) * (f.dashDist / 8);
  }
}

function updateCPU(f, foe) {
  const ai = f.ai;
  const d = dist(f.x, f.y, foe.x, foe.y);
  f.aim = Math.atan2(foe.y - f.y, foe.x - f.x);

  ai.decision--;
  if (ai.decision <= 0) {
    ai.decision = 40 + Math.random() * 40;
    ai.strafeDir = Math.random() < 0.5 ? 1 : -1;
    ai.wantDist = 170 + Math.random() * 140;
  }

  // Move toward preferred range, strafe, keep inside arena
  let mx = 0, my = 0;
  const toFoe = { x: Math.cos(f.aim), y: Math.sin(f.aim) };
  if (d > ai.wantDist + 30) { mx += toFoe.x; my += toFoe.y; }
  else if (d < ai.wantDist - 30) { mx -= toFoe.x; my -= toFoe.y; }
  // strafe perpendicular
  mx += -toFoe.y * ai.strafeDir * 0.8;
  my += toFoe.x * ai.strafeDir * 0.8;
  // nudge toward center if near a wall
  mx += (W / 2 - f.x) * 0.0008;
  my += (H / 2 - f.y) * 0.0008;

  const m = Math.hypot(mx, my) || 1;
  f.x += (mx / m) * f.moveSpeed * 0.92;
  f.y += (my / m) * f.moveSpeed * 0.92;

  // Shoot with charge when roughly facing & clear line
  if (f.cooldown <= 0) {
    f.charging = true;
    f.charge = Math.min(1, f.charge + 0.03);
    const wantCharge = ai.wantDist > 250 ? 0.85 : 0.35;
    if (f.charge >= wantCharge && Math.random() < 0.06) {
      spawnArrow(f); f.charging = false; f.charge = 0;
    }
  }
  // Melee if very close
  if (d < 60 && f.meleeCd <= 0) { f.meleeTimer = 12; f.meleeCd = 34; doMelee(f); }
  // Dodge occasionally
  if (f.dashCd <= 0 && Math.random() < 0.012) {
    f.dashTimer = 8; f.dashCd = 50;
    f.dashVx = -toFoe.y * ai.strafeDir * (f.dashDist / 8);
    f.dashVy = toFoe.x * ai.strafeDir * (f.dashDist / 8);
  }
}

function doMelee(f) {
  const foe = f === game.p1 ? game.p2 : game.p1;
  const reach = 46;
  game.slashes.push({ x: f.x, y: f.y, aim: f.aim, life: 12, owner: f });
  const mx = f.x + Math.cos(f.aim) * reach * 0.6;
  const my = f.y + Math.sin(f.aim) * reach * 0.6;
  if (dist(mx, my, foe.x, foe.y) < reach) {
    // facing check
    const a = Math.atan2(foe.y - f.y, foe.x - f.x);
    let diff = Math.abs(a - f.aim);
    diff = Math.min(diff, Math.PI * 2 - diff);
    if (diff < 1.1) {
      damage(foe, f.arrowDamage * 0.7);
      // knockback
      foe.x += Math.cos(f.aim) * 22;
      foe.y += Math.sin(f.aim) * 22;
      burst(foe.x, foe.y, "#fff", 10, 4);
    }
  }
}

function damage(f, amount) {
  f.hp -= amount;
  f.flash = 6;
  if (f.hp <= 0) { f.hp = 0; endMatch(f === game.p2); }
}

function stepFighter(f) {
  if (f.cooldown > 0) f.cooldown--;
  if (f.meleeTimer > 0) f.meleeTimer--;
  if (f.meleeCd > 0) f.meleeCd--;
  if (f.dashCd > 0) f.dashCd--;
  if (f.flash > 0) f.flash--;
  if (f.dashTimer > 0) {
    f.x += f.dashVx; f.y += f.dashVy; f.dashTimer--;
    burst(f.x, f.y, f.def.color, 2, 1.5);
  }
  clampToArena(f);
}

/* ---- Arrows ---- */
function updateArrows() {
  for (let i = game.arrows.length - 1; i >= 0; i--) {
    const a = game.arrows[i];
    a.x += a.vx; a.y += a.vy; a.life--;
    const foe = a.owner === game.p1 ? game.p2 : game.p1;
    let dead = false;
    if (a.x < 0 || a.x > W || a.y < 0 || a.y > H || a.life <= 0) dead = true;
    else if (lineHitsObstacle(a.x, a.y)) { burst(a.x, a.y, "#999", 6, 3); dead = true; }
    else if (dist(a.x, a.y, foe.x, foe.y) < PLAYER_R + 4) {
      damage(foe, a.dmg);
      burst(a.x, a.y, foe.def.color, 12, 4);
      dead = true;
    }
    if (dead) game.arrows.splice(i, 1);
  }
}

function updateParticles() {
  for (let i = game.particles.length - 1; i >= 0; i--) {
    const p = game.particles[i];
    p.x += p.vx; p.y += p.vy; p.vx *= 0.9; p.vy *= 0.9; p.life--;
    if (p.life <= 0) game.particles.splice(i, 1);
  }
  for (let i = game.slashes.length - 1; i >= 0; i--) {
    if (--game.slashes[i].life <= 0) game.slashes.splice(i, 1);
  }
}

/* ============================================================
   DRAW
   ============================================================ */
function draw() {
  const L = game.level;
  ctx.fillStyle = L.floor;
  ctx.fillRect(0, 0, W, H);
  // grid
  ctx.strokeStyle = L.grid;
  ctx.lineWidth = 1;
  for (let x = 0; x <= W; x += 48) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
  for (let y = 0; y <= H; y += 48) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
  // border glow
  ctx.strokeStyle = L.accent; ctx.lineWidth = 4;
  ctx.strokeRect(2, 2, W - 4, H - 4);

  // obstacles
  for (const ob of L.obstacles) {
    ctx.beginPath(); ctx.arc(ob.x, ob.y, ob.r, 0, Math.PI * 2);
    ctx.fillStyle = L.accent; ctx.fill();
    ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,.3)"; ctx.stroke();
  }

  // slashes
  for (const s of game.slashes) {
    ctx.save();
    ctx.translate(s.x, s.y); ctx.rotate(s.aim);
    ctx.globalAlpha = s.life / 12;
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 4;
    ctx.beginPath(); ctx.arc(8, 0, 40, -0.9, 0.9); ctx.stroke();
    ctx.restore(); ctx.globalAlpha = 1;
  }

  drawFighter(game.p1);
  drawFighter(game.p2);

  // arrows
  for (const a of game.arrows) {
    const ang = Math.atan2(a.vy, a.vx);
    ctx.save(); ctx.translate(a.x, a.y); ctx.rotate(ang);
    const len = 14 + a.charge * 6;
    ctx.strokeStyle = a.charge > 0.6 ? "#ffce54" : "#e8edf6";
    ctx.lineWidth = 2 + a.charge * 2;
    ctx.beginPath(); ctx.moveTo(-len, 0); ctx.lineTo(len * 0.5, 0); ctx.stroke();
    // arrowhead
    ctx.fillStyle = ctx.strokeStyle;
    ctx.beginPath(); ctx.moveTo(len * 0.5, 0); ctx.lineTo(len * 0.5 - 6, -4);
    ctx.lineTo(len * 0.5 - 6, 4); ctx.fill();
    ctx.restore();
  }

  // particles
  for (const p of game.particles) {
    ctx.globalAlpha = Math.max(0, p.life / 24);
    ctx.fillStyle = p.color;
    ctx.fillRect(p.x - 2, p.y - 2, 4, 4);
  }
  ctx.globalAlpha = 1;

  // intro countdown
  if (game.intro > 0) {
    ctx.fillStyle = "rgba(0,0,0,.35)"; ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = "#ffce54"; ctx.font = "bold 64px Segoe UI"; ctx.textAlign = "center";
    const n = Math.ceil(game.intro / 30);
    ctx.fillText(n > 0 ? n : "FIGHT!", W / 2, H / 2 + 20);
    ctx.textAlign = "left";
  }
}

function drawFighter(f) {
  // shadow
  ctx.fillStyle = "rgba(0,0,0,.35)";
  ctx.beginPath(); ctx.ellipse(f.x, f.y + PLAYER_R - 2, PLAYER_R, 6, 0, 0, Math.PI * 2); ctx.fill();

  // body
  ctx.beginPath(); ctx.arc(f.x, f.y, PLAYER_R, 0, Math.PI * 2);
  ctx.fillStyle = f.flash > 0 ? "#fff" : f.def.color; ctx.fill();
  ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,.4)"; ctx.stroke();

  // emoji face
  ctx.font = "18px serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(f.def.emoji, f.x, f.y + 1);
  ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";

  // bow + aim direction
  const ax = f.x + Math.cos(f.aim) * (PLAYER_R + 4);
  const ay = f.y + Math.sin(f.aim) * (PLAYER_R + 4);
  ctx.save(); ctx.translate(ax, ay); ctx.rotate(f.aim + Math.PI / 2);
  ctx.strokeStyle = "#6b4a2a"; ctx.lineWidth = 3;
  ctx.beginPath(); ctx.arc(0, 0, 11, -1.0, 1.0); ctx.stroke();
  // bowstring (pulled back when charging)
  const pull = f.charging ? -3 - f.charge * 6 : 0;
  ctx.strokeStyle = "#ddd"; ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, -9); ctx.lineTo(pull, 0); ctx.lineTo(0, 9); ctx.stroke();
  ctx.restore();

  // charge ring
  if (f.charging && f.charge > 0.02) {
    ctx.beginPath(); ctx.arc(f.x, f.y, PLAYER_R + 6, -Math.PI / 2, -Math.PI / 2 + f.charge * Math.PI * 2);
    ctx.strokeStyle = "#ffce54"; ctx.lineWidth = 3; ctx.stroke();
  }
}

/* ---- HUD ---- */
function updateHUD() {
  document.getElementById("hpP1").style.width = (game.p1.hp / game.p1.maxHp * 100) + "%";
  document.getElementById("hpP2").style.width = (game.p2.hp / game.p2.maxHp * 100) + "%";
  document.getElementById("chargeBar").style.width = (game.p1.charge * 100) + "%";
}

/* ============================================================
   MAIN LOOP
   ============================================================ */
function loop() {
  if (!game) return;
  if (game.intro > 0) {
    game.intro--;
    if (game.intro === 0) document.getElementById("roundMsg").textContent = "FIGHT!";
  } else if (!game.over) {
    updatePlayer(game.p1);
    updateCPU(game.p2, game.p1);
    stepFighter(game.p1);
    stepFighter(game.p2);
    updateArrows();
    updateParticles();
    updateHUD();
  } else {
    updateParticles();
  }
  draw();
  rafId = requestAnimationFrame(loop);
}

/* ---- Match end ---- */
function endMatch(playerWon) {
  if (game.over) return;
  game.over = true;
  const overlay = document.getElementById("overlay");
  document.getElementById("roundMsg").textContent = "";
  burst(playerWon ? game.p2.x : game.p1.x, playerWon ? game.p2.y : game.p1.y,
        "#ffce54", 40, 7);

  if (playerWon) {
    const before = save.wins;
    save.wins = before + 1;
    saveSave(save);
    const newlyUnlocked = checkNewUnlocks(before, save.wins);
    document.getElementById("overlayTitle").textContent = "Victory! 🏆";
    let sub = `Wins: ${save.wins}`;
    if (newlyUnlocked.length) sub += ` — Unlocked: ${newlyUnlocked.join(", ")}!`;
    document.getElementById("overlaySub").textContent = sub;
  } else {
    document.getElementById("overlayTitle").textContent = "Defeated 💀";
    document.getElementById("overlaySub").textContent = "Try a different fighter or arena.";
  }
  overlay.classList.remove("hidden");
}

function checkNewUnlocks(before, after) {
  const out = [];
  for (const c of CHARACTERS)
    if (CHAR_UNLOCK[c.id] && before < CHAR_UNLOCK[c.id] && after >= CHAR_UNLOCK[c.id])
      out.push(c.name);
  for (const l of LEVELS)
    if (LEVEL_UNLOCK[l.id] && before < LEVEL_UNLOCK[l.id] && after >= LEVEL_UNLOCK[l.id])
      out.push(l.name);
  return out;
}

/* ---- Boot ---- */
goto("menu");
