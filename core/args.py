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

class ArgsMixin:
    def _parse_args(self, args: List[str]) -> dict:
        logger.info(f"[BRANCH] _parse_args 进入, args={args}")
        if len(args) == 3:
            logger.info("[BRANCH] _parse_args 模式: 3参数（时间模式）")
            start_str, end_str, keyword = args[0], args[1], args[2]
            start_dict = self._parse_time_str(start_str)
            end_dict = self._parse_time_str(end_str)
            if all(v is None for v in start_dict.values()) or all(v is None for v in end_dict.values()):
                logger.info("[BRANCH] _parse_args 时间解析失败，抛出异常")
                raise ValueError("时间格式无法解析，请使用 YYYY-MM-DD、YYYY-MM-DD_HH:MM、MM-DD 或 HH:MM")
            try:
                start_ts, end_ts = self._normalize_times(start_dict, end_dict)
                logger.info(f"[BRANCH] _parse_args 时间戳: start={start_ts}, end={end_ts}")
            except Exception as e:
                logger.info(f"[BRANCH] _parse_args 时间补全失败: {e}")
                raise ValueError(f"时间补全失败: {e}")
            if start_ts >= end_ts:
                logger.info("[BRANCH] _parse_args 开始时间 >= 结束时间，抛出异常")
                raise ValueError("开始时间必须早于结束时间")
            return {'mode': 'time', 'start': start_ts, 'end': end_ts, 'keyword': keyword}
        elif len(args) == 1:
            logger.info("[BRANCH] _parse_args 模式: 1参数（引用模式）")
            return {'mode': 'quote', 'keyword': args[0]}
        else:
            logger.info(f"[BRANCH] _parse_args 参数数量错误: {len(args)}，抛出异常")
            raise ValueError(f"参数数量错误（需要 1 个或 3 个，实际 {len(args)} 个）")

    # ---------- 辅助提取 ----------

    def _extract_plain_text(self, chain: List) -> str:
        logger.info("[BRANCH] _extract_plain_text 进入")
        texts = []
        for seg in chain:
            if hasattr(seg, 'type'):
                seg_type = seg.type
                data = seg.data if hasattr(seg, 'data') else {}
            else:
                seg_type = seg.get('type')
                data = seg.get('data', {})
            if seg_type == 'text':
                texts.append(data.get('text', ''))
        result = ''.join(texts).strip()
        logger.info(f"[BRANCH] _extract_plain_text 返回: {result[:30]}...")
        return result

    def _extract_image_urls(self, chain: List) -> List[str]:
        logger.info("[BRANCH] _extract_image_urls 进入")
        urls = []
        for seg in chain:
            if hasattr(seg, 'type'):
                seg_type = seg.type
                data = seg.data if hasattr(seg, 'data') else {}
            else:
                seg_type = seg.get('type')
                data = seg.get('data', {})
            if seg_type == 'image':
                url = data.get('url', '')
                if not url:
                    file_val = data.get('file', '')
                    if file_val.startswith('http'):
                        url = file_val
                if url:
                    urls.append(url)
        logger.info(f"[BRANCH] _extract_image_urls 返回 {len(urls)} 个 URL")
        return urls

    # ---------- 公共核心方法 ----------
