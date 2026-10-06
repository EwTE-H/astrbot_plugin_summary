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

# ---------- 指令：回顾 ----------
