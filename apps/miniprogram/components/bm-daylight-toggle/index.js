const { setTheme, haptic } = require('../../utils/theme');
const { playSound } = require('../../utils/sound');

Component({
  properties: {
    theme: {
      type: String,
      value: 'light'
    },
    compact: {
      type: Boolean,
      value: false
    }
  },
  methods: {
    toggle() {
      haptic();
      playSound('toggle');
      const next = this.data.theme === 'dark' ? 'light' : 'dark';
      setTheme(next);
      this.triggerEvent('change', { theme: next });
    }
  }
});
