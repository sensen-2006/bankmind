const SOUND_KEY = 'bankmind-sound';

let ctx = null;
let lastPlay = 0;

function isSoundEnabled() {
  try {
    const saved = wx.getStorageSync(SOUND_KEY);
    if (saved === false || saved === 'false') return false;
  } catch (_) {}
  return true;
}

function setSoundEnabled(enabled) {
  const next = Boolean(enabled);
  try {
    wx.setStorageSync(SOUND_KEY, next);
  } catch (_) {}
  return next;
}

function getCtx() {
  if (typeof wx === 'undefined' || typeof wx.createWebAudioContext !== 'function') return null;
  if (!ctx) {
    try {
      ctx = wx.createWebAudioContext();
    } catch (_) {
      return null;
    }
  }
  if (ctx && ctx.state === 'suspended' && typeof ctx.resume === 'function') {
    try {
      ctx.resume();
    } catch (_) {}
  }
  return ctx;
}

function tone(audioCtx, type, startFreq, endFreq, startOffset, duration, peakGain) {
  const now = audioCtx.currentTime + startOffset;
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(startFreq, now);
  if (endFreq && endFreq !== startFreq) {
    osc.frequency.exponentialRampToValueAtTime(endFreq, now + duration);
  }
  gain.gain.setValueAtTime(0.001, now);
  gain.gain.linearRampToValueAtTime(peakGain, now + Math.min(0.012, duration * 0.25));
  gain.gain.exponentialRampToValueAtTime(0.0008, now + duration);
  osc.connect(gain);
  gain.connect(audioCtx.destination);
  osc.start(now);
  osc.stop(now + duration + 0.01);
}

function playSound(cue = 'click', force = false) {
  if (!force && !isSoundEnabled()) return;
  const nowMs = Date.now();
  if (!force && nowMs - lastPlay < 70) return;
  lastPlay = nowMs;

  const audioCtx = getCtx();
  if (!audioCtx) return;

  try {
    if (cue === 'toggle') {
      // Soft watery droplet chime (matches Web playLiquid)
      tone(audioCtx, 'sine', 440, 690, 0, 0.095, 0.14);
    } else if (cue === 'open') {
      // Soft rising pop
      tone(audioCtx, 'sine', 520, 780, 0, 0.075, 0.11);
    } else if (cue === 'success') {
      // Pleasant two-note confirmation chime
      tone(audioCtx, 'sine', 587, 620, 0, 0.08, 0.12);
      tone(audioCtx, 'sine', 880, 920, 0.075, 0.13, 0.13);
    } else if (cue === 'error') {
      // Gentle low alert double-tap
      tone(audioCtx, 'triangle', 320, 220, 0, 0.09, 0.12);
    } else {
      // Subtle crisp tick (matches Web playTick)
      tone(audioCtx, 'triangle', 1180, 640, 0, 0.035, 0.1);
    }
  } catch (_) {}
}

module.exports = { isSoundEnabled, setSoundEnabled, playSound };
