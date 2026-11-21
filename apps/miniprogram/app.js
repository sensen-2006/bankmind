const config=require('./config');
App({globalData:{config,user:null},onLaunch(){this.globalData.user=wx.getStorageSync('bankmind-user')||null;}});
