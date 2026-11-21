const { setTheme, haptic } = require('../../utils/theme');

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
      const next = this.data.theme === 'dark' ? 'light' : 'dark';
      setTheme(next);
      this.triggerEvent('change', { theme: next });
    }
  }
});
