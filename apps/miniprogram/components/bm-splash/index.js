Component({
  data: {
    visible: false,
    leaving: false
  },
  lifetimes: {
    attached() {
      const app = getApp();
      if (!app || !app.globalData || app.globalData.splashShown) return;
      app.globalData.splashShown = true;
      this.setData({ visible: true, leaving: false });
      this._timer = setTimeout(() => this.dismiss(), 1800);
    },
    detached() {
      clearTimeout(this._timer);
      clearTimeout(this._hideTimer);
    }
  },
  methods: {
    noop() {},
    dismiss() {
      if (!this.data.visible || this.data.leaving) return;
      clearTimeout(this._timer);
      this.setData({ leaving: true });
      this._hideTimer = setTimeout(() => {
        this.setData({ visible: false });
      }, 380);
    }
  }
});
