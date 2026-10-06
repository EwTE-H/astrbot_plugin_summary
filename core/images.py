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

class ImageMixin:
    def _ext_of_bytes(self, data: bytes) -> str:
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return 'png'
        if data[:3] == b'GIF8':
            return 'gif'
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            return 'webp'
        if data[:2] == b'\xff\xd8':
            return 'jpg'
        if data[:4] == b'\x00\x00\x01\x00':
            return 'ico'
        return 'jpg'

    async def _materialize_images(self, image_meta: list) -> None:
        """把 image_meta 里的图片全部下载到本地临时文件，写入 meta['local']。

        目的：QQ 图片直链（gchat/multimedia，带 rkey）有效期很短，若把 URL 直接交给
        LLM 提供方，任何一张下载失败都会被静默跳过，导致「第 N 张图」与 [图#N] 整体错位
        ——这正是之前归档贴错图的根因。先落地成本地文件，再以 file:// 传给 LLM，
        顺序就不会变；失败的那张我们明确知道，并在清单里标注「不可用」。
        """
        import tempfile
        if not image_meta:
            return
        tmpdir = tempfile.mkdtemp(prefix='astrbot_reviewimg_')
        sem = asyncio.Semaphore(6)

        def _download(url: str):
            from curl_cffi.requests import Session
            ua = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
            last = None
            for _ in range(3):
                try:
                    s = Session(impersonate='chrome', timeout=30)
                    s.headers['User-Agent'] = ua
                    r = s.get(url)
                    if r.status_code == 200 and r.content:
                        return r.content
                    last = f'HTTP {r.status_code}'
                except Exception as e:
                    last = str(e)
            raise RuntimeError(last or '下载失败')

        async def _one(meta):
            async with sem:
                try:
                    data = await asyncio.to_thread(_download, meta['url'])
                    ext = self._ext_of_bytes(data)
                    path = os.path.join(tmpdir, f"img_{meta['n']}.{ext}")
                    with open(path, 'wb') as f:
                        f.write(data)
                    meta['local'] = 'file://' + path
                    logger.info(f"[IMG] 图#{meta['n']} 本地化成功: {path} ({len(data)}B)")
                except Exception as e:
                    meta['local'] = None
                    logger.warning(f"[IMG] 图#{meta['n']} 本地化失败: {e}")

        await asyncio.gather(*[_one(m) for m in image_meta])
        ok = sum(1 for m in image_meta if m.get('local'))
        logger.info(f"[IMG] 图片本地化完成 {ok}/{len(image_meta)}")

    def _build_image_manifest(self, image_meta: list, only: list = None,
                              attached: bool = True) -> str:
        """生成给 LLM 看的「附图清单」：编号 + 发言人 + 时间 + 上下文。

        attached=True：图片本体确实附在本条消息末尾，需声明「第 k 张附图就是 [图#k]」。
        attached=False：不附图（省 token），只给编号与上下文，模型靠上下文定位插图位置。
        """
        if not image_meta:
            return ""
        # 默认只列「已成功下载到本地」的图，保证清单里的编号一定可用；
        # only 显式指定时按调用方给的编号列（调用方已确保这些图可用）。
        if only is None:
            items = [m for m in image_meta if m.get('local')]
        else:
            items = [m for m in image_meta if m['n'] in only and m.get('local')]
        if not items:
            return ""
        if attached:
            lines = ["", "== 附图清单 ==",
                     "下面列出本次记录中的图片，并'''按编号升序'''依次附在本条消息末尾",
                     "（第 k 张附图就是 [图#k]）。引用图片时必须原样照抄这里的标记。"]
        else:
            lines = ["", "== 可用图片清单 ==",
                     "下面列出本次记录中的图片（编号 + 发言时间 + 发言人 + 发言上下文）。",
                     "正文中需要插图时，'''原样照抄'''这里的 [图#N] 标记即可，编号即指代该条消息所发的那张图。"]
        for m in items:
            src = f"{m.get('ts','')} {m.get('sender','')}".strip()
            ctx = (m.get('ctx') or '').strip()
            status = '' if m.get('local') else '（该图已失效，禁止引用）'
            head = f"* [图#{m['n']}] 来源：{src}"
            if ctx:
                head += f"，上下文：{ctx}"
            lines.append(head + status)
        return "\n".join(lines)

    # ---------- 统一准备消息（含引用检测） ----------
