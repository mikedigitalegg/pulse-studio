const appShell = document.getElementById("appShell");
const visualFrame = document.getElementById("visualFrame");
const shareBtn = document.getElementById("shareBtn");
const deviceBtn = document.getElementById("deviceBtn");
const fullscreenBtn = document.getElementById("fullscreenBtn");
const cinematicBtn = document.getElementById("cinematicBtn");
const hudFullscreenBtn = document.getElementById("hudFullscreenBtn");
const hudCinematicBtn = document.getElementById("hudCinematicBtn");
const stopBtn = document.getElementById("stopBtn");
const refreshDevicesBtn = document.getElementById("refreshDevicesBtn");
const prevSceneBtn = document.getElementById("prevSceneBtn");
const nextSceneBtn = document.getElementById("nextSceneBtn");
const prevThemeBtn = document.getElementById("prevThemeBtn");
const nextThemeBtn = document.getElementById("nextThemeBtn");
const autoOffBtn = document.getElementById("autoOffBtn");
const autoCycleBtn = document.getElementById("autoCycleBtn");
const autoRandomBtn = document.getElementById("autoRandomBtn");
const autoReactiveBtn = document.getElementById("autoReactiveBtn");
const hudAutoOffBtn = document.getElementById("hudAutoOffBtn");
const hudAutoCycleBtn = document.getElementById("hudAutoCycleBtn");
const hudAutoRandomBtn = document.getElementById("hudAutoRandomBtn");
const hudAutoReactiveBtn = document.getElementById("hudAutoReactiveBtn");
const autoIntervalSelect = document.getElementById("autoIntervalSelect");
const lockThemeBtn = document.getElementById("lockThemeBtn");
const deviceSelect = document.getElementById("deviceSelect");
const bassSensitivityInput = document.getElementById("bassSensitivityInput");
const bassSizeInput = document.getElementById("bassSizeInput");
const themeButtons = [...document.querySelectorAll(".theme-btn")];
const sceneButtons = [...document.querySelectorAll(".scene-btn")];
const statusText = document.getElementById("statusText");
const energyText = document.getElementById("energyText");
const bandText = document.getElementById("bandText");
const sourceText = document.getElementById("sourceText");
const sceneLabel = document.getElementById("sceneLabel");
const heroPreviewScene = document.getElementById("heroPreviewScene");
const hudSceneName = document.getElementById("hudSceneName");
const hudThemeName = document.getElementById("hudThemeName");
const hudAutoState = document.getElementById("hudAutoState");
const hudLockThemeBtn = document.getElementById("hudLockThemeBtn");
const bassSensitivityValue = document.getElementById("bassSensitivityValue");
const bassSizeValue = document.getElementById("bassSizeValue");
const canvas = document.getElementById("visualizer");
const ctx = canvas.getContext("2d");
const previewFrame = document.getElementById("heroPreviewFrame");
const previewCanvas = document.getElementById("heroPreviewCanvas");
const previewCtx = previewCanvas.getContext("2d");

const themes = {
  aurora: {
    name: "Aurora",
    backgroundLow: [20, 62, 120],
    backgroundMid: [34, 152, 175],
    backgroundHigh: [145, 244, 255],
    bloomA: [255, 209, 102],
    bloomB: [100, 230, 255],
    ring: [120, 238, 255],
    barsBaseHue: 190,
    barsHueRange: 70,
    wave: [255, 255, 255],
    core: [245, 252, 255]
  },
  sunset: {
    name: "Sunset",
    backgroundLow: [62, 14, 60],
    backgroundMid: [189, 56, 88],
    backgroundHigh: [255, 164, 97],
    bloomA: [255, 196, 117],
    bloomB: [255, 107, 107],
    ring: [255, 207, 125],
    barsBaseHue: 24,
    barsHueRange: 50,
    wave: [255, 236, 212],
    core: [255, 245, 223]
  },
  noir: {
    name: "Noir Pulse",
    backgroundLow: [10, 14, 24],
    backgroundMid: [33, 33, 48],
    backgroundHigh: [166, 0, 255],
    bloomA: [208, 116, 255],
    bloomB: [77, 209, 255],
    ring: [230, 242, 255],
    barsBaseHue: 275,
    barsHueRange: 110,
    wave: [214, 229, 255],
    core: [255, 255, 255]
  },
  forest: {
    name: "Forest Glow",
    backgroundLow: [7, 33, 24],
    backgroundMid: [45, 111, 77],
    backgroundHigh: [173, 255, 188],
    bloomA: [244, 211, 94],
    bloomB: [102, 255, 173],
    ring: [150, 255, 208],
    barsBaseHue: 135,
    barsHueRange: 55,
    wave: [231, 255, 240],
    core: [246, 255, 247]
  },
  glacier: {
    name: "Glacier",
    backgroundLow: [7, 22, 44],
    backgroundMid: [33, 92, 148],
    backgroundHigh: [187, 242, 255],
    bloomA: [183, 234, 255],
    bloomB: [87, 205, 255],
    ring: [212, 248, 255],
    barsBaseHue: 205,
    barsHueRange: 42,
    wave: [227, 248, 255],
    core: [245, 252, 255]
  },
  ember: {
    name: "Ember",
    backgroundLow: [31, 7, 7],
    backgroundMid: [117, 33, 18],
    backgroundHigh: [255, 143, 57],
    bloomA: [255, 212, 115],
    bloomB: [255, 94, 58],
    ring: [255, 196, 105],
    barsBaseHue: 15,
    barsHueRange: 36,
    wave: [255, 228, 193],
    core: [255, 247, 223]
  },
  volt: {
    name: "Volt",
    backgroundLow: [8, 18, 10],
    backgroundMid: [42, 88, 18],
    backgroundHigh: [225, 255, 105],
    bloomA: [246, 255, 146],
    bloomB: [89, 255, 122],
    ring: [232, 255, 173],
    barsBaseHue: 98,
    barsHueRange: 38,
    wave: [244, 255, 223],
    core: [252, 255, 242]
  },
  solstice: {
    name: "Solstice",
    backgroundLow: [26, 18, 66],
    backgroundMid: [93, 73, 193],
    backgroundHigh: [255, 191, 116],
    bloomA: [255, 210, 129],
    bloomB: [255, 131, 188],
    ring: [255, 228, 163],
    barsBaseHue: 34,
    barsHueRange: 82,
    wave: [255, 239, 223],
    core: [255, 248, 235]
  },
  rose: {
    name: "Rose Glow",
    backgroundLow: [39, 10, 26],
    backgroundMid: [132, 36, 84],
    backgroundHigh: [255, 160, 201],
    bloomA: [255, 221, 186],
    bloomB: [255, 112, 170],
    ring: [255, 214, 230],
    barsBaseHue: 330,
    barsHueRange: 42,
    wave: [255, 233, 243],
    core: [255, 247, 250]
  },
  oceanic: {
    name: "Oceanic",
    backgroundLow: [4, 25, 41],
    backgroundMid: [0, 96, 128],
    backgroundHigh: [79, 221, 217],
    bloomA: [129, 240, 223],
    bloomB: [34, 154, 255],
    ring: [173, 246, 240],
    barsBaseHue: 182,
    barsHueRange: 34,
    wave: [219, 250, 247],
    core: [238, 255, 251]
  },
  starlight: {
    name: "Starlight",
    backgroundLow: [9, 15, 42],
    backgroundMid: [47, 61, 128],
    backgroundHigh: [182, 198, 255],
    bloomA: [255, 232, 169],
    bloomB: [152, 171, 255],
    ring: [226, 232, 255],
    barsBaseHue: 220,
    barsHueRange: 64,
    wave: [239, 244, 255],
    core: [255, 252, 242]
  }
};

const sceneNames = {
  halo: "Halo Ring",
  tunnel: "Neon Tunnel",
  constellation: "Constellation",
  equalizer: "Prism Bars",
  radialBars: "Radar Bars",
  cityBars: "City Bars",
  subwoofer: "Subwoofer",
  waterfall: "Waterfall Spectrum",
  laserClub: "Laser Club",
  orbital: "Orbital Lines",
  dualSub: "Dual Sub Stack"
};

const themeOrder = Object.keys(themes);
const sceneOrder = Object.keys(sceneNames);

let audioContext;
let analyser;
let sourceNode;
let dataArray;
let stream;
let animationFrame;
let phase = 0;
let energy = 0;
let bassPulse = 0;
let bassImpact = 0;
let snareImpact = 0;
let hatEnergy = 0;
let currentSourceLabel = "None";
let activeTheme = themeOrder[0];
let activeScene = sceneOrder[0];
let autoMode = "off";
let autoTimer = null;
let beatBaseline = 0;
let lastBeatSignal = 0;
let lastReactiveChangeAt = 0;
let lastAutoAdvanceAt = 0;
let transitionFlash = 0;
let transitionTheme = "aurora";
let cinematicMode = false;
let lockTheme = false;
let hudVisible = true;
let cinematicHideTimer = null;
let phraseShortEnergy = 0;
let phraseLongEnergy = 0;
let phraseContrast = 0;
let lastPhraseAt = 0;
let previousBands = { bass: 0, mids: 0, highs: 0 };
let waterfallHistory = [];

const STORAGE_KEY = "pulse-canvas-preferences-v1";

function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const { clientWidth, clientHeight } = canvas;
  canvas.width = Math.max(1, Math.floor(clientWidth * dpr));
  canvas.height = Math.max(1, Math.floor(clientHeight * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const previewWidth = previewFrame.clientWidth;
  const previewHeight = previewFrame.clientHeight;
  previewCanvas.width = Math.max(1, Math.floor(previewWidth * dpr));
  previewCanvas.height = Math.max(1, Math.floor(previewHeight * dpr));
  previewCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function syncPreview() {
  const previewWidth = previewFrame.clientWidth;
  const previewHeight = previewFrame.clientHeight;

  if (!previewWidth || !previewHeight) {
    return;
  }

  previewCtx.clearRect(0, 0, previewWidth, previewHeight);
  previewCtx.drawImage(canvas, 0, 0, canvas.width, canvas.height, 0, 0, previewWidth, previewHeight);
}

function savePreferences() {
  const payload = {
    theme: activeTheme,
    scene: activeScene,
    autoMode,
    autoInterval: autoIntervalSelect.value,
    bassSensitivity: bassSensitivityInput.value,
    bassSize: bassSizeInput.value,
    cinematicMode,
    lockTheme
  };

  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  } catch (error) {
    console.warn("Unable to save preferences.", error);
  }
}

function loadPreferences() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return null;
    }
    return JSON.parse(raw);
  } catch (error) {
    console.warn("Unable to load preferences.", error);
    return null;
  }
}

function updateCinematicButtons() {
  const label = cinematicMode ? "Cinematic On" : "Cinematic Off";
  cinematicBtn.textContent = label;
  hudCinematicBtn.textContent = label;
  cinematicBtn.classList.toggle("is-active", cinematicMode);
  hudCinematicBtn.classList.toggle("is-active", cinematicMode);
}

function updateLockThemeButtons() {
  const label = lockTheme ? "Color Lock On" : "Color Lock Off";
  lockThemeBtn.textContent = label;
  hudLockThemeBtn.textContent = label;
  lockThemeBtn.classList.toggle("is-active", lockTheme);
  hudLockThemeBtn.classList.toggle("is-active", lockTheme);
}

function setLockTheme(nextValue) {
  lockTheme = nextValue;
  updateLockThemeButtons();
  syncAutoButtons();
  savePreferences();
}

function toggleLockTheme() {
  setLockTheme(!lockTheme);
}

function clearCinematicTimer() {
  if (cinematicHideTimer) {
    clearTimeout(cinematicHideTimer);
    cinematicHideTimer = null;
  }
}

function setHudVisible(nextVisible) {
  hudVisible = nextVisible;
  visualFrame.classList.toggle("hud-visible", nextVisible);
}

function scheduleCinematicHide() {
  clearCinematicTimer();
  if (!cinematicMode) {
    setHudVisible(true);
    return;
  }

  setHudVisible(true);
  cinematicHideTimer = setTimeout(() => {
    setHudVisible(false);
  }, 2200);
}

function setCinematicMode(nextMode) {
  cinematicMode = nextMode;
  appShell.classList.toggle("is-cinematic", cinematicMode);
  updateCinematicButtons();
  scheduleCinematicHide();
  savePreferences();
}

function nudgeCinematicHud() {
  if (!cinematicMode) {
    return;
  }
  scheduleCinematicHide();
}

function toggleCinematicMode() {
  setCinematicMode(!cinematicMode);
}
function triggerTransition(level = 1, tint = activeTheme) {
  transitionFlash = Math.max(transitionFlash, Math.min(1, 0.24 + level * 0.36));
  transitionTheme = tint;
}

function drawTransitionOverlay(width, height) {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width < 1 || height < 1) {
    return;
  }

  const flash = Math.max(transitionFlash, Math.max(0, bassImpact - 0.72) * 0.22);
  if (flash <= 0.01) {
    return;
  }

  const tintTheme = themes[transitionTheme] || themes[activeTheme];
  const sweepSpan = Math.max(width * 1.8, 1);
  const sweep = ((phase * 90) % sweepSpan) - width * 0.4;

  const wash = ctx.createLinearGradient(sweep, 0, sweep + width * 0.6, height);
  wash.addColorStop(0, rgba(tintTheme.bloomA, 0));
  wash.addColorStop(0.45, rgba(tintTheme.bloomA, 0.04 + flash * 0.16));
  wash.addColorStop(0.7, rgba(tintTheme.bloomB, 0.03 + flash * 0.14));
  wash.addColorStop(1, rgba(tintTheme.backgroundLow, 0));
  ctx.fillStyle = wash;
  ctx.fillRect(0, 0, width, height);

  const burst = ctx.createRadialGradient(width * 0.5, height * 0.5, 30, width * 0.5, height * 0.5, Math.max(width, height) * (0.2 + flash * 0.22));
  burst.addColorStop(0, rgba(tintTheme.core, 0.02 + flash * 0.12));
  burst.addColorStop(0.32, rgba(tintTheme.bloomA, 0.04 + flash * 0.16));
  burst.addColorStop(0.75, rgba(tintTheme.bloomB, 0.02 + flash * 0.1));
  burst.addColorStop(1, rgba(tintTheme.backgroundLow, 0));
  ctx.fillStyle = burst;
  ctx.fillRect(0, 0, width, height);

  ctx.strokeStyle = rgba(tintTheme.wave, 0.04 + flash * 0.12);
  ctx.lineWidth = 2 + flash * 6;
  ctx.beginPath();
  for (let x = 0; x <= width; x += 12) {
    const normalized = x / width;
    const y = height * 0.5 + Math.sin(normalized * 12 + phase * 4.2) * (10 + flash * 28);
    if (x === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();
}
function setStatus(message) {
  statusText.textContent = message;
}

function setSource(label) {
  currentSourceLabel = label;
  sourceText.textContent = label;
}

function bassSensitivityFactor() {
  return Number(bassSensitivityInput.value) / 100;
}

function bassSizeFactor() {
  return Number(bassSizeInput.value) / 100;
}

function syncBassControls() {
  bassSensitivityValue.textContent = `${bassSensitivityInput.value}%`;
  bassSizeValue.textContent = `${bassSizeInput.value}%`;
  savePreferences();
}

function setButtonsCapturing(isCapturing) {
  shareBtn.disabled = isCapturing;
  deviceBtn.disabled = isCapturing;
  stopBtn.disabled = !isCapturing;
  refreshDevicesBtn.disabled = isCapturing;
  deviceSelect.disabled = isCapturing;
}

function setTheme(themeName) {
  const changed = activeTheme !== themeName;
  activeTheme = themeName;
  hudThemeName.textContent = themes[themeName].name;
  themeButtons.forEach((button) => {
    button.classList.toggle("is-active", button.dataset.theme === themeName);
  });
  if (changed) {
    triggerTransition(0.75, themeName);
  }
  savePreferences();
}

function setScene(sceneName) {
  const changed = activeScene !== sceneName;
  activeScene = sceneName;
  const label = sceneNames[sceneName];
  sceneLabel.textContent = label;
  heroPreviewScene.textContent = label;
  hudSceneName.textContent = label;
  sceneButtons.forEach((button) => {
    button.classList.toggle("is-active", button.dataset.scene === sceneName);
  });
  if (changed) {
    triggerTransition(1, activeTheme);
  }
  savePreferences();
}

function cycleItem(order, current, direction) {
  const currentIndex = order.indexOf(current);
  const nextIndex = (currentIndex + direction + order.length) % order.length;
  return order[nextIndex];
}

function randomItem(order, current) {
  if (order.length <= 1) {
    return current;
  }

  let next = current;
  while (next === current) {
    next = order[Math.floor(Math.random() * order.length)];
  }
  return next;
}

function cycleTheme(direction) {
  setTheme(cycleItem(themeOrder, activeTheme, direction));
}

function cycleScene(direction) {
  setScene(cycleItem(sceneOrder, activeScene, direction));
}

function advanceAuto() {
  if (autoMode === "cycle") {
    cycleScene(1);
    if (!lockTheme) {
      cycleTheme(1);
    }
    lastAutoAdvanceAt = performance.now();
    return;
  }

  if (autoMode === "random") {
    setScene(randomItem(sceneOrder, activeScene));
    if (!lockTheme) {
      setTheme(randomItem(themeOrder, activeTheme));
    }
    lastAutoAdvanceAt = performance.now();
  }
}

function clearAutoTimer() {
  if (autoTimer) {
    clearInterval(autoTimer);
    autoTimer = null;
  }
}

function resetReactiveDetector() {
  beatBaseline = 0;
  lastBeatSignal = 0;
  lastReactiveChangeAt = 0;
  lastAutoAdvanceAt = 0;
  phraseShortEnergy = 0;
  phraseLongEnergy = 0;
  phraseContrast = 0;
  lastPhraseAt = 0;
  previousBands = { bass: 0, mids: 0, highs: 0 };
}

function syncAutoButtons() {
  const cycle = autoMode === "cycle";
  const random = autoMode === "random";
  const reactive = autoMode === "reactive";
  const off = autoMode === "off";

  autoOffBtn.classList.toggle("is-active", off);
  autoCycleBtn.classList.toggle("is-active", cycle);
  autoRandomBtn.classList.toggle("is-active", random);
  autoReactiveBtn.classList.toggle("is-active", reactive);
  hudAutoOffBtn.classList.toggle("is-active", off);
  hudAutoCycleBtn.classList.toggle("is-active", cycle);
  hudAutoRandomBtn.classList.toggle("is-active", random);
  hudAutoReactiveBtn.classList.toggle("is-active", reactive);

  if (off) {
    hudAutoState.textContent = lockTheme ? "Manual | Locked" : "Manual";
  } else if (reactive) {
    hudAutoState.textContent = lockTheme ? "Reactive Beat | Locked" : "Reactive Beat";
  } else {
    const modeLabel = `${autoMode === "cycle" ? "Cycle" : "Random"} ${Math.round(Number(autoIntervalSelect.value) / 1000)}s`;
    hudAutoState.textContent = lockTheme ? `${modeLabel} | Locked` : modeLabel;
  }
}

function setAutoMode(mode) {
  autoMode = mode;
  lastAutoAdvanceAt = performance.now();
  clearAutoTimer();
  resetReactiveDetector();

  if (mode === "cycle" || mode === "random") {
    autoTimer = setInterval(advanceAuto, Number(autoIntervalSelect.value));
  }

  syncAutoButtons();
  savePreferences();
}

function averageRange(array, start, end) {
  const safeStart = Math.max(0, start);
  const safeEnd = Math.min(array.length, end);
  if (safeEnd <= safeStart) {
    return 0;
  }

  let total = 0;
  for (let i = safeStart; i < safeEnd; i += 1) {
    total += array[i];
  }
  return total / (safeEnd - safeStart);
}

function detectDominantBand(bass, mids, highs) {
  if (bass > mids && bass > highs) {
    return "Bass";
  }
  if (mids > highs) {
    return "Mids";
  }
  if (highs > 0) {
    return "Treble";
  }
  return "None";
}

function maybeTriggerReactiveChange(bass, mids, highs, overall) {
  if (autoMode !== "reactive") {
    return;
  }

  const now = performance.now();
  const sensitivity = bassSensitivityFactor();
  const beatSignal = bass * (0.78 * sensitivity) + overall * 0.18 + highs * 0.08;
  const sectionSignal = overall * 0.48 + bass * 0.34 + mids * 0.18;
  phraseShortEnergy += (sectionSignal - phraseShortEnergy) * 0.16;
  phraseLongEnergy += (sectionSignal - phraseLongEnergy) * 0.028;
  const toneShift = Math.abs(bass - previousBands.bass) * 1.15 + Math.abs(mids - previousBands.mids) * 0.85 + Math.abs(highs - previousBands.highs) * 0.95;
  previousBands = { bass, mids, highs };
  phraseContrast += ((phraseShortEnergy - phraseLongEnergy) + toneShift * 0.55 - phraseContrast) * 0.14;

  beatBaseline += (beatSignal - beatBaseline) * 0.06;
  const spike = beatSignal - beatBaseline;
  const risingEdge = beatSignal - lastBeatSignal;
  lastBeatSignal = beatSignal;

  const cooldownMs = Math.max(650, 1180 - sensitivity * 190);
  const spikeThreshold = Math.max(0.026, 0.055 / sensitivity);
  const riseThreshold = Math.max(0.004, 0.011 / sensitivity);
  const bassThreshold = Math.max(0.07, 0.12 / sensitivity);
  const strongEnough = spike > spikeThreshold && risingEdge > riseThreshold && bass > bassThreshold;

  const phraseThreshold = Math.max(0.04, 0.078 / sensitivity);
  const phraseChange = phraseContrast > phraseThreshold && toneShift > 0.085 && overall > 0.09 && now - lastPhraseAt > 6800;

  if (phraseChange) {
    const dominantBand = detectDominantBand(bass, mids, highs);
    let scenePool;
    if (dominantBand === "Bass") {
      scenePool = ["subwoofer", "dualSub", "waterfall", "radialBars", "cityBars", "equalizer", "laserClub"];
    } else if (dominantBand === "Mids") {
      scenePool = ["waterfall", "tunnel", "constellation", "equalizer", "orbital"];
    } else {
      scenePool = ["waterfall", "constellation", "halo", "tunnel", "orbital"];
    }

    setScene(randomItem(scenePool, activeScene));
    triggerTransition(1.2, activeTheme);
    if (!lockTheme && (toneShift > 0.14 || highs > 0.24 || mids > 0.24)) {
      setTheme(randomItem(themeOrder, activeTheme));
    }
    lastPhraseAt = now;
    lastReactiveChangeAt = now;
    lastAutoAdvanceAt = now;
    return;
  }

  if (strongEnough && now - lastReactiveChangeAt > cooldownMs) {
    const heavyBass = bass > 0.24 / sensitivity;
    const nextScene = heavyBass
      ? randomItem(["equalizer", "radialBars", "cityBars", "subwoofer", "dualSub", "tunnel", "waterfall", "laserClub"], activeScene)
      : randomItem(sceneOrder, activeScene);
    setScene(nextScene);

    if (!lockTheme && (spike > spikeThreshold * 1.5 || mids > 0.2 || highs > 0.24)) {
      setTheme(randomItem(themeOrder, activeTheme));
    }

    lastReactiveChangeAt = now;
    lastAutoAdvanceAt = now;
    return;
  }

  const fallbackMs = Math.max(5000, 9000 - sensitivity * 1600);
  if (now - lastAutoAdvanceAt > fallbackMs && overall > 0.05) {
    setScene(randomItem(sceneOrder, activeScene));
    if (!lockTheme && (mids > 0.16 || bass > 0.12)) {
      setTheme(randomItem(themeOrder, activeTheme));
    }
    lastAutoAdvanceAt = now;
  }
}

function mixChannel(base, peak, amount) {
  return Math.round(base + (peak - base) * amount);
}

function rgba(values, alpha) {
  return `rgba(${values[0]}, ${values[1]}, ${values[2]}, ${alpha})`;
}

function clearBackground(width, height, theme, bass, mids, highs) {
  const background = ctx.createLinearGradient(0, 0, width, height);
  background.addColorStop(0, `rgba(${mixChannel(theme.backgroundLow[0], theme.backgroundHigh[0], highs)}, ${mixChannel(theme.backgroundLow[1], theme.backgroundHigh[1], mids)}, ${mixChannel(theme.backgroundLow[2], theme.backgroundHigh[2], bass)}, 1)`);
  background.addColorStop(0.4, `rgba(${theme.backgroundMid[0]}, ${theme.backgroundMid[1]}, ${theme.backgroundMid[2]}, 0.96)`);
  background.addColorStop(1, rgba(theme.backgroundLow, 1));
  ctx.fillStyle = background;
  ctx.fillRect(0, 0, width, height);
}

function drawBloom(width, height, theme, bass, mids) {
  const bloom = ctx.createRadialGradient(
    width * 0.5,
    height * 0.5,
    40,
    width * 0.5,
    height * 0.5,
    Math.max(width, height) * (0.18 + bass * 0.24)
  );
  bloom.addColorStop(0, rgba(theme.bloomA, 0.16 + bass * 0.35));
  bloom.addColorStop(0.4, rgba(theme.bloomB, 0.1 + mids * 0.28));
  bloom.addColorStop(1, rgba(theme.backgroundLow, 0));
  ctx.fillStyle = bloom;
  ctx.fillRect(0, 0, width, height);
}

function drawSubwooferFloor(width, height, theme) {
  const floorHeight = height * (0.16 + bassImpact * 0.08);
  const top = height - floorHeight;
  const floorGlow = ctx.createLinearGradient(0, top, 0, height);
  floorGlow.addColorStop(0, rgba(theme.bloomA, 0.02 + bassImpact * 0.12));
  floorGlow.addColorStop(0.5, rgba(theme.bloomB, 0.08 + bassPulse * 0.16));
  floorGlow.addColorStop(1, rgba(theme.backgroundLow, 0.34 + bassImpact * 0.18));
  ctx.fillStyle = floorGlow;
  ctx.fillRect(0, top, width, floorHeight);

  ctx.strokeStyle = rgba(theme.wave, 0.08 + bassImpact * 0.16);
  ctx.lineWidth = 1.5 + bassImpact * 4;
  ctx.beginPath();
  for (let x = 0; x <= width; x += 14) {
    const normalized = x / width;
    const y = top + Math.sin(normalized * 10 + phase * 1.6) * (4 + bassImpact * 14);
    if (x === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();
}
function drawTrebleAccents(width, height, theme) {
  const sparkle = Math.max(hatEnergy, snareImpact * 0.35);
  if (sparkle <= 0.01) {
    return;
  }

  ctx.strokeStyle = rgba(theme.wave, 0.03 + snareImpact * 0.16);
  ctx.lineWidth = 1.5 + snareImpact * 5;
  ctx.beginPath();
  ctx.moveTo(0, height * (0.5 - snareImpact * 0.06));
  ctx.lineTo(width, height * (0.5 + snareImpact * 0.06));
  ctx.stroke();

  ctx.lineWidth = 1 + sparkle * 2.5;
  for (let i = 0; i < 9; i += 1) {
    const x = ((i + 1) / 10) * width + Math.sin(phase * 2.2 + i * 1.6) * width * 0.015;
    const y = height * (0.12 + (i % 3) * 0.08) + Math.cos(phase * 3.4 + i) * 12;
    const length = 16 + hatEnergy * 42 + (i % 2 === 0 ? snareImpact * 18 : 0);
    ctx.strokeStyle = rgba(i % 2 === 0 ? theme.bloomB : theme.wave, 0.05 + sparkle * 0.16);
    ctx.beginPath();
    ctx.moveTo(x - length * 0.5, y - length * 0.3);
    ctx.lineTo(x + length * 0.5, y + length * 0.3);
    ctx.stroke();
  }

  ctx.fillStyle = rgba(theme.core, 0.03 + hatEnergy * 0.1);
  for (let i = 0; i < 18; i += 1) {
    const x = (i / 17) * width;
    const y = height * (0.08 + (i % 4) * 0.06) + Math.sin(phase * 5 + i) * (4 + hatEnergy * 10);
    const size = 1 + hatEnergy * 2.6 + (i % 5 === 0 ? snareImpact * 2.4 : 0);
    ctx.beginPath();
    ctx.arc(x, y, size, 0, Math.PI * 2);
    ctx.fill();
  }
}
function drawBassAccent(width, height, theme, bass, overall) {
  const centerX = width / 2;
  const centerY = height / 2;
  const sizeFactor = bassSizeFactor();
  const punch = bassImpact * sizeFactor;
  const pulseSize = Math.max(width, height) * (0.07 + bassPulse * 0.18 * sizeFactor + punch * 0.12 + bass * 0.05 * sizeFactor);

  const flash = ctx.createRadialGradient(
    centerX,
    centerY,
    16,
    centerX,
    centerY,
    pulseSize
  );
  flash.addColorStop(0, rgba(theme.core, 0.08 + punch * 0.24));
  flash.addColorStop(0.22, rgba(theme.bloomA, 0.12 + bassPulse * 0.22 * sizeFactor + punch * 0.18));
  flash.addColorStop(0.52, rgba(theme.bloomB, 0.05 + bassPulse * 0.16 * sizeFactor));
  flash.addColorStop(1, rgba(theme.backgroundLow, 0));
  ctx.fillStyle = flash;
  ctx.fillRect(0, 0, width, height);

  const coneRadius = Math.min(width, height) * (0.1 + bassPulse * 0.08 * sizeFactor + punch * 0.07);
  const coneGradient = ctx.createRadialGradient(centerX, centerY, coneRadius * 0.18, centerX, centerY, coneRadius);
  coneGradient.addColorStop(0, rgba(theme.core, 0.18 + punch * 0.24));
  coneGradient.addColorStop(0.4, rgba(theme.bloomA, 0.12 + punch * 0.18));
  coneGradient.addColorStop(0.72, rgba(theme.bloomB, 0.06 + bassPulse * 0.12));
  coneGradient.addColorStop(1, rgba(theme.backgroundLow, 0));
  ctx.fillStyle = coneGradient;
  ctx.beginPath();
  ctx.arc(centerX, centerY, coneRadius, 0, Math.PI * 2);
  ctx.fill();

  ctx.strokeStyle = rgba(theme.bloomA, 0.12 + punch * 0.26);
  ctx.lineWidth = 2 + punch * 8;
  ctx.beginPath();
  ctx.arc(centerX, centerY, Math.min(width, height) * (0.16 + bassPulse * 0.12 * sizeFactor + punch * 0.11 + overall * 0.03), 0, Math.PI * 2);
  ctx.stroke();

  ctx.strokeStyle = rgba(theme.wave, 0.05 + punch * 0.12);
  ctx.lineWidth = 1.5 + punch * 4;
  ctx.beginPath();
  ctx.arc(centerX, centerY, Math.min(width, height) * (0.22 + punch * 0.16), 0, Math.PI * 2);
  ctx.stroke();
}
function renderIdle() {
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const theme = themes[activeTheme];

  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = rgba(theme.backgroundLow, 1);
  ctx.fillRect(0, 0, width, height);
  drawBloom(width, height, theme, 0.4, 0.2);

  ctx.fillStyle = "rgba(237, 244, 255, 0.78)";
  ctx.font = "600 30px Georgia";
  ctx.fillText("Waiting for audio capture", 38, height / 2 - 8);
  ctx.fillStyle = "rgba(158, 180, 209, 0.9)";
  ctx.font = "18px Georgia";
  ctx.fillText("Choose browser share or a loopback input device to drive the scene.", 38, height / 2 + 28);
  syncPreview();
}

function renderHaloScene(width, height, theme, bass, mids, highs, overall) {
  drawBloom(width, height, theme, bass, mids);
  const centerX = width / 2;
  const centerY = height / 2;
  const baseRadius = Math.min(width, height) * 0.16;
  const ringRadius = baseRadius + bass * 110;
  const ringThickness = 14 + overall * 22;

  ctx.lineWidth = ringThickness;
  ctx.strokeStyle = rgba(theme.ring, 0.28 + highs * 0.55);
  ctx.beginPath();
  for (let angle = 0; angle <= Math.PI * 2 + 0.1; angle += 0.06) {
    const wave = Math.sin(angle * 5 + phase * 2.4) * mids * 18;
    const pulse = Math.sin(angle * 2 - phase) * highs * 12;
    const radius = ringRadius + wave + pulse + Math.cos(angle * 2 + phase * 1.4) * bassImpact * 12 * bassSizeFactor();
    const x = centerX + Math.cos(angle) * radius;
    const y = centerY + Math.sin(angle) * radius;
    if (angle === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();

  ctx.strokeStyle = rgba(theme.wave, 0.28 + overall * 0.2);
  ctx.lineWidth = 2;
  ctx.beginPath();
  for (let x = 0; x < width; x += 8) {
    const normalized = x / width;
    const y = centerY + Math.sin(normalized * 18 + phase * 3.6) * (18 + mids * 64) + Math.cos(normalized * 7 - phase * 2.2) * (8 + highs * 34);
    if (x === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();

  ctx.fillStyle = rgba(theme.core, 0.12 + overall * 0.35);
  ctx.beginPath();
  ctx.arc(centerX, centerY, baseRadius * (0.7 + overall * 0.9), 0, Math.PI * 2);
  ctx.fill();
}

function renderTunnelScene(width, height, theme, bass, mids, highs, overall) {
  drawBloom(width, height, theme, bass, mids);
  const centerX = width / 2;
  const centerY = height / 2;
  const ringCount = 16;
  for (let i = ringCount; i >= 1; i -= 1) {
    const depth = i / ringCount;
    const radius = Math.min(width, height) * 0.08 + depth * Math.min(width, height) * (0.44 + bass * 0.18);
    const wobble = Math.sin(phase * 2 + i * 0.8) * mids * 24;
    ctx.strokeStyle = rgba(theme.ring, 0.05 + depth * 0.14 + highs * 0.16);
    ctx.lineWidth = 2 + depth * 5;
    ctx.beginPath();
    for (let angle = 0; angle <= Math.PI * 2 + 0.08; angle += 0.12) {
      const distortion = Math.sin(angle * 6 + phase * 2.8 + i) * (6 + highs * 18) + wobble;
      const x = centerX + Math.cos(angle) * (radius + distortion);
      const y = centerY + Math.sin(angle) * (radius + distortion * 0.55);
      if (angle === 0) {
        ctx.moveTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
    }
    ctx.stroke();
  }

  for (let i = 0; i < 24; i += 1) {
    const angle = (Math.PI * 2 * i) / 24 + phase * 0.3;
    const length = Math.min(width, height) * (0.18 + highs * 0.3);
    ctx.strokeStyle = rgba(theme.bloomB, 0.14 + overall * 0.22);
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(centerX, centerY);
    ctx.lineTo(centerX + Math.cos(angle) * length, centerY + Math.sin(angle) * length);
    ctx.stroke();
  }
}

function renderConstellationScene(width, height, theme, bass, mids, highs, overall) {
  drawBloom(width, height, theme, bass, mids);
  const points = [];
  const count = 80;
  for (let i = 0; i < count; i += 1) {
    const t = i / count;
    const x = (Math.sin(t * 17 + phase * (0.7 + bass)) * 0.38 + 0.5 + Math.cos(t * 7 - phase) * 0.08) * width;
    const y = (Math.cos(t * 13 - phase * (0.5 + highs)) * 0.34 + 0.5 + Math.sin(t * 5 + phase * 0.8) * 0.1) * height;
    const size = 1.5 + mids * 4 + (i % 5 === 0 ? bass * 7 : 0);
    points.push({ x, y, size });
  }

  for (let i = 0; i < points.length; i += 1) {
    const a = points[i];
    for (let j = i + 1; j < Math.min(points.length, i + 4); j += 1) {
      const b = points[j];
      const distance = Math.hypot(a.x - b.x, a.y - b.y);
      if (distance < 160 + highs * 120) {
        ctx.strokeStyle = rgba(theme.wave, 0.03 + (1 - distance / 280) * (0.12 + overall * 0.18));
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
    }
  }

  points.forEach((point, index) => {
    ctx.fillStyle = rgba(index % 3 === 0 ? theme.bloomA : theme.bloomB, 0.35 + overall * 0.4);
    ctx.beginPath();
    ctx.arc(point.x, point.y, point.size, 0, Math.PI * 2);
    ctx.fill();
  });
}

function renderEqualizerScene(width, height, theme, bass, mids, highs, overall) {
  drawBloom(width, height, theme, bass, mids);
  const columns = 72;
  const spacing = width / columns;
  for (let i = 0; i < columns; i += 1) {
    const bucket = Math.min(dataArray.length - 1, Math.floor(i * (dataArray.length / columns)));
    const value = dataArray[bucket] / 255;
    const x = i * spacing;
    const sizeFactor = bassSizeFactor();
    const lowBandBoost = i < Math.floor(columns * 0.22) ? 1 + bassPulse * 1.1 * sizeFactor + bassImpact * 1.2 * sizeFactor : 1 + bassPulse * 0.2 * sizeFactor;
    const barHeight = value * height * 0.55 * lowBandBoost;
    const lift = Math.sin(phase * 2 + i * 0.28) * highs * 24;
    const hue = theme.barsBaseHue - value * theme.barsHueRange + Math.sin(i * 0.2 + phase) * 18;
    ctx.fillStyle = `hsla(${hue}, 92%, ${54 + value * 22}%, ${0.42 + value * 0.44})`;
    const kickDrop = i < Math.floor(columns * 0.18) ? bassImpact * 36 * sizeFactor : 0;
    ctx.fillRect(x, height - barHeight - 32 + lift - kickDrop, Math.max(4, spacing - 4), barHeight + kickDrop);
    ctx.fillRect(x, 32 - lift, Math.max(4, spacing - 4), barHeight * 0.32);
  }

  ctx.strokeStyle = rgba(theme.ring, 0.26 + overall * 0.34);
  ctx.lineWidth = 3;
  ctx.beginPath();
  for (let x = 0; x <= width; x += 10) {
    const normalized = x / width;
    const y = height * 0.5 + Math.sin(normalized * 14 + phase * 3.2) * (20 + bass * 72) + Math.cos(normalized * 11 - phase * 1.8) * (8 + mids * 28);
    if (x === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();
}

function renderRadialBarsScene(width, height, theme, bass, mids, highs, overall) {
  drawBloom(width, height, theme, bass, mids);
  const centerX = width / 2;
  const centerY = height / 2;
  const barCount = 96;
  const innerRadius = Math.min(width, height) * (0.15 + bass * 0.04 - bassImpact * 0.018 * bassSizeFactor());

  for (let i = 0; i < barCount; i += 1) {
    const angle = (Math.PI * 2 * i) / barCount + phase * 0.22;
    const bucket = Math.min(dataArray.length - 1, Math.floor(i * (dataArray.length / barCount)));
    const value = dataArray[bucket] / 255;
    const sizeFactor = bassSizeFactor();
    const radialBoost = i < Math.floor(barCount * 0.25) ? 1 + bassPulse * 0.9 * sizeFactor + bassImpact * 0.95 * sizeFactor : 1 + bassPulse * 0.2 * sizeFactor;
    const length = 30 + value * Math.min(width, height) * 0.26 * radialBoost + bassImpact * 12 * sizeFactor;
    const x1 = centerX + Math.cos(angle) * innerRadius;
    const y1 = centerY + Math.sin(angle) * innerRadius;
    const x2 = centerX + Math.cos(angle) * (innerRadius + length);
    const y2 = centerY + Math.sin(angle) * (innerRadius + length);
    const hue = theme.barsBaseHue - value * theme.barsHueRange + i * 0.9;
    ctx.strokeStyle = `hsla(${hue}, 94%, ${56 + value * 20}%, ${0.34 + value * 0.5})`;
    ctx.lineWidth = 2 + value * 7;
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
  }

  ctx.strokeStyle = rgba(theme.ring, 0.24 + highs * 0.4);
  ctx.lineWidth = 4 + overall * 6;
  ctx.beginPath();
  ctx.arc(centerX, centerY, innerRadius - 12, 0, Math.PI * 2);
  ctx.stroke();

  ctx.fillStyle = rgba(theme.core, 0.14 + overall * 0.28);
  ctx.beginPath();
  ctx.arc(centerX, centerY, innerRadius * (0.55 + overall * 0.24), 0, Math.PI * 2);
  ctx.fill();
}

function renderSubwooferScene(width, height, theme, bass, mids, highs, overall) {
  drawBloom(width, height, theme, bass, mids);
  const centerX = width / 2;
  const centerY = height * 0.54;
  const sizeFactor = bassSizeFactor();
  const punch = bassImpact * sizeFactor;
  const shimmer = Math.max(mids * 0.75, highs * 0.9, hatEnergy * 0.85, snareImpact * 0.45);
  const outerRadius = Math.min(width, height) * (0.24 + bassPulse * 0.03 * sizeFactor + punch * 0.05);
  const surroundRadius = outerRadius * (0.86 + bassPulse * 0.05 * sizeFactor + punch * 0.04 + shimmer * 0.015);
  const coneRadius = outerRadius * (0.62 + bassPulse * 0.16 * sizeFactor - punch * 0.2 + shimmer * 0.028);
  const dustRadius = coneRadius * (0.22 + punch * 0.18 + shimmer * 0.06);
  const rippleReach = outerRadius * (1.25 + bassPulse * 0.32 * sizeFactor + punch * 0.45 + shimmer * 0.08);

  const backGlow = ctx.createRadialGradient(centerX, centerY, dustRadius * 0.2, centerX, centerY, rippleReach * 1.8);
  backGlow.addColorStop(0, rgba(theme.bloomA, 0.18 + punch * 0.14));
  backGlow.addColorStop(0.28, rgba(theme.bloomB, 0.12 + bassPulse * 0.14 + shimmer * 0.08));
  backGlow.addColorStop(0.7, rgba(theme.backgroundMid, 0.08 + overall * 0.08));
  backGlow.addColorStop(1, rgba(theme.backgroundLow, 0));
  ctx.fillStyle = backGlow;
  ctx.fillRect(0, 0, width, height);

  for (let i = 0; i < 5; i += 1) {
    const rippleRadius = rippleReach * (0.92 + i * 0.18 + punch * 0.1 + shimmer * 0.025);
    ctx.strokeStyle = rgba(i % 2 === 0 ? theme.bloomA : theme.bloomB, Math.max(0, 0.16 + punch * 0.14 + shimmer * 0.05 - i * 0.028));
    ctx.lineWidth = Math.max(1.5, 6 - i + punch * 5 + shimmer * 1.8);
    ctx.beginPath();
    ctx.arc(centerX, centerY, rippleRadius, 0, Math.PI * 2);
    ctx.stroke();
  }

  const frameGradient = ctx.createRadialGradient(centerX, centerY, outerRadius * 0.5, centerX, centerY, outerRadius * 1.08);
  frameGradient.addColorStop(0, rgba(theme.backgroundLow, 0.22));
  frameGradient.addColorStop(0.72, rgba(theme.backgroundMid, 0.82));
  frameGradient.addColorStop(1, rgba(theme.backgroundLow, 0.96));
  ctx.fillStyle = frameGradient;
  ctx.beginPath();
  ctx.arc(centerX, centerY, outerRadius * 1.04, 0, Math.PI * 2);
  ctx.fill();

  ctx.strokeStyle = rgba(theme.ring, 0.2 + overall * 0.18 + punch * 0.12 + shimmer * 0.06);
  ctx.lineWidth = 16 + punch * 10 + shimmer * 3;
  ctx.beginPath();
  ctx.arc(centerX, centerY, outerRadius, 0, Math.PI * 2);
  ctx.stroke();

  ctx.strokeStyle = rgba(theme.wave, 0.1 + bassPulse * 0.18 + punch * 0.16 + shimmer * 0.08);
  ctx.lineWidth = 10 + bassPulse * 14 * sizeFactor + punch * 10 + shimmer * 4;
  ctx.beginPath();
  ctx.arc(centerX, centerY, surroundRadius, 0, Math.PI * 2);
  ctx.stroke();

  const coneGradient = ctx.createRadialGradient(centerX - outerRadius * 0.18, centerY - outerRadius * 0.22, dustRadius * 0.2, centerX, centerY, coneRadius);
  coneGradient.addColorStop(0, rgba(theme.core, 0.28 + punch * 0.16 + shimmer * 0.06));
  coneGradient.addColorStop(0.34, rgba(theme.bloomA, 0.18 + bassPulse * 0.14 + mids * 0.1));
  coneGradient.addColorStop(0.72, rgba(theme.bloomB, 0.14 + punch * 0.1 + highs * 0.08));
  coneGradient.addColorStop(1, rgba(theme.backgroundMid, 0.92));
  ctx.fillStyle = coneGradient;
  ctx.beginPath();
  ctx.arc(centerX, centerY, coneRadius, 0, Math.PI * 2);
  ctx.fill();

  ctx.strokeStyle = rgba(theme.wave, 0.12 + punch * 0.24 + shimmer * 0.08);
  ctx.lineWidth = 5 + punch * 9 + shimmer * 3;
  ctx.beginPath();
  ctx.arc(centerX, centerY, coneRadius * (0.96 + punch * 0.06), 0, Math.PI * 2);
  ctx.stroke();

  ctx.strokeStyle = rgba(theme.wave, 0.06 + highs * 0.12 + hatEnergy * 0.1);
  ctx.lineWidth = 1.2 + shimmer * 1.8;
  for (let i = 0; i < 4; i += 1) {
    ctx.beginPath();
    ctx.arc(centerX, centerY, coneRadius * (0.56 + i * 0.1 + Math.sin(phase * 2.2 + i) * 0.012 + shimmer * 0.016), 0, Math.PI * 2);
    ctx.stroke();
  }

  ctx.strokeStyle = rgba(theme.bloomB, 0.06 + shimmer * 0.12);
  ctx.lineWidth = 1.2 + highs * 1.8;
  for (let i = 0; i < 10; i += 1) {
    const angle = (Math.PI * 2 * i) / 10 + phase * (0.55 + shimmer * 0.12);
    const inner = dustRadius * (1.2 + Math.sin(angle * 2 + phase) * 0.06);
    const outer = coneRadius * (0.88 + Math.cos(angle * 3 - phase) * 0.05);
    ctx.beginPath();
    ctx.moveTo(centerX + Math.cos(angle) * inner, centerY + Math.sin(angle) * inner);
    ctx.lineTo(centerX + Math.cos(angle) * outer, centerY + Math.sin(angle) * outer);
    ctx.stroke();
  }

  ctx.fillStyle = rgba(theme.core, 0.2 + punch * 0.28 + bassPulse * 0.08 + shimmer * 0.08);
  ctx.beginPath();
  ctx.arc(centerX, centerY, dustRadius, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = rgba(theme.bloomA, 0.08 + shimmer * 0.1);
  for (let i = 0; i < 14; i += 1) {
    const angle = (Math.PI * 2 * i) / 14 + phase * (0.8 + highs * 0.3);
    const radius = surroundRadius * (1.05 + Math.sin(i + phase * 1.6) * 0.025);
    const x = centerX + Math.cos(angle) * radius;
    const y = centerY + Math.sin(angle) * radius;
    const size = 1.2 + shimmer * 2 + (i % 4 === 0 ? snareImpact * 1.4 : 0);
    ctx.beginPath();
    ctx.arc(x, y, size, 0, Math.PI * 2);
    ctx.fill();
  }

  ctx.strokeStyle = rgba(theme.bloomA, 0.12 + bassImpact * 0.18 + shimmer * 0.06);
  ctx.lineWidth = 2 + punch * 5;
  for (let i = 0; i < 3; i += 1) {
    ctx.beginPath();
    ctx.arc(centerX, centerY, outerRadius * (1.12 + i * 0.14 + punch * 0.08 + shimmer * 0.02), 0, Math.PI * 2);
    ctx.stroke();
  }

  ctx.strokeStyle = rgba(theme.wave, 0.08 + overall * 0.12 + mids * 0.08);
  ctx.lineWidth = 2;
  ctx.beginPath();
  for (let x = 0; x <= width; x += 12) {
    const normalized = x / width;
    const y = centerY + Math.sin(normalized * 10 + phase * 1.8) * (12 + bassImpact * 30) + Math.cos(normalized * 6 - phase) * (4 + mids * 14 + shimmer * 10);
    if (x === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();
}
function renderDualSubScene(width, height, theme, bass, mids, highs, overall) {
  drawBloom(width, height, theme, bass, mids);
  const centerX = width / 2;
  const topY = height * 0.33;
  const bottomY = height * 0.69;
  const sizeFactor = bassSizeFactor();
  const shimmer = Math.max(mids * 0.72, highs * 0.88, hatEnergy * 0.8, snareImpact * 0.4);
  const topPunch = Math.max(0, bassImpact * 0.78 + Math.sin(phase * 1.8) * 0.05);
  const bottomPunch = Math.max(0, bassImpact * 1.05 + Math.cos(phase * 1.4 + 0.9) * 0.06);
  const cabinetGlow = ctx.createLinearGradient(0, 0, width, height);
  cabinetGlow.addColorStop(0, rgba(theme.backgroundLow, 0.12));
  cabinetGlow.addColorStop(0.5, rgba(theme.bloomA, 0.06 + bassImpact * 0.08));
  cabinetGlow.addColorStop(1, rgba(theme.backgroundLow, 0.16));
  ctx.fillStyle = cabinetGlow;
  ctx.fillRect(0, 0, width, height);

  function drawDriver(centerY, punch, scale, accentFlip) {
    const outerRadius = Math.min(width, height) * (0.19 * scale + bassPulse * 0.024 * sizeFactor + punch * 0.04);
    const surroundRadius = outerRadius * (0.87 + bassPulse * 0.04 * sizeFactor + punch * 0.03 + shimmer * 0.012);
    const coneRadius = outerRadius * (0.64 + bassPulse * 0.14 * sizeFactor - punch * 0.17 + shimmer * 0.02);
    const dustRadius = coneRadius * (0.21 + punch * 0.16 + shimmer * 0.05);
    const waveRadius = outerRadius * (1.22 + bassPulse * 0.2 * sizeFactor + punch * 0.26 + shimmer * 0.05);
    const primary = accentFlip ? theme.bloomB : theme.bloomA;
    const secondary = accentFlip ? theme.bloomA : theme.bloomB;

    const glow = ctx.createRadialGradient(centerX, centerY, dustRadius * 0.2, centerX, centerY, waveRadius * 1.55);
    glow.addColorStop(0, rgba(primary, 0.14 + punch * 0.12));
    glow.addColorStop(0.34, rgba(secondary, 0.1 + bassPulse * 0.1 + shimmer * 0.06));
    glow.addColorStop(1, rgba(theme.backgroundLow, 0));
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, width, height);

    for (let i = 0; i < 4; i += 1) {
      ctx.strokeStyle = rgba(i % 2 === 0 ? primary : secondary, Math.max(0, 0.14 + punch * 0.12 - i * 0.026));
      ctx.lineWidth = Math.max(1.2, 5 - i + punch * 4);
      ctx.beginPath();
      ctx.arc(centerX, centerY, waveRadius * (0.96 + i * 0.18 + punch * 0.05), 0, Math.PI * 2);
      ctx.stroke();
    }

    const frameGradient = ctx.createRadialGradient(centerX, centerY, outerRadius * 0.46, centerX, centerY, outerRadius * 1.08);
    frameGradient.addColorStop(0, rgba(theme.backgroundLow, 0.24));
    frameGradient.addColorStop(0.72, rgba(theme.backgroundMid, 0.82));
    frameGradient.addColorStop(1, rgba(theme.backgroundLow, 0.98));
    ctx.fillStyle = frameGradient;
    ctx.beginPath();
    ctx.arc(centerX, centerY, outerRadius * 1.05, 0, Math.PI * 2);
    ctx.fill();

    ctx.strokeStyle = rgba(theme.ring, 0.18 + overall * 0.16 + punch * 0.1);
    ctx.lineWidth = 14 + punch * 9;
    ctx.beginPath();
    ctx.arc(centerX, centerY, outerRadius, 0, Math.PI * 2);
    ctx.stroke();

    ctx.strokeStyle = rgba(theme.wave, 0.1 + bassPulse * 0.16 + punch * 0.15 + shimmer * 0.06);
    ctx.lineWidth = 9 + bassPulse * 12 * sizeFactor + punch * 8;
    ctx.beginPath();
    ctx.arc(centerX, centerY, surroundRadius, 0, Math.PI * 2);
    ctx.stroke();

    const coneGradient = ctx.createRadialGradient(centerX - outerRadius * 0.18, centerY - outerRadius * 0.2, dustRadius * 0.2, centerX, centerY, coneRadius);
    coneGradient.addColorStop(0, rgba(theme.core, 0.26 + punch * 0.14 + shimmer * 0.05));
    coneGradient.addColorStop(0.34, rgba(primary, 0.18 + bassPulse * 0.12 + mids * 0.08));
    coneGradient.addColorStop(0.72, rgba(secondary, 0.14 + highs * 0.08 + punch * 0.08));
    coneGradient.addColorStop(1, rgba(theme.backgroundMid, 0.92));
    ctx.fillStyle = coneGradient;
    ctx.beginPath();
    ctx.arc(centerX, centerY, coneRadius, 0, Math.PI * 2);
    ctx.fill();

    ctx.strokeStyle = rgba(theme.wave, 0.1 + punch * 0.2 + shimmer * 0.08);
    ctx.lineWidth = 4 + punch * 8 + shimmer * 2;
    ctx.beginPath();
    ctx.arc(centerX, centerY, coneRadius * (0.96 + punch * 0.05), 0, Math.PI * 2);
    ctx.stroke();

    ctx.fillStyle = rgba(theme.core, 0.18 + punch * 0.24 + bassPulse * 0.08 + shimmer * 0.05);
    ctx.beginPath();
    ctx.arc(centerX, centerY, dustRadius, 0, Math.PI * 2);
    ctx.fill();

    ctx.strokeStyle = rgba(secondary, 0.06 + shimmer * 0.1);
    ctx.lineWidth = 1.2 + highs * 1.4;
    for (let i = 0; i < 8; i += 1) {
      const angle = (Math.PI * 2 * i) / 8 + phase * (0.62 + shimmer * 0.08) + (accentFlip ? 0.2 : -0.2);
      const inner = dustRadius * (1.18 + Math.sin(angle * 2 + phase) * 0.05);
      const outer = coneRadius * (0.88 + Math.cos(angle * 3 - phase) * 0.05);
      ctx.beginPath();
      ctx.moveTo(centerX + Math.cos(angle) * inner, centerY + Math.sin(angle) * inner);
      ctx.lineTo(centerX + Math.cos(angle) * outer, centerY + Math.sin(angle) * outer);
      ctx.stroke();
    }
  }

  drawDriver(topY, topPunch, 0.96, false);
  drawDriver(bottomY, bottomPunch, 1.06, true);

  ctx.strokeStyle = rgba(theme.wave, 0.08 + overall * 0.12 + mids * 0.08);
  ctx.lineWidth = 2.4;
  ctx.beginPath();
  for (let x = 0; x <= width; x += 12) {
    const normalized = x / width;
    const y = height * 0.5 + Math.sin(normalized * 9 + phase * 1.7) * (10 + bassImpact * 22) + Math.cos(normalized * 5 - phase) * (6 + mids * 12 + shimmer * 8);
    if (x === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();
}
function renderWaterfallScene(width, height, theme, bass, mids, highs, overall) {
  drawBloom(width, height, theme, bass, mids);

  const rowCount = 78;
  const bandCount = 92;
  const nextRow = [];
  for (let i = 0; i < bandCount; i += 1) {
    const bucket = Math.min(dataArray.length - 1, Math.floor(i * (dataArray.length / bandCount)));
    const value = dataArray[bucket] / 255;
    const weighted = i < Math.floor(bandCount * 0.2)
      ? Math.min(1, value * (1.14 + bassImpact * 0.45))
      : value;
    nextRow.push(weighted);
  }

  waterfallHistory.unshift(nextRow);
  if (waterfallHistory.length > rowCount) {
    waterfallHistory.length = rowCount;
  }

  const rowHeight = height / rowCount;
  const columnWidth = width / bandCount;
  for (let row = 0; row < waterfallHistory.length; row += 1) {
    const rowData = waterfallHistory[row];
    const y = row * rowHeight;
    const fade = 1 - row / rowCount;
    for (let i = 0; i < rowData.length; i += 1) {
      const value = rowData[i];
      const hue = theme.barsBaseHue - value * theme.barsHueRange + i * 0.72 + row * 0.35 + phase * 20;
      const lightness = 18 + value * 54 + fade * 8;
      const alpha = 0.06 + value * 0.56 * fade;
      ctx.fillStyle = `hsla(${hue}, 94%, ${lightness}%, ${alpha})`;
      ctx.fillRect(i * columnWidth, y, Math.ceil(columnWidth + 1), Math.ceil(rowHeight + 1));
    }
  }

  const sweep = ctx.createLinearGradient(0, 0, width, height);
  sweep.addColorStop(0, rgba(theme.backgroundLow, 0.12));
  sweep.addColorStop(0.45, rgba(theme.bloomA, 0.04 + phraseContrast * 0.3));
  sweep.addColorStop(1, rgba(theme.backgroundLow, 0.22));
  ctx.fillStyle = sweep;
  ctx.fillRect(0, 0, width, height);

  ctx.strokeStyle = rgba(theme.wave, 0.05 + highs * 0.18 + hatEnergy * 0.12);
  ctx.lineWidth = 1.4 + hatEnergy * 1.8;
  ctx.beginPath();
  for (let x = 0; x <= width; x += 12) {
    const normalized = x / width;
    const y = height * 0.18 + Math.sin(normalized * 18 + phase * 3.4) * (10 + highs * 18 + phraseContrast * 24);
    if (x === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();

  ctx.strokeStyle = rgba(theme.ring, 0.14 + overall * 0.26);
  ctx.lineWidth = 2 + bassImpact * 3;
  ctx.strokeRect(14, 14, width - 28, height - 28);
}
function renderLaserClubScene(width, height, theme, bass, mids, highs, overall) {
  drawBloom(width, height, theme, bass, mids);
  const centerX = width / 2;
  const horizonY = height * 0.74;
  const beamCount = 18;
  const sweepEnergy = Math.max(highs * 0.9, mids * 0.6, hatEnergy * 0.85, snareImpact * 0.5);
  const sweepPhase = Math.sin(phase * (1.4 + sweepEnergy * 0.5));
  const inversePhase = Math.cos(phase * (1.15 + sweepEnergy * 0.38));
  const fanWidth = width * (0.48 + mids * 0.24 + highs * 0.18 + sweepEnergy * 0.12);
  const headX = centerX + sweepPhase * width * (0.18 + sweepEnergy * 0.1);
  const headY = horizonY - height * (0.22 + highs * 0.08 + sweepEnergy * 0.06);
  const headX2 = centerX - inversePhase * width * (0.16 + sweepEnergy * 0.08);
  const headY2 = horizonY - height * (0.18 + mids * 0.06 + sweepEnergy * 0.05);

  const haze = ctx.createRadialGradient(headX, headY, 24, headX, headY, width * 0.34);
  haze.addColorStop(0, rgba(theme.core, 0.08 + sweepEnergy * 0.1));
  haze.addColorStop(0.35, rgba(theme.bloomA, 0.08 + highs * 0.12));
  haze.addColorStop(1, rgba(theme.backgroundLow, 0));
  ctx.fillStyle = haze;
  ctx.fillRect(0, 0, width, height);

  const haze2 = ctx.createRadialGradient(headX2, headY2, 20, headX2, headY2, width * 0.28);
  haze2.addColorStop(0, rgba(theme.core, 0.06 + sweepEnergy * 0.08));
  haze2.addColorStop(0.32, rgba(theme.bloomB, 0.07 + mids * 0.12));
  haze2.addColorStop(1, rgba(theme.backgroundLow, 0));
  ctx.fillStyle = haze2;
  ctx.fillRect(0, 0, width, height);

  for (let i = 0; i < beamCount; i += 1) {
    const spread = i / (beamCount - 1) - 0.5;
    const angle = spread * (2.25 + mids * 0.95 + sweepEnergy * 0.4) + sweepPhase * 0.32 + Math.sin(phase * 2.1 + i * 0.5) * 0.08;
    const crossAngle = spread * -(1.95 + highs * 0.7 + sweepEnergy * 0.3) + inversePhase * 0.28 + Math.cos(phase * 1.7 + i * 0.46) * 0.06;
    const length = height * (0.62 + highs * 0.28 + sweepEnergy * 0.16 + (i % 3 === 0 ? snareImpact * 0.16 : 0));
    const crossLength = height * (0.54 + mids * 0.2 + sweepEnergy * 0.14 + (i % 4 === 0 ? snareImpact * 0.12 : 0));
    const endX = centerX + Math.sin(angle) * fanWidth;
    const endY = horizonY - length;
    const crossX = centerX + Math.sin(crossAngle) * fanWidth * 0.86;
    const crossY = horizonY - crossLength;

    const glow = ctx.createLinearGradient(centerX, horizonY, endX, endY);
    glow.addColorStop(0, rgba(theme.core, 0.06 + bassImpact * 0.08));
    glow.addColorStop(0.18, rgba(i % 2 === 0 ? theme.bloomA : theme.bloomB, 0.18 + sweepEnergy * 0.18 + snareImpact * 0.1));
    glow.addColorStop(0.6, rgba(i % 2 === 0 ? theme.bloomA : theme.bloomB, 0.08 + highs * 0.14));
    glow.addColorStop(1, rgba(theme.backgroundLow, 0));
    ctx.strokeStyle = glow;
    ctx.lineWidth = 8 + highs * 5 + sweepEnergy * 5 + (i % 4 === 0 ? snareImpact * 4 : 0);
    ctx.beginPath();
    ctx.moveTo(centerX, horizonY);
    ctx.lineTo(endX, endY);
    ctx.stroke();

    ctx.strokeStyle = rgba(theme.core, 0.14 + sweepEnergy * 0.18);
    ctx.lineWidth = 1.5 + highs * 1.8;
    ctx.beginPath();
    ctx.moveTo(centerX, horizonY);
    ctx.lineTo(endX, endY);
    ctx.stroke();

    ctx.strokeStyle = rgba(theme.bloomB, 0.06 + sweepEnergy * 0.12 + highs * 0.08);
    ctx.lineWidth = 4 + sweepEnergy * 3;
    ctx.beginPath();
    ctx.moveTo(centerX, horizonY);
    ctx.lineTo(crossX, crossY);
    ctx.stroke();
  }

  const sweepBeam = ctx.createLinearGradient(centerX, horizonY, headX, headY);
  sweepBeam.addColorStop(0, rgba(theme.core, 0.08 + sweepEnergy * 0.08));
  sweepBeam.addColorStop(0.2, rgba(theme.bloomB, 0.22 + sweepEnergy * 0.18));
  sweepBeam.addColorStop(1, rgba(theme.backgroundLow, 0));
  ctx.strokeStyle = sweepBeam;
  ctx.lineWidth = 18 + sweepEnergy * 14;
  ctx.beginPath();
  ctx.moveTo(centerX, horizonY);
  ctx.lineTo(headX, headY);
  ctx.stroke();

  const sweepBeam2 = ctx.createLinearGradient(centerX, horizonY, headX2, headY2);
  sweepBeam2.addColorStop(0, rgba(theme.core, 0.07 + sweepEnergy * 0.07));
  sweepBeam2.addColorStop(0.18, rgba(theme.bloomA, 0.18 + sweepEnergy * 0.16));
  sweepBeam2.addColorStop(1, rgba(theme.backgroundLow, 0));
  ctx.strokeStyle = sweepBeam2;
  ctx.lineWidth = 12 + sweepEnergy * 10;
  ctx.beginPath();
  ctx.moveTo(centerX, horizonY);
  ctx.lineTo(headX2, headY2);
  ctx.stroke();

  ctx.fillStyle = rgba(theme.bloomA, 0.08 + bassImpact * 0.16 + sweepEnergy * 0.08);
  ctx.fillRect(0, horizonY, width, height - horizonY);

  ctx.strokeStyle = rgba(theme.wave, 0.18 + highs * 0.24 + sweepEnergy * 0.1);
  ctx.lineWidth = 2.4 + sweepEnergy * 1.8;
  ctx.beginPath();
  for (let x = 0; x <= width; x += 10) {
    const normalized = x / width;
    const y = horizonY - 22 + Math.sin(normalized * 14 + phase * 2.8) * (10 + bass * 22 + snareImpact * 20) + Math.cos(normalized * 9 - phase * 1.6) * (4 + sweepEnergy * 12);
    if (x === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();

  const strobeAlpha = Math.max(0, snareImpact * 0.18 + transitionFlash * 0.08);
  if (strobeAlpha > 0.02) {
    ctx.fillStyle = rgba(theme.core, Math.min(0.16, strobeAlpha));
    ctx.fillRect(0, 0, width, height * (0.16 + sweepEnergy * 0.05));
    ctx.strokeStyle = rgba(theme.wave, Math.min(0.24, strobeAlpha + 0.04));
    ctx.lineWidth = 3 + snareImpact * 4;
    for (let i = 0; i < 3; i += 1) {
      const y = height * (0.12 + i * 0.1) + Math.sin(phase * 4 + i) * 8;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
      ctx.stroke();
    }
  }

  for (let i = 0; i < 12; i += 1) {
    const pulseWidth = width * (0.14 + i * 0.06 + bassImpact * 0.03 + sweepEnergy * 0.02);
    ctx.strokeStyle = rgba(i % 2 === 0 ? theme.bloomB : theme.ring, Math.max(0, 0.08 + bassImpact * 0.08 + sweepEnergy * 0.06 - i * 0.006));
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    ctx.ellipse(centerX, horizonY, pulseWidth, 16 + i * 9 + bassImpact * 14, 0, 0, Math.PI * 2);
    ctx.stroke();
  }
}

function renderOrbitalScene(width, height, theme, bass, mids, highs, overall) {
  drawBloom(width, height, theme, bass, mids);
  const centerX = width / 2;
  const centerY = height / 2;
  const orbitCount = 5;

  for (let i = 0; i < orbitCount; i += 1) {
    const radiusX = Math.min(width, height) * (0.16 + i * 0.08 + mids * 0.03);
    const radiusY = radiusX * (0.46 + i * 0.08 + highs * 0.08);
    const rotation = phase * (0.35 + i * 0.08) + i * 0.44;
    ctx.strokeStyle = rgba(i % 2 === 0 ? theme.ring : theme.wave, 0.08 + overall * 0.12 + i * 0.01);
    ctx.lineWidth = 1.5 + (i === 0 ? bassImpact * 4 : 0);
    ctx.beginPath();
    ctx.ellipse(centerX, centerY, radiusX, radiusY, rotation, 0, Math.PI * 2);
    ctx.stroke();

    const nodeCount = 6 + i * 2;
    for (let j = 0; j < nodeCount; j += 1) {
      const angle = (Math.PI * 2 * j) / nodeCount + phase * (0.8 + i * 0.12);
      const x = centerX + Math.cos(angle) * radiusX * Math.cos(rotation) - Math.sin(angle) * radiusY * Math.sin(rotation);
      const y = centerY + Math.cos(angle) * radiusX * Math.sin(rotation) + Math.sin(angle) * radiusY * Math.cos(rotation);
      const size = 2 + highs * 2.4 + (j % 3 === 0 ? snareImpact * 2.2 : 0);
      ctx.fillStyle = rgba(j % 2 === 0 ? theme.bloomA : theme.bloomB, 0.22 + overall * 0.26);
      ctx.beginPath();
      ctx.arc(x, y, size, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  ctx.strokeStyle = rgba(theme.bloomB, 0.08 + highs * 0.14);
  ctx.lineWidth = 1.4;
  for (let i = 0; i < 18; i += 1) {
    const angle = (Math.PI * 2 * i) / 18 + phase * 0.42;
    const inner = 22 + bassImpact * 16;
    const outer = Math.min(width, height) * (0.28 + highs * 0.06 + (i % 4 === 0 ? snareImpact * 0.08 : 0));
    ctx.beginPath();
    ctx.moveTo(centerX + Math.cos(angle) * inner, centerY + Math.sin(angle) * inner);
    ctx.lineTo(centerX + Math.cos(angle) * outer, centerY + Math.sin(angle) * outer);
    ctx.stroke();
  }

  ctx.fillStyle = rgba(theme.core, 0.12 + bassPulse * 0.12 + bassImpact * 0.1);
  ctx.beginPath();
  ctx.arc(centerX, centerY, 18 + bassPulse * 24 + bassImpact * 12, 0, Math.PI * 2);
  ctx.fill();
}
function renderCityBarsScene(width, height, theme, bass, mids, highs, overall) {
  drawBloom(width, height, theme, bass, mids);
  const columns = 54;
  const spacing = width / columns;
  const horizon = height * (0.62 + Math.sin(phase * 0.8) * 0.02);

  for (let i = 0; i < columns; i += 1) {
    const bucket = Math.min(dataArray.length - 1, Math.floor(i * (dataArray.length / columns)));
    const value = dataArray[bucket] / 255;
    const x = i * spacing;
    const sizeFactor = bassSizeFactor();
    const skylineBoost = i < Math.floor(columns * 0.28) ? 1 + bassPulse * 0.95 * sizeFactor + bassImpact * 1.05 * sizeFactor : 1 + bassPulse * 0.18 * sizeFactor;
    const buildingHeight = 20 + value * height * 0.5 * skylineBoost;
    const widthScale = (i % 3 === 0 ? 1.2 : 0.82) * spacing;
    const hue = theme.barsBaseHue - value * theme.barsHueRange + Math.sin(i * 0.4 + phase) * 12;
    ctx.fillStyle = `hsla(${hue}, 88%, ${30 + value * 30}%, ${0.38 + value * 0.46})`;
    ctx.fillRect(x, horizon - buildingHeight, Math.max(5, widthScale - 4), buildingHeight + height);

    ctx.fillStyle = `hsla(${hue}, 96%, ${72 + value * 12}%, ${0.18 + mids * 0.18})`;
    for (let row = 0; row < 8; row += 1) {
      const windowY = horizon - 18 - row * 18;
      if (windowY < horizon - buildingHeight + 14) {
        break;
      }
      ctx.fillRect(x + 6, windowY, 4, 8);
      ctx.fillRect(x + Math.max(10, widthScale - 12), windowY, 4, 8);
    }
  }

  ctx.strokeStyle = rgba(theme.wave, 0.28 + overall * 0.22);
  ctx.lineWidth = 2;
  ctx.beginPath();
  for (let x = 0; x <= width; x += 12) {
    const normalized = x / width;
    const y = horizon - 24 + Math.sin(normalized * 10 + phase * 2.4) * (10 + bass * 28) + Math.cos(normalized * 16 - phase * 2.1) * (6 + highs * 16);
    if (x === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();

  ctx.fillStyle = rgba(theme.bloomA, 0.12 + bass * 0.16);
  ctx.fillRect(0, horizon + 8, width, height - horizon);
}

function render() {
  animationFrame = requestAnimationFrame(render);

  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const theme = themes[activeTheme];

  const dimsOk = Number.isFinite(width) && Number.isFinite(height) && width >= 2 && height >= 2;
  if (!dimsOk) {
    transitionFlash += (0 - transitionFlash) * 0.12;
    return;
  }

  if (!analyser || !dataArray) {
    bassPulse += (0 - bassPulse) * 0.18;
    bassImpact += (0 - bassImpact) * 0.24;
    snareImpact += (0 - snareImpact) * 0.24;
    hatEnergy += (0 - hatEnergy) * 0.18;
    transitionFlash += (0 - transitionFlash) * 0.16;
    renderIdle();
    return;
  }

  analyser.getByteFrequencyData(dataArray);

  const sensitivity = bassSensitivityFactor();
  const bass = averageRange(dataArray, 0, 12) / 255;
  const snare = averageRange(dataArray, 18, 42) / 255;
  const mids = averageRange(dataArray, 12, 48) / 255;
  const hats = averageRange(dataArray, 78, 150) / 255;
  const highs = averageRange(dataArray, 48, 120) / 255;
  const overall = averageRange(dataArray, 0, 120) / 255;
  const bassTarget = Math.min(1.9, bass * 1.55 * sensitivity + Math.max(0, bass - overall * 0.74) * 3.8 * sensitivity);
  const bassTransient = Math.max(0, bass - bassPulse * 0.34 - overall * 0.16);
  const snareTransient = Math.max(0, snare - bass * 0.24 - mids * 0.28);
  const hatTarget = Math.max(0, hats * 1.45 - bass * 0.16);
  maybeTriggerReactiveChange(bass, mids, highs, overall);

  energy += (overall - energy) * 0.14;
  bassPulse += (bassTarget - bassPulse) * (bassTarget > bassPulse ? 0.34 : 0.12);
  const impactTarget = Math.min(1.6, bassTransient * 8.5 * sensitivity + Math.max(0, bassTarget - bassPulse) * 0.45);
  bassImpact += (impactTarget - bassImpact) * (impactTarget > bassImpact ? 0.55 : 0.18);
  const snareTarget = Math.min(1.35, snareTransient * 7.2 + Math.max(0, snare - overall * 0.32) * 0.9);
  snareImpact += (snareTarget - snareImpact) * (snareTarget > snareImpact ? 0.48 : 0.2);
  hatEnergy += (hatTarget - hatEnergy) * (hatTarget > hatEnergy ? 0.24 : 0.1);
  transitionFlash += (0 - transitionFlash) * 0.12;
  if (impactTarget > 0.78) {
    transitionFlash = Math.max(transitionFlash, Math.min(0.18, impactTarget * 0.12));
  }
  if (snareTarget > 0.34) {
    transitionFlash = Math.max(transitionFlash, Math.min(0.22, snareTarget * 0.16));
  }
  phase += 0.01 + energy * 0.045 + bassPulse * 0.008 + bassImpact * 0.012 + hatEnergy * 0.008 + phraseContrast * 0.004;

  energyText.textContent = `${Math.round(energy * 100)}%`;
  bandText.textContent = detectDominantBand(bass, mids, highs);

  ctx.clearRect(0, 0, width, height);
  clearBackground(width, height, theme, bass, mids, highs);
  drawBassAccent(width, height, theme, bass, overall);

  if (activeScene === "tunnel") {
    renderTunnelScene(width, height, theme, bass, mids, highs, overall);
  } else if (activeScene === "constellation") {
    renderConstellationScene(width, height, theme, bass, mids, highs, overall);
  } else if (activeScene === "equalizer") {
    renderEqualizerScene(width, height, theme, bass, mids, highs, overall);
  } else if (activeScene === "radialBars") {
    renderRadialBarsScene(width, height, theme, bass, mids, highs, overall);
  } else if (activeScene === "cityBars") {
    renderCityBarsScene(width, height, theme, bass, mids, highs, overall);
  } else if (activeScene === "subwoofer") {
    renderSubwooferScene(width, height, theme, bass, mids, highs, overall);
  } else if (activeScene === "waterfall") {
    renderWaterfallScene(width, height, theme, bass, mids, highs, overall);
  } else if (activeScene === "laserClub") {
    renderLaserClubScene(width, height, theme, bass, mids, highs, overall);
  } else if (activeScene === "orbital") {
    renderOrbitalScene(width, height, theme, bass, mids, highs, overall);
  } else if (activeScene === "dualSub") {
    renderDualSubScene(width, height, theme, bass, mids, highs, overall);
  } else {
    renderHaloScene(width, height, theme, bass, mids, highs, overall);
  }

  drawTrebleAccents(width, height, theme);
  drawTransitionOverlay(width, height);
  syncPreview();
}

async function ensureAudioPipeline(nextStream, sourceLabel) {
  const audioTracks = nextStream.getAudioTracks();
  if (!audioTracks.length) {
    throw new Error("No audio track was provided by that source.");
  }

  audioContext = new AudioContext();
  sourceNode = audioContext.createMediaStreamSource(nextStream);
  analyser = audioContext.createAnalyser();
  analyser.fftSize = 512;
  analyser.smoothingTimeConstant = 0.82;
  dataArray = new Uint8Array(analyser.frequencyBinCount);
  sourceNode.connect(analyser);
  stream = nextStream;
  setSource(sourceLabel);
  resetReactiveDetector();

  stream.getTracks().forEach((track) => {
    track.addEventListener("ended", () => {
      stopCapture();
    });
  });
}

async function stopCapture() {
  if (animationFrame) {
    cancelAnimationFrame(animationFrame);
    animationFrame = null;
  }

  if (stream) {
    stream.getTracks().forEach((track) => track.stop());
    stream = null;
  }

  if (sourceNode) {
    sourceNode.disconnect();
    sourceNode = null;
  }

  if (audioContext) {
    await audioContext.close();
    audioContext = null;
  }

  analyser = null;
  dataArray = null;
  energy = 0;
  bassPulse = 0;
  bassImpact = 0;
  snareImpact = 0;
  hatEnergy = 0;
  waterfallHistory = [];
  resetReactiveDetector();
  setStatus("Idle");
  setSource("None");
  energyText.textContent = "0%";
  bandText.textContent = "None";
  setButtonsCapturing(false);
  render();
}

function selectedDeviceLabel() {
  const selectedOption = deviceSelect.options[deviceSelect.selectedIndex];
  return selectedOption ? selectedOption.textContent : "Audio input device";
}

async function startShareCapture() {
  try {
    setStatus("Requesting shared audio source...");
    setButtonsCapturing(true);

    const nextStream = await navigator.mediaDevices.getDisplayMedia({
      video: true,
      audio: {
        suppressLocalAudioPlayback: false
      },
      preferCurrentTab: true,
      selfBrowserSurface: "include",
      systemAudio: "include"
    });

    await ensureAudioPipeline(nextStream, "Browser/System Share");
    setStatus("Listening");
    render();
  } catch (error) {
    console.error(error);
    setStatus(error.message || "Capture failed");
    setButtonsCapturing(false);
  }
}

async function refreshInputDevices() {
  const previousValue = deviceSelect.value;
  deviceSelect.innerHTML = "";

  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const audioInputs = devices.filter((device) => device.kind === "audioinput");

    if (!audioInputs.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "No audio input devices found";
      deviceSelect.appendChild(option);
      deviceBtn.disabled = true;
      return;
    }

    audioInputs.forEach((device, index) => {
      const option = document.createElement("option");
      option.value = device.deviceId;
      option.textContent = device.label || `Audio input ${index + 1}`;
      deviceSelect.appendChild(option);
    });

    if ([...deviceSelect.options].some((option) => option.value === previousValue)) {
      deviceSelect.value = previousValue;
    }

    deviceBtn.disabled = false;
  } catch (error) {
    console.error(error);
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Unable to enumerate devices";
    deviceSelect.appendChild(option);
    deviceBtn.disabled = true;
  }
}

async function unlockDeviceLabels() {
  try {
    const temporaryStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    temporaryStream.getTracks().forEach((track) => track.stop());
  } catch (error) {
    console.warn("Audio permission not granted yet.", error);
  }
}

async function startDeviceCapture() {
  try {
    setStatus("Requesting audio input device...");
    setButtonsCapturing(true);

    if (!deviceSelect.value) {
      throw new Error("Pick an audio input device first.");
    }

    const nextStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: { exact: deviceSelect.value },
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false
      },
      video: false
    });

    await ensureAudioPipeline(nextStream, selectedDeviceLabel());
    setStatus("Listening");
    render();
  } catch (error) {
    console.error(error);
    setStatus(error.message || "Device capture failed");
    setButtonsCapturing(false);
  }
}

async function toggleFullscreen() {
  try {
    if (document.fullscreenElement) {
      await document.exitFullscreen();
    } else {
      await visualFrame.requestFullscreen();
    }
  } catch (error) {
    console.error(error);
    setStatus("Fullscreen request was blocked by the browser");
  }
}

function syncFullscreenState() {
  const isFullscreen = Boolean(document.fullscreenElement);
  appShell.classList.toggle("is-fullscreen", isFullscreen);
  fullscreenBtn.textContent = isFullscreen ? "Exit Fullscreen" : "Fullscreen";
  hudFullscreenBtn.textContent = isFullscreen ? "Exit Fullscreen" : "Fullscreen";
  resizeCanvas();
}

window.addEventListener("resize", () => {
  resizeCanvas();
  nudgeCinematicHud();
});
window.addEventListener("keydown", (event) => {
  if (event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement || event.target instanceof HTMLTextAreaElement) {
    return;
  }

  const key = event.key.toLowerCase();
  if (key === "f" && !event.repeat) {
    event.preventDefault();
    toggleFullscreen();
    return;
  }
  if (key === "arrowleft") {
    event.preventDefault();
    cycleScene(-1);
    return;
  }
  if (key === "arrowright") {
    event.preventDefault();
    cycleScene(1);
    return;
  }
  if (key === "w" || key === "arrowup") {
    event.preventDefault();
    cycleTheme(-1);
    return;
  }
  if (key === "s" || key === "arrowdown") {
    event.preventDefault();
    cycleTheme(1);
  }
});

shareBtn.addEventListener("click", startShareCapture);
cinematicBtn.addEventListener("click", toggleCinematicMode);
deviceBtn.addEventListener("click", startDeviceCapture);
fullscreenBtn.addEventListener("click", toggleFullscreen);
hudFullscreenBtn.addEventListener("click", toggleFullscreen);
hudCinematicBtn.addEventListener("click", toggleCinematicMode);
stopBtn.addEventListener("click", stopCapture);
refreshDevicesBtn.addEventListener("click", async () => {
  await unlockDeviceLabels();
  await refreshInputDevices();
});
prevSceneBtn.addEventListener("click", () => cycleScene(-1));
nextSceneBtn.addEventListener("click", () => cycleScene(1));
prevThemeBtn.addEventListener("click", () => cycleTheme(-1));
nextThemeBtn.addEventListener("click", () => cycleTheme(1));
autoOffBtn.addEventListener("click", () => setAutoMode("off"));
autoCycleBtn.addEventListener("click", () => setAutoMode("cycle"));
autoRandomBtn.addEventListener("click", () => setAutoMode("random"));
autoReactiveBtn.addEventListener("click", () => setAutoMode("reactive"));
hudAutoOffBtn.addEventListener("click", () => setAutoMode("off"));
hudAutoCycleBtn.addEventListener("click", () => setAutoMode("cycle"));
hudAutoRandomBtn.addEventListener("click", () => setAutoMode("random"));
hudAutoReactiveBtn.addEventListener("click", () => setAutoMode("reactive"));
lockThemeBtn?.addEventListener("click", toggleLockTheme);
hudLockThemeBtn?.addEventListener("click", toggleLockTheme);
bassSensitivityInput.addEventListener("input", syncBassControls);
bassSizeInput.addEventListener("input", syncBassControls);
autoIntervalSelect.addEventListener("change", () => {
  if (autoMode === "cycle" || autoMode === "random") {
    setAutoMode(autoMode);
  } else {
    syncAutoButtons();
  }
});
themeButtons.forEach((button) => {
  button.addEventListener("click", () => setTheme(button.dataset.theme));
});
sceneButtons.forEach((button) => {
  button.addEventListener("click", () => setScene(button.dataset.scene));
});
document.addEventListener("fullscreenchange", syncFullscreenState);
visualFrame.addEventListener("mousemove", nudgeCinematicHud);
visualFrame.addEventListener("click", nudgeCinematicHud);
visualFrame.addEventListener("touchstart", nudgeCinematicHud, { passive: true });
navigator.mediaDevices.addEventListener("devicechange", refreshInputDevices);

(async function init() {
  const preferences = loadPreferences();
  if (preferences) {
    if (preferences.theme && themes[preferences.theme]) {
      activeTheme = preferences.theme;
    }
    if (preferences.scene && sceneNames[preferences.scene]) {
      activeScene = preferences.scene;
    }
    if (preferences.autoInterval) {
      autoIntervalSelect.value = String(preferences.autoInterval);
    }
    if (preferences.bassSensitivity) {
      bassSensitivityInput.value = String(preferences.bassSensitivity);
    }
    if (preferences.bassSize) {
      bassSizeInput.value = String(preferences.bassSize);
    }
    lockTheme = Boolean(preferences.lockTheme);
  }

  resizeCanvas();
  render();
  setTheme(activeTheme);
  setScene(activeScene);
  setLockTheme(lockTheme);
  setAutoMode(preferences?.autoMode && ["off", "cycle", "random", "reactive"].includes(preferences.autoMode) ? preferences.autoMode : "off");
  syncBassControls();
  setCinematicMode(Boolean(preferences?.cinematicMode));
  syncFullscreenState();
  await unlockDeviceLabels();
  await refreshInputDevices();
  setSource(currentSourceLabel);
})();
