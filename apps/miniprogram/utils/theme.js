const STORAGE_KEY = 'bankmind-theme';

function getTheme() {
  try {
    const saved = wx.getStorageSync(STORAGE_KEY);
    return saved === 'dark' ? 'dark' : 'light';
  } catch (e) {
    return 'light';
  }
}

function applyThemeChrome(theme) {
  const isDark = theme === 'dark';
  if (typeof wx !== 'undefined' && typeof wx.setNavigationBarColor === 'function') {
    try {
      wx.setNavigationBarColor({
        frontColor: isDark ? '#ffffff' : '#000000',
        backgroundColor: isDark ? '#0B0F14' : '#F6F8FA',
        animation: { duration: 180, timingFunc: 'easeInOut' },
        fail() {}
      });
    } catch (e) {}
  }
  if (typeof wx !== 'undefined' && typeof wx.setTabBarStyle === 'function') {
    try {
      wx.setTabBarStyle({
        color: isDark ? '#8492A6' : '#6B7280',
        selectedColor: isDark ? '#2DD4BF' : '#0D9488',
        backgroundColor: isDark ? '#121821' : '#FFFFFF',
        borderStyle: isDark ? 'black' : 'white',
        fail() {}
      });
    } catch (e) {}
  }
}

function setTheme(theme) {
  const next = theme === 'dark' ? 'dark' : 'light';
  try {
    wx.setStorageSync(STORAGE_KEY, next);
  } catch (e) {}
  const app = typeof getApp === 'function' ? getApp() : null;
  if (app && app.globalData) {
    app.globalData.theme = next;
  }
  applyThemeChrome(next);
  return next;
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
  const fmt = (b) => {
    const n = Number(b) || 0;
    if (n >= 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + ' MB';
    if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
    return n + ' B';
  };
  const pct = total > 0 ? Math.min(100, Math.round((received / total) * 100)) : 0;
  return `${fmt(received)} / ${fmt(total)} (${pct}%)`;
}

function shortId(value) {
  if (!value) return '';
  const s = String(value);
  return s.length > 10 ? s.slice(0, 8) + '…' : s;
}

module.exports = {
  getTheme,
  setTheme,
  applyThemeChrome,
  syncPageTheme,
  formatBytes,
  shortId
};
