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

class PackMixin:
    def _wiki_pack_parse_page_name(self, event: AstrMessageEvent) -> str:
        # 与本项目「回顾」指令保持一致：参数来源用 event.message_str（含完整指令词+参数），
        # 而非 event.message_obj.message 的文本段（合并转发引用场景下拿不到页面名）。
        raw = (getattr(event, 'message_str', '') or '').strip()
        for p in ("/测试wiki打包上传", "测试wiki打包上传"):
            if raw.startswith(p):
                raw = raw[len(p):].strip()
                break
        return raw

    def _get_cq_client(self, event: AstrMessageEvent):
        platform = self.context.get_platform('aiocqhttp')
        if not platform:
            return None
        if hasattr(platform, 'get_client'):
            return platform.get_client()
        if hasattr(platform, 'client'):
            return platform.client
        if hasattr(event, 'bot'):
            return event.bot
        return None

    async def _wiki_pack_get_forward_id(self, event, client) -> Optional[str]:
        reply_seg = None
        for seg in event.message_obj.message:
            if isinstance(seg, Reply):
                reply_seg = seg
                break
        if not reply_seg:
            return None
        try:
            msg_resp = await client.api.call_action('get_msg', message_id=int(reply_seg.id))
        except Exception as e:
            logger.warning(f"[WIKI-PACK] get_msg 失败: {e}")
            return None
        if not msg_resp:
            return None
        content = msg_resp.get('message', [])
        if isinstance(content, str):
            import re
            m = re.search(r'\[forward[,\s]id=([^\]]+)\]', content)
            return m.group(1) if m else None
        for seg in content:
            seg_type = seg.get('type') if isinstance(seg, dict) else getattr(seg, 'type', None)
            data = seg.get('data', {}) if isinstance(seg, dict) else getattr(seg, 'data', {})
            if str(seg_type) == 'forward':
                fid = data.get('id')
                if fid:
                    return str(fid)
        return None

    async def _collect_forward_nodes(self, client, forward_id: str) -> List[dict]:
        resp = await client.api.call_action('get_forward_msg', id=str(forward_id))
        messages = (resp.get('messages') or resp.get('message')
                    or (resp.get('data', {}) or {}).get('messages') or [])
        nodes = []
        for msg in messages:
            sender_info = msg.get('sender', {})
            sender = (sender_info.get('card') or sender_info.get('nickname')
                      or str(sender_info.get('user_id', '')) or '')
            content = msg.get('message', msg.get('content', []))
            texts = []
            image_urls = []
            image_files = []
            if isinstance(content, str):
                import re
                def _unescape(s):
                    return (s.replace('&#44;', ',').replace('&#91;', '[')
                             .replace('&#93;', ']').replace('&amp;', '&'))
                for tm in re.findall(r'\[CQ:text,text=([^\]]*)\]', content):
                    texts.append(_unescape(tm))
                for im in re.findall(r'\[CQ:image,[^\]]*\]', content):
                    kv = dict(re.findall(r'(\w+)=([^,\]]*)', im))
                    url = _unescape(kv.get('url', ''))
                    fid = _unescape(kv.get('file', '') or kv.get('file_id', ''))
                    if url or fid:
                        image_urls.append(url)
                        image_files.append(fid)
            else:
                for seg in content:
                    seg_type = seg.get('type') if isinstance(seg, dict) else getattr(seg, 'type', None)
                    data = seg.get('data', {}) if isinstance(seg, dict) else getattr(seg, 'data', {})
                    t = str(seg_type)
                    if t == 'text':
                        texts.append(data.get('text', ''))
                    elif t == 'image':
                        url = data.get('url', '')
                        if url:
                            image_urls.append(url)
                            image_files.append(data.get('file', '') or data.get('file_id', '') or '')
                    # forward / reply / 其他：按需求忽略
            nodes.append({'sender': sender, 'texts': texts,
                          'image_urls': image_urls, 'image_files': image_files})
        return nodes

    async def _wiki_pack_download_image(self, client, url: str, file_id: str) -> bytes:
        """下载一张 QQ 图片为字节。优先用 OneBot 的 get_image（机器人本身能访问 QQ CDN），
        失败再回退到直连 URL。"""
        # 1) OneBot get_image：file_id 是图片段的 file 字段（机器人本机缓存，无需直连 QQ CDN）
        if client is not None and file_id:
            try:
                resp = await client.api.call_action('get_image', file=file_id)
                local = ''
                if isinstance(resp, dict):
                    local = resp.get('file') or resp.get('path') or ''
                    if not local and isinstance(resp.get('data'), dict):
                        local = resp['data'].get('file') or resp['data'].get('path') or ''
                if local:
                    data = await asyncio.to_thread(self._read_if_exists, local)
                    if data:
                        return data
                    logger.warning(f"[WIKI-PACK] get_image 返回路径但读不到: {local}")
            except Exception as e:
                logger.warning(f"[WIKI-PACK] get_image 失败 file={file_id}: {e}")
        # 2) 回退：直连 URL（服务器直连 QQ CDN 可能超时，给 60s）
        if url:
            from curl_cffi.requests import Session
            try:
                s = Session(impersonate='chrome')
                s.headers['User-Agent'] = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                                           'Chrome/120.0.0.0 Safari/537.36')
                r = await asyncio.to_thread(s.get, url, timeout=60)
                if r.status_code == 200 and r.content:
                    return r.content
            except Exception as e:
                logger.warning(f"[WIKI-PACK] 直连下载失败 url={url[:60]}: {e}")
        return b''

    @staticmethod

    def _read_if_exists(self, path: str):
        try:
            with open(path, 'rb') as f:
                return f.read()
        except Exception:
            return None

    def _wiki_pack_upload(self, api_url, username, password, page_title, nodes,
                          image_data: dict) -> str:
        import json
        import urllib.parse
        from curl_cffi.requests import Session

        sess = Session(impersonate='chrome', timeout=180)
        sess.headers['User-Agent'] = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                                      'Chrome/120.0.0.0 Safari/537.36')

        def call(params, is_post=False, files=None):
            if files is not None:
                r = sess.post(api_url, data=params, files=files)
            elif is_post:
                r = sess.post(api_url, data=params)
            else:
                r = sess.get(api_url, params=params)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            return r.json()

        j = call({'action': 'query', 'meta': 'tokens', 'type': 'login', 'format': 'json'})
        lt = j['query']['tokens']['logintoken']
        j = call({'action': 'login', 'lgname': username, 'lgpassword': password,
                  'lgtoken': lt, 'format': 'json'}, is_post=True)
        if j.get('login', {}).get('result') != 'Success':
            raise RuntimeError(f"登录失败: {json.dumps(j, ensure_ascii=False)}")
        j = call({'action': 'query', 'meta': 'tokens', 'type': 'csrf', 'format': 'json'})
        csrf = j['query']['tokens']['csrftoken']

        image_map = {}
        img_index = 0
        upload_errors = []
        for node in nodes:
            for url in node['image_urls']:
                if url in image_map:
                    continue
                img_index += 1
                data = image_data.get(url)
                if not data:
                    logger.warning(f"[WIKI-PACK] 图片 {img_index} 无可用字节（下载失败），跳过上传")
                    upload_errors.append(f"图{img_index}")
                    continue
                try:
                    fname = self._wiki_upload_one_image(sess, api_url, url, data, img_index, csrf)
                    image_map[url] = fname
                except Exception as e:
                    logger.warning(f"[WIKI-PACK] 图片 {img_index} 上传失败: {e}")
                    upload_errors.append(f"图{img_index}")

        lines = []
        for node in nodes:
            sender = (node.get('sender', '') or '').replace('=', '＝')
            if sender:
                lines.append(f"=== {sender} ===")
            for t in node['texts']:
                t = (t or '').strip()
                if t:
                    lines.append(" " + t)
                    lines.append("")
            for url in node['image_urls']:
                fn = image_map.get(url)
                if fn:
                    lines.append(f"[[File:{fn}|thumb|center]]")
                else:
                    lines.append("[[File:上传失败]]")
            lines.append("")
            lines.append("----")
            lines.append("")
        wikitext = "\n".join(lines).strip()

        j = call({'action': 'edit', 'title': page_title, 'text': wikitext,
                  'summary': 'test: 合并转发打包上传', 'token': csrf,
                  'assert': 'user', 'format': 'json'}, is_post=True)
        if j.get('edit', {}).get('result') == 'Success':
            newrev = j['edit'].get('newrevid')
            info = f"已写入 {page_title}（{len(wikitext)} 字节，新版本 {newrev}）。"
            if upload_errors:
                info += f" 但有 {len(upload_errors)} 张图片上传失败：{','.join(upload_errors)}"
            return info
        else:
            raise RuntimeError(f"编辑失败: {json.dumps(j, ensure_ascii=False)}")
