const STORAGE_KEY = 'bankmind-theme';

function getTheme() {
  try {
    const saved = wx.getStorageSync(STORAGE_KEY);
    if (saved === 'dark' || saved === 'light') return saved;
  } catch (_) {}
  return 'light';
}

function haptic() {
  try {
    wx.vibrateShort({ type: 'light', fail() {} });
  } catch (_) {}
}

function setTheme(theme) {
  const next = theme === 'dark' ? 'dark' : 'light';
  try {
    wx.setStorageSync(STORAGE_KEY, next);
  } catch (_) {}
  applyThemeChrome(next);
  return next;
}

function applyThemeChrome(theme) {
  const dark = theme === 'dark';
  if (typeof wx.setNavigationBarColor === 'function') {
    wx.setNavigationBarColor({
      frontColor: dark ? '#ffffff' : '#000000',
      backgroundColor: dark ? '#0c1920' : '#f3f7f6',
      animation: { duration: 220, timingFunc: 'easeInOut' },
      fail() {}
    });
  }
  if (typeof wx.setTabBarStyle === 'function') {
    wx.setTabBarStyle({
      color: dark ? '#8ea39a' : '#627770',
      selectedColor: dark ? '#dfc992' : '#17211f',
      backgroundColor: dark ? '#11222a' : '#ffffff',
      borderStyle: dark ? 'black' : 'white',
      fail() {}
    });
  }
}

function syncPageTheme(page) {
  const theme = getTheme();
  if (page && typeof page.setData === 'function' && page.data.theme !== theme) {
    page.setData({ theme });
  }
  applyThemeChrome(theme);
  return theme;
}

function formatBytes(received, total) {
  const fmt = (n) => {
    const v = Number(n) || 0;
    if (v >= 1024 * 1024) return (v / (1024 * 1024)).toFixed(1) + ' MB';
    if (v >= 1024) return (v / 1024).toFixed(1) + ' KB';
    return v + ' B';
  };
  if (!total) return fmt(received);
  const pct = Math.min(100, Math.round(((Number(received) || 0) / Number(total)) * 100));
  return `${fmt(received)} / ${fmt(total)} (${pct}%)`;
}

module.exports = { getTheme, setTheme, applyThemeChrome, syncPageTheme, formatBytes, haptic };
