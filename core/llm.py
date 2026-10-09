import os
import time
import json
import re
import asyncio
import base64
import urllib.request
import urllib.error
from datetime import datetime, date
from typing import List, Optional, Tuple, Dict
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Reply

class LLMMixin:
    async def _run_summary_llm(self, event: AstrMessageEvent, system_prompt: str,
                               user_prompt: str, all_images: List[str], tag: str = "SUMMARY",
                               stream: bool = True) -> str:
        """流式调用 LLM 生成归档正文；流式为空时回退非流式。返回正文字符串。

        stream=False 时直接走非流式（多图场景下部分 provider 流式会返回空，省一次空等）。
        """
        provider = await self.context.get_using_provider_async(umo=event.unified_msg_origin)

        if not stream:
            logger.info(f"[{tag}] 直接非流式调用")
            llm_resp = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
                image_urls=all_images if all_images else None
            )
            text = llm_resp.completion_text or ""
            logger.info(f"[{tag}] 非流式结束，总长度 {len(text)}")
            return text

        full_response = ""
        buffer = ""
        chunk_counter = 0
        log_threshold = 100

        logger.info(f"[{tag}] 开始流式调用")
        stream_gen = provider.text_chat_stream(
            prompt=user_prompt,
            system_prompt=system_prompt,
            image_urls=all_images if all_images else None
        )
        async for chunk in stream_gen:
            if chunk.is_chunk:
                chunk_text = chunk.completion_text
                if chunk_text:
                    full_response += chunk_text
                    buffer += chunk_text
                    chunk_counter += 1
                    if len(buffer) >= log_threshold:
                        logger.info(f"[{tag}] 流式片段 #{chunk_counter}: {buffer}")
                        buffer = ""
        if buffer:
            logger.info(f"[{tag}] 剩余缓冲区: {buffer}")
        logger.info(f"[{tag}] 流式结束，总长度 {len(full_response)}")

        if not full_response.strip():
            logger.warning(f"[{tag}] 流式响应为空，尝试非流式")
            llm_resp = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
                image_urls=all_images if all_images else None
            )
            full_response = llm_resp.completion_text or ""
        return full_response

    async def _run_summary_glm_direct(self, system_prompt: str, user_prompt: str,
                                      image_meta: list, glm_api_key: str,
                                      glm_model: str = "glm-5.3-flash",
                                      glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4") -> str:
        """直连 GLM API（绕过 AStrBot）生成图文混排归档。返回正文字符串。

        与 _run_summary_llm 的唯一区别：不走 AStrBot provider，自己 POST GLM Chat
        Completion 端点，按 user_prompt 中 [图#N] 出现位置把图片穿插进 content[]，
        实现真正的图文混排（而非把图整体堆在末尾）。图片来源：优先 m['url']（QQ 原链，
        http(s) 合规），缺失时回退读 m['local']（file:// 本地路径）转 Base64。
        """
        if not glm_api_key:
            raise ValueError("未配置 glm_api_key（请在插件配置项填写）")

        base_url = glm_base_url.rstrip('/')
        endpoint = base_url + "/chat/completions"

        by_num = {m['n']: m for m in image_meta if 'n' in m}

        def _img_url(m: dict):
            url = (m.get('url') or '').strip()
            if url.startswith('http://') or url.startswith('https://'):
                return url
            local = m.get('local') or ''
            if local:
                path = local[len('file://'):] if local.startswith('file://') else local
                try:
                    with open(path, 'rb') as f:
                        data = f.read()
                    ext = os.path.splitext(path)[1].lower().lstrip('.') or 'jpg'
                    mime = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
                            'gif': 'image/gif', 'webp': 'image/webp', 'bmp': 'image/bmp'}.get(ext, 'image/jpeg')
                    b64 = base64.b64encode(data).decode('ascii')
                    return f"data:{mime};base64,{b64}"
                except Exception as e:
                    logger.warning(f"[GLM-DIRECT] 读取本地图失败 n={m.get('n')}: {e}")
            return None

        # 图文混排：按 user_prompt 中 [图#N] 出现顺序穿插 image_url 块；重复编号只发一次
        segments = re.split(r'(\[图#(\d+)\])', user_prompt)
        content = []
        seen = set()
        for i in range(0, len(segments), 3):
            text = segments[i]
            if text:
                content.append({"type": "text", "text": text})
            if i + 1 < len(segments):
                marker = segments[i + 1]
                num = int(segments[i + 2])
                content.append({"type": "text", "text": marker})  # 让模型看到 [图#N] 标记
                if num in seen:
                    continue
                seen.add(num)
                m = by_num.get(num)
                if m:
                    u = _img_url(m)
                    if u:
                        content.append({"type": "image_url", "image_url": {"url": u}})
                    else:
                        logger.warning(f"[GLM-DIRECT] 图#{num} 无可用 url/local，跳过")
                else:
                    logger.warning(f"[GLM-DIRECT] 图#{num} 不在 image_meta，跳过")

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ]
        payload = {
            "model": glm_model,
            "messages": messages,
            "thinking": {"type": "enabled", "clear_thinking": True},
            "reasoning_effort": "low",
            "temperature": 0,
            "top_p": 0.95,
            "stream": True,
        }

        # ---- 输入打印至日志（图片块只打 URL/长度，避免 base64 刷屏）----
        logger.info(f"[GLM-DIRECT] 请求 model={glm_model} stream=True reasoning_effort=low "
                    f"temperature=0 clear_thinking=True top_p={payload['top_p']}")
        logger.info(f"[GLM-DIRECT] SYSTEM: {system_prompt}")
        for _blk in content:
            if _blk.get("type") == "text":
                logger.info(f"[GLM-DIRECT] USER-TEXT: {_blk['text']}")
            elif _blk.get("type") == "image_url":
                _u = _blk["image_url"]["url"]
                if _u.startswith("data:"):
                    logger.info(f"[GLM-DIRECT] USER-IMG: base64 长度={len(_u)}")
                else:
                    logger.info(f"[GLM-DIRECT] USER-IMG: {_u[:120]}{'...' if len(_u) > 120 else ''}")

        def _call_stream():
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                endpoint, data=data, method='POST',
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {glm_api_key}"})
            full = ""
            buffer = ""
            threshold = 100
            with urllib.request.urlopen(req, timeout=300) as resp:
                for raw in resp:
                    line = raw.decode('utf-8', 'replace').strip()
                    if not line or not line.startswith('data:'):
                        continue
                    chunk = line[5:].strip()
                    if chunk == '[DONE]':
                        break
                    try:
                        obj = json.loads(chunk)
                    except Exception:
                        continue
                    choices = obj.get('choices') or []
                    if not choices:
                        continue
                    delta = choices[0].get('delta', {}) or {}
                    piece = delta.get('content') or ''
                    if piece:
                        full += piece
                        buffer += piece
                        if len(buffer) >= threshold:
                            logger.info(f"[GLM-DIRECT] 流式片段: {buffer}")
                            buffer = ""
            if buffer:
                logger.info(f"[GLM-DIRECT] 流式剩余: {buffer}")
            return full

        try:
            logger.info(f"[GLM-DIRECT] 调用 {endpoint}（流式）内容块数={len(content)}")
            text = await asyncio.to_thread(_call_stream)
            logger.info(f"[GLM-DIRECT] 返回总长度 {len(text)}")
            return text
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:600]
            logger.error(f"[GLM-DIRECT] HTTP {e.code}: {detail}")
            raise RuntimeError(f"GLM API 返回 {e.code}: {detail}")
        except Exception as e:
            logger.error(f"[GLM-DIRECT] 调用异常: {e}", exc_info=True)
            raise

# ---------- 指令：回顾 ----------
