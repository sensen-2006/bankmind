import os
import httpx
from fastapi import HTTPException
from .security import digest

def exchange_code(code):
    app_id,secret=os.getenv('WECHAT_APP_ID',''),os.getenv('WECHAT_APP_SECRET','')
    if not app_id or not secret:
        raise HTTPException(501,'真实微信登录未配置；开发模式请使用企业账号，不伪造微信身份')
    try:
        with httpx.Client(timeout=10,follow_redirects=False) as client:
            response=client.get('https://api.weixin.qq.com/sns/jscode2session',params={'appid':app_id,'secret':secret,'js_code':code,'grant_type':'authorization_code'})
            response.raise_for_status();data=response.json()
        if data.get('errcode') or not data.get('openid'):raise ValueError('WX_REJECTED')
        return app_id,digest(app_id+':'+data['openid'])
    except Exception:
        # No provider response, session_key, secret, or request URL is logged.
        raise HTTPException(401,'微信code校验失败，请重新登录')
