import os
import time
import json
import re
import asyncio
from datetime import datetime, date
from typing import List, Optional, Tuple, Dict
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Reply

class WikiMixin:
    def _wiki_write_main_py(self, content: str, api_url: str, username: str,
                            password: str, target_page: str) -> str:
        """登录 Miraheze 的机器人账号，将 content 写入指定 wiki 页面。

        账号密码来自插件配置项（__init__ 注入的 self.config），不在代码中硬编码。
        """
        import json
        from curl_cffi.requests import Session

        API = api_url
        USER = username
        PASSWORD = password
        TITLE = target_page
        BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

        sess = Session(impersonate='chrome')
        sess.headers['User-Agent'] = BROWSER_UA

        def call(params, is_post=False):
            if is_post:
                r = sess.post(API, data=params)
            else:
                r = sess.get(API, params=params)
            return r.json()

        # 1) 获取登录 token
        j = call({'action': 'query', 'meta': 'tokens', 'type': 'login', 'format': 'json'})
        lt = j['query']['tokens']['logintoken']

        # 2) 登录
        j = call({'action': 'login', 'lgname': USER, 'lgpassword': PASSWORD,
                  'lgtoken': lt, 'format': 'json'}, is_post=True)
        if j.get('login', {}).get('result') != 'Success':
            raise RuntimeError(f"登录失败: {json.dumps(j, ensure_ascii=False)}")

        # 3) 获取 csrf token
        j = call({'action': 'query', 'meta': 'tokens', 'type': 'csrf', 'format': 'json'})
        csrf = j['query']['tokens']['csrftoken']

        # 4) 写入页面
        j = call({'action': 'edit', 'title': TITLE, 'text': content,
                  'summary': 'test: sync astrbot_plugin_summary/main.py',
                  'token': csrf, 'assert': 'user', 'format': 'json'}, is_post=True)
        if j.get('edit', {}).get('result') == 'Success':
            newrev = j['edit'].get('newrevid')
            return f"已写入 {TITLE}（{len(content)} 字节，新版本 {newrev}）。"
        else:
            raise RuntimeError(f"编辑失败: {json.dumps(j, ensure_ascii=False)}")

    # ---------- 指令：测试wiki打包上传 ----------

    def _wiki_upload_one_image(self, sess, api_url, url, data: bytes, idx, csrf,
                               name_base: str = None) -> str:
        """上传单张图片，返回 wiki 上的文件名。

        name_base: 可选，自定义文件名主体（不含扩展名）。默认 PackImg_{idx}；
        不同用途应传不同的 name_base，否则会互相覆盖同名文件。
        """
        import re
        import urllib.parse
        from io import BytesIO
        base = name_base or f"PackImg_{idx}"
        # 1) 优先 upload-by-url（直传）：把 QQ 图直链交给 Miraheze，让它自己去抓，
        #    本地无需下载字节。若 Miraheze 未开启 $wgAllowCopyUploads
        #    （copyuploaddisabled）或抓取失败，则回退到下方「用已下载字节做 multipart 上传」，功能不退化。
        if url and url.startswith("http"):
            try:
                ext = 'jpg'
                m = re.search(r'\.([a-z0-9]{1,4})(?:[?#]|$)', url, re.I)
                if m and m.group(1).lower() in ('jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'):
                    ext = m.group(1).lower()
                r0 = sess.post(api_url, data={
                    'action': 'upload', 'url': url, 'filename': f"{base}.{ext}",
                    'token': csrf, 'comment': f'upload img {idx} (byurl)',
                    'ignorewarnings': '1', 'format': 'json',
                }, timeout=180)
                j0 = r0.json()
                if j0.get('upload', {}).get('result') == 'Success':
                    logger.info(f"[WIKI-PACK] 图片 {idx} 直传成功: {j0['upload']['filename']}")
                    return j0['upload']['filename']
                code = j0.get('error', {}).get('code')
                logger.warning(f"[WIKI-PACK] 图片 {idx} 直传不可用（{code}），回退本地下载上传")
            except Exception as e:
                logger.warning(f"[WIKI-PACK] 图片 {idx} 直传异常，回退下载上传: {e}")
        # 2) 回退：用已下载的 data 字节做 multipart 上传
        if not data:
            raise RuntimeError("图片字节为空（下载失败且直传不可用）")
        ct_map = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/gif': 'gif',
                  'image/webp': 'webp', 'image/bmp': 'bmp'}
        ext = 'jpg'
        # 用 Pillow 处理：小体积的无损图（结构式、谱图多为 PNG）原样上传，保住文字锐度；
        # 其余统一转 JPEG、最长边 2560、quality 88，减小体积以免传到境外 Miraheze 超时。
        # 压缩失败则按原格式上传。
        try:
            from PIL import Image
            im = Image.open(BytesIO(data))
            fmt = (im.format or '').upper()
            passthrough = {'PNG': 'png', 'GIF': 'gif', 'WEBP': 'webp'}.get(fmt)
            if passthrough and len(data) <= 1536 * 1024 and max(im.size) <= 2560:
                ext = passthrough
                logger.info(f"[WIKI] 图片 {idx} 为 {fmt} 且体积合适，原样上传")
            else:
                if max(im.size) > 2560:
                    im.thumbnail((2560, 2560))
                buf = BytesIO()
                im.convert('RGB').save(buf, 'JPEG', quality=88, subsampling=0)
                data = buf.getvalue()
                ext = 'jpg'
        except Exception as e:
            logger.warning(f"[WIKI-PACK] 图片 {idx} 压缩失败，按原格式上传: {e}")
            path = urllib.parse.urlparse(url).path
            base = path.rsplit('/', 1)[-1].split('?')[0]
            if '.' in base:
                e2 = base.rsplit('.', 1)[-1].lower()
                if 1 <= len(e2) <= 4 and e2.isalpha() and e2 in ct_map.values():
                    ext = e2
        fname = f"{base}.{ext}"
        mime = ct_map.get(ext, 'image/jpeg')
        params = {
            'action': 'upload',
            'filename': fname,
            'token': csrf,
            'comment': f'upload img {idx}',
            'ignorewarnings': '1',
            'format': 'json',
        }
        # curl_cffi 0.16.x 的 multipart 必须是 curl_cffi.curl.CurlMime 对象，逐项 addpart
        from curl_cffi.curl import CurlMime
        form = CurlMime()
        form.addpart('file', data=data, filename=fname, content_type=mime)
        for k, v in params.items():
            form.addpart(k, data=str(v))
        r2 = sess.post(api_url, multipart=form, timeout=180)
        if r2.status_code != 200:
            raise RuntimeError(f"上传 HTTP {r2.status_code}: {r2.text[:200]}")
        j = r2.json()
        if j.get('upload', {}).get('result') == 'Success':
            return j['upload']['filename']
        if j.get('error', {}).get('code') == 'fileexists-no-change':
            return fname
        raise RuntimeError(f"图片上传失败: {json.dumps(j, ensure_ascii=False)}")

# ---------- 公共：调用 LLM 生成归档正文 ----------
