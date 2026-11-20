const { setTheme } = require('../../utils/theme');

Component({
  options: {
    styleIsolation: 'apply-shared'
  },
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
      const next = this.data.theme === 'dark' ? 'light' : 'dark';
      setTheme(next);
      this.triggerEvent('change', { theme: next });
    }
  }
});
