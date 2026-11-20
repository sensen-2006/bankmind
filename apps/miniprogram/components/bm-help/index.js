Component({
  options: {
    styleIsolation: 'apply-shared'
  },
  properties: {
    text: { type: String, value: '' },
    label: { type: String, value: '说明' }
  },
  methods: {
    show() {
      wx.showModal({
        title: this.data.label,
        content: this.data.text,
        showCancel: false,
        confirmText: '知道了'
      });
    }
  }
});
