const { getTheme } = require('../../utils/theme');

Component({
  properties: {
    text: {
      type: String,
      value: '正在读取业务数据'
    },
    theme: {
      type: String,
      value: ''
    }
  },
  data: {
    resolvedTheme: 'light'
  },
  lifetimes: {
    attached() {
      this.setData({ resolvedTheme: this.data.theme || getTheme() });
    }
  },
  observers: {
    theme(val) {
      this.setData({ resolvedTheme: val || getTheme() });
    }
  }
});
