import os
import time
import json
import re
import asyncio
from datetime import datetime, date
from typing import List, Optional, Tuple, Dict

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Reply
import coverage
class PluginSummary(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.default_days = 2
        self.max_messages = 1000
        self.max_text_chars = 50000
        self.max_images = 20
        self.max_upload_images = 20   # 单次归档最多往 wiki 传几张图（与 max_images 一致，即不额外限制）
        logger.info("[BRANCH] PluginSummary __init__ 完成")
                # 启动 coverage，只统计本插件目录
        self.cov = coverage.Coverage(source=['.'])  # 或者写插件所在目录的绝对路径
        self.cov.start()
        logger.info("[BRANCH] Coverage started")

    # ---------- 辅助：获取引用消息文本 ----------
    async def _get_reply_text(self, client, reply_id) -> str:
        logger.info(f"[BRANCH] _get_reply_text 进入, reply_id={reply_id}, type={type(reply_id)}")
        try:
            msg_id = int(reply_id)
            logger.info(f"[BRANCH] _get_reply_text 转换成功: {msg_id}")
        except Exception as e:
            logger.info(f"[BRANCH] _get_reply_text 转换失败: {e}")
            return "[引用消息ID无效]"
        try:
            logger.info("[BRANCH] _get_reply_text 调用 get_msg API")
            resp = await client.api.call_action('get_msg', message_id=msg_id)
            logger.info(f"[BRANCH] _get_reply_text 响应类型: {type(resp)}")
            if not resp:
                logger.info("[BRANCH] _get_reply_text 响应为空")
                return "[引用消息获取失败]"
            sender_info = resp.get('sender', {})
            sender = sender_info.get('card') or sender_info.get('nickname') or str(sender_info.get('user_id', '未知'))
            content = resp.get('message', [])
            texts = []
            for seg in content:
                if isinstance(seg, dict):
                    seg_type = seg.get('type')
                    data = seg.get('data', {})
                else:
                    seg_type = getattr(seg, 'type', '')
                    data = getattr(seg, 'data', {})
                if seg_type == 'text':
                    texts.append(data.get('text', ''))
                elif seg_type == 'image':
                    texts.append('[图片]')
                else:
                    logger.info(f"[BRANCH] _get_reply_text 忽略非文本/图片段: {seg_type}")
            text_content = ''.join(texts).strip() or '[空消息]'
            result = f"回复 @{sender}: {text_content}"
            logger.info(f"[BRANCH] _get_reply_text 成功: {result[:50]}...")
            return result
        except Exception as e:
            logger.warning(f"[BRANCH] _get_reply_text 异常: {e}")
            return "[引用消息获取失败]"

    # ---------- 图片编号注册（全局统一，保证 [图#N] 与图片列表严格一一对应） ----------
    def _register_image(self, url: str, image_counter: list, image_urls: list,
                        image_meta: list = None, url_index: dict = None,
                        sender: str = '', ts: str = '', ctx: str = '') -> int:
        """登记一张图片并返回它的编号。

        所有产生 [图#N] 标记的地方（普通消息、合并转发递归）都必须走这里，
        保证「编号 N」永远等于 image_urls[N-1]，不会出现编号与图片错位。
        - 相同 URL 只登记一次（复用同一编号），避免同一张图被编成多个号；
        - 超过 max_images 时返回 -1（调用方不写标记，也不登记图片）。
        """
        if not url:
            return -1
        if url_index is not None and url in url_index:
            return url_index[url]
        if image_urls is not None and len(image_urls) >= self.max_images:
            logger.info(f"[IMG] 图片已达上限 {self.max_images}，不再登记新图片")
            return -1
        n = image_counter[0]
        image_counter[0] += 1
        if image_urls is not None:
            image_urls.append(url)
        if url_index is not None:
            url_index[url] = n
        if image_meta is not None:
            image_meta.append({
                'n': n, 'url': url, 'sender': sender or '', 'ts': ts or '',
                'ctx': (ctx or '').replace('\n', ' ')[:120], 'local': None, 'cap': '',
            })
        return n

    # ---------- 消息解析（递归展开合并转发） ----------
    async def _parse_messages(self, messages: list, client, depth: int, image_counter: list, image_urls: list,
                              image_meta: list = None, url_index: dict = None) -> List[str]:
        logger.info(f"[BRANCH] _parse_messages 进入, depth={depth}, 消息数={len(messages)}")
        if messages:
            logger.info(f"[BRANCH] _parse_messages 第一条消息类型: {type(messages[0])}")
        result = []
        for idx, msg in enumerate(messages):
            logger.info(f"[BRANCH] _parse_messages 处理消息 #{idx+1}")
            sender_info = msg.get('sender', {})
            sender = sender_info.get('card') or sender_info.get('nickname') or str(sender_info.get('user_id', '未知'))
            content = msg.get('message', msg.get('content', []))
            if isinstance(content, list):
                logger.info(f"[BRANCH] _parse_messages content 是 list, 长度={len(content)}")
            else:
                logger.info(f"[BRANCH] _parse_messages content 不是 list, 类型={type(content)}")
            text_parts = []
            nested_parts = []

            for seg_idx, seg in enumerate(content):
                seg_type = None
                data = {}
                if isinstance(seg, dict):
                    seg_type = seg.get('type')
                    data = seg.get('data', {})
                else:
                    logger.info(f"[BRANCH] _parse_messages seg 不是 dict, 类型={type(seg)}")
                    if hasattr(seg, 'type'):
                        seg_type = seg.type
                        data = seg.data if hasattr(seg, 'data') else {}
                    else:
                        seg_type = getattr(seg, 'type', None)
                        data = getattr(seg, 'data', {})
                seg_type_str = str(seg_type) if seg_type is not None else ''
                logger.info(f"[BRANCH] _parse_messages 段 #{seg_idx+1} type='{seg_type_str}'")

                if seg_type_str == 'text':
                    logger.info("[BRANCH] _parse_messages -> 分支: text")
                    text_parts.append(data.get('text', ''))
                elif seg_type_str == 'image':
                    logger.info("[BRANCH] _parse_messages -> 分支: image")
                    url = data.get('url', '')
                    if not url:
                        fv = data.get('file', '')
                        if isinstance(fv, str) and fv.startswith('http'):
                            url = fv
                    if url:
                        img_num = self._register_image(
                            url, image_counter, image_urls, image_meta, url_index,
                            sender=sender, ts=self._fmt_ts(msg.get('time', 0)),
                            ctx=(' '.join(text_parts)[:100] or '(合并转发内)'))
                        if img_num > 0:
                            text_parts.append(f"[图#{img_num}]")
                    else:
                        logger.info("[BRANCH] _parse_messages image 无 url")
                elif seg_type_str == 'forward':
                    logger.info("[BRANCH] _parse_messages -> 分支: forward")
                    if 'content' in data and data['content']:
                        logger.info("[BRANCH] _parse_messages forward 有 content 字段，递归解析")
                        nested = await self._parse_messages(data['content'], client, depth + 1,
                                                            image_counter, image_urls, image_meta, url_index)
                        if nested:
                            nested_str = " | ".join(nested)
                            nested_parts.append(f"( {nested_str} )")
                    elif 'id' in data:
                        logger.info(f"[BRANCH] _parse_messages forward 有 id 字段，调用 _extract_forward_msg, id={data['id']}")
                        nested = await self._extract_forward_msg(client, data['id'], depth + 1, image_counter,
                                                                 image_urls, image_meta, url_index)
                        if nested:
                            nested_str = " | ".join(nested)
                            nested_parts.append(f"( {nested_str} )")
                    else:
                        logger.info("[BRANCH] _parse_messages forward 既无 content 也无 id，跳过")
                elif seg_type_str == 'reply':
                    # 合并转发内的引用直接摆烂，不查找任何消息
                    logger.info("[BRANCH] _parse_messages -> 分支: reply，直接忽略引用内容")
                    text_parts.append("[引用]")
                else:
                    logger.info(f"[BRANCH] _parse_messages -> 其他类型: {seg_type_str}")

            # 组装该消息的文本
            has_self = bool(text_parts)
            self_str = ""
            if has_self:
                self_str = f"[{sender}] " + " ".join(text_parts)
            nested_combined = " ".join(nested_parts) if nested_parts else ""
            if has_self and nested_combined:
                result.append(f"{self_str} {nested_combined}")
                logger.info(f"[BRANCH] _parse_messages 组装结果: 有自身文本且有嵌套")
            elif has_self:
                result.append(self_str)
                logger.info(f"[BRANCH] _parse_messages 组装结果: 仅自身文本")
            elif nested_combined:
                result.append(nested_combined)
                logger.info(f"[BRANCH] _parse_messages 组装结果: 仅嵌套")
            else:
                logger.info(f"[BRANCH] _parse_messages 组装结果: 空消息，不添加")
        logger.info(f"[BRANCH] _parse_messages 返回 {len(result)} 条")
        return result
    async def _extract_forward_msg(self, client, forward_id: str, depth: int = 0, image_counter: list = None,
                                   image_urls: list = None, image_meta: list = None, url_index: dict = None) -> List[str]:
        logger.info(f"[BRANCH] _extract_forward_msg 进入, forward_id={forward_id}, depth={depth}")
        if depth > 5:
            logger.info("[BRANCH] _extract_forward_msg 嵌套过深，返回")
            return ["[合并转发嵌套过深]"]
        try:
            logger.info("[BRANCH] _extract_forward_msg 调用 get_forward_msg")
            resp = await client.api.call_action('get_forward_msg', id=str(forward_id))
            logger.info(f"[BRANCH] _extract_forward_msg 响应类型: {type(resp)}")
            messages = []
            if isinstance(resp, dict):
                if 'messages' in resp:
                    messages = resp['messages']
                    logger.info("[BRANCH] _extract_forward_msg 从 resp['messages'] 获取")
                elif 'message' in resp:
                    messages = resp['message']
                    logger.info("[BRANCH] _extract_forward_msg 从 resp['message'] 获取")
                elif 'data' in resp and isinstance(resp['data'], dict):
                    messages = resp['data'].get('messages', resp['data'].get('message', []))
                    logger.info("[BRANCH] _extract_forward_msg 从 resp['data'] 中获取")
                else:
                    logger.info("[BRANCH] _extract_forward_msg 未找到 messages 字段")
            else:
                logger.info("[BRANCH] _extract_forward_msg 响应不是 dict")
            if not messages:
                logger.info("[BRANCH] _extract_forward_msg messages 为空")
                return ["[合并转发内容为空]"]
            logger.info(f"[BRANCH] _extract_forward_msg 获取到 {len(messages)} 条内部消息")
            # 不再向 cache 添加索引
            result = await self._parse_messages(messages, client, depth + 1, image_counter,
                                                image_urls, image_meta, url_index)
            logger.info(f"[BRANCH] _extract_forward_msg 返回 {len(result)} 条")
            return result
        except Exception as e:
            logger.error(f"[BRANCH] _extract_forward_msg 异常: {e}")
            return ["[合并转发展开失败]"]
    # ---------- 发送合并转发 ----------
    async def _send_as_forward(self, event: AstrMessageEvent, text: str, title: str = ""):
        logger.info("[BRANCH] _send_as_forward 进入")
        if not text:
            logger.info("[BRANCH] _send_as_forward text 为空，直接返回")
            return

        full_content =  text
        max_len = 3000
        chunks = [full_content[i:i+max_len] for i in range(0, len(full_content), max_len)]
        logger.info(f"[BRANCH] _send_as_forward 拆分为 {len(chunks)} 块")

        nodes = []
        bot_user_id = 10086
        for chunk in chunks:
            nodes.append({
                "type": "node",
                "data": {
                    "user_id": bot_user_id,
                    "nickname": "Bot",
                    "content": [{"type": "text", "data": {"text": chunk}}]
                }
            })

        platform = self.context.get_platform('aiocqhttp')
        if not platform:
            logger.info("[BRANCH] _send_as_forward 未找到 aiocqhttp 平台，降级普通发送")
            for chunk in chunks:
                await event.send(event.plain_result(chunk))
            return

        client = None
        if hasattr(platform, 'get_client'):
            client = platform.get_client()
            logger.info("[BRANCH] _send_as_forward 通过 get_client 获取 client")
        elif hasattr(platform, 'client'):
            client = platform.client
            logger.info("[BRANCH] _send_as_forward 通过 platform.client 获取 client")
        elif hasattr(event, 'bot'):
            client = event.bot
            logger.info("[BRANCH] _send_as_forward 通过 event.bot 获取 client")
        if not client:
            logger.info("[BRANCH] _send_as_forward client 获取失败，降级普通发送")
            for chunk in chunks:
                await event.send(event.plain_result(chunk))
            return

        group_id = event.message_obj.group_id
        if not group_id:
            logger.info("[BRANCH] _send_as_forward 无 group_id，降级普通发送")
            for chunk in chunks:
                await event.send(event.plain_result(text))
            return

        try:
            logger.info("[BRANCH] _send_as_forward 调用 send_group_forward_msg")
            await client.api.call_action(
                'send_group_forward_msg',
                **{
                    'group_id': int(group_id),
                    'message': nodes
                }
            )
            logger.info("[BRANCH] _send_as_forward 发送成功")
        except Exception as e:
            logger.error(f"[BRANCH] _send_as_forward 发送合并转发失败: {e}")
            for chunk in chunks:
                await event.send(event.plain_result(chunk))

    # ---------- 拉取消息并过滤 ----------
    async def _fetch_and_filter(self, client, group_id: int, start_time: Optional[int] = None, end_time: Optional[int] = None, self_id: Optional[int] = None):
        logger.info(f"[BRANCH] _fetch_and_filter 进入, group_id={group_id}, start_time={start_time}, end_time={end_time}, self_id={self_id}")
        now = time.time()
        if start_time is None:
            start_time = now - self.default_days * 86400
            logger.info(f"[BRANCH] _fetch_and_filter start_time 为 None，设为 {start_time}")
        if end_time is None:
            end_time = now
            logger.info(f"[BRANCH] _fetch_and_filter end_time 为 None，设为 {end_time}")

        all_messages = []
        seen_message_ids = set()   # 分页去重用
        next_anchor = None         # SnowLuma 用 message_id 做历史锚点
        max_loops = 20
        per_page = 200             # SnowLuma 单次上限约 200，写 1000 也会被截断

        logger.info(f"[BRANCH] _fetch_and_filter 目标区间: {datetime.fromtimestamp(start_time)} ~ {datetime.fromtimestamp(end_time)}")

        while max_loops > 0:
            logger.info(f"[BRANCH] _fetch_and_filter 循环第 {21-max_loops} 次, next_anchor={next_anchor}")
            params = {'group_id': int(group_id), 'count': per_page}
            if next_anchor is not None:
                # SnowLuma 锚点参数名是 message_id（可能为负），不是 message_seq
                params['message_id'] = next_anchor
                params['reverse_order'] = True
                logger.info(f"[BRANCH] _fetch_and_filter 使用 message_id={next_anchor} 和 reverse_order=True")

            resp = await client.api.call_action('get_group_msg_history', **params)
            logger.info(f"[BRANCH] _fetch_and_filter 响应类型: {type(resp)}")

            if isinstance(resp, dict) and 'messages' in resp:
                msgs = resp['messages']
                logger.info("[BRANCH] _fetch_and_filter 从 resp['messages'] 获取")
            elif isinstance(resp, dict) and 'data' in resp and isinstance(resp['data'], dict):
                msgs = resp['data'].get('messages', [])
                logger.info("[BRANCH] _fetch_and_filter 从 resp['data']['messages'] 获取")
            else:
                logger.info("[BRANCH] _fetch_and_filter 无法解析响应，跳出")
                break

            if not msgs:
                logger.info("[BRANCH] _fetch_and_filter msgs 为空，跳出")
                break

            msgs.sort(key=lambda x: x.get('time', 0))

            # SnowLuma 的 reverse_order=True 会“包含锚点本身”，必须去重
            new_msgs = []
            for m in msgs:
                mid = m.get('message_id')
                if mid is None:
                    new_msgs.append(m)
                    continue
                if mid in seen_message_ids:
                    continue
                seen_message_ids.add(mid)
                new_msgs.append(m)

            all_messages.extend(new_msgs)
            logger.info(f"[BRANCH] _fetch_and_filter 本次获取 {len(msgs)} 条（新增 {len(new_msgs)} 条），累计 {len(all_messages)} 条")

            oldest_in_batch = msgs[0]
            oldest_time = oldest_in_batch.get('time', 0)
            if oldest_time <= start_time:
                logger.info(f"[BRANCH] _fetch_and_filter 最旧时间 {oldest_time} <= start_time，停止")
                break

            oldest_mid = oldest_in_batch.get('message_id')
            if oldest_mid is None:
                logger.info("[BRANCH] _fetch_and_filter 最旧消息无 message_id，无法继续翻页，停止")
                break

            # 若本批最旧消息 == 上一轮锚点，说明已经没有更旧的消息了
            if next_anchor is not None and oldest_mid == next_anchor:
                logger.info(f"[BRANCH] _fetch_and_filter 最旧消息仍为锚点 {oldest_mid}，无更旧消息，停止")
                break

            next_anchor = oldest_mid
            logger.info(f"[BRANCH] _fetch_and_filter 设置 next_anchor={next_anchor}")

            max_loops -= 1
        else:
            logger.info("[BRANCH] _fetch_and_filter 循环因 max_loops 耗尽退出")

        logger.info(f"[BRANCH] _fetch_and_filter 共拉取 {len(all_messages)} 条原始消息")

        if self_id is not None:
            original_len = len(all_messages)
            all_messages = [m for m in all_messages if m.get('sender', {}).get('user_id') != self_id]
            logger.info(f"[BRANCH] _fetch_and_filter 过滤机器人自身，过滤前 {original_len}，过滤后 {len(all_messages)}")
        else:
            logger.info("[BRANCH] _fetch_and_filter self_id 为 None，不进行过滤")

        filtered = [m for m in all_messages if start_time <= m.get('time', 0) <= end_time]
        if not filtered:
            logger.info("[BRANCH] _fetch_and_filter filtered 为空")
            if all_messages:
                logger.info(f"[BRANCH] _fetch_and_filter 消息时间跨度: {datetime.fromtimestamp(all_messages[0].get('time', 0))} ~ {datetime.fromtimestamp(all_messages[-1].get('time', 0))}")
                logger.info("[BRANCH] _fetch_and_filter 目标区间内无匹配消息")
            return "", [], 0, []
        else:
            logger.info(f"[BRANCH] _fetch_and_filter filtered 数量: {len(filtered)}")

        filtered.sort(key=lambda x: x.get('time', 0))
        # 打印完整消息列表（保留）
        logger.info("=" * 60)
        logger.info("【完整消息列表】开始打印所有拉取到的消息")
        for idx, msg in enumerate(filtered):
            logger.info(f"--- 消息 {idx+1}/{len(filtered)} ---")
            try:
                msg_str = json.dumps(msg, ensure_ascii=False, indent=2, default=str)
            except Exception:
                msg_str = str(msg)
            logger.info(msg_str)
        logger.info("【完整消息列表】打印完毕")
        logger.info("=" * 60)

        # 构建消息缓存（保留观察）
        msg_cache = {}
        for m in all_messages:
            msg_id = m.get('message_id')
            if msg_id is not None:
                try:
                    msg_cache[int(msg_id)] = m
                except (ValueError, TypeError):
                    pass
            msg_seq = m.get('message_seq')
            if msg_seq is not None:
                try:
                    msg_cache[int(msg_seq)] = m
                except (ValueError, TypeError):
                    pass
        logger.info(f"[BRANCH] _fetch_and_filter 缓存构建完成，条目数: {len(msg_cache)}")
        if msg_cache:
            sample_keys = list(msg_cache.keys())[:5]
            sample_msg = msg_cache[sample_keys[0]]
            logger.info(f"[BRANCH] _fetch_and_filter 示例键: {sample_keys}")
            logger.info(f"[BRANCH] _fetch_and_filter 示例消息字段: {list(sample_msg.keys())}")
        else:
            logger.warning("[BRANCH] _fetch_and_filter 缓存为空")

        # 解析每条消息
        msg_texts = []
        all_images = []
        image_meta = []      # 与 all_images 一一对应：{'n','url','sender','ts','ctx','local','cap'}
        url_index = {}       # url -> 编号，用于同一张图复用编号
        image_counter = [1]

        for idx, msg in enumerate(filtered):
            logger.info(f"[BRANCH] _fetch_and_filter 解析消息 #{idx+1}")
            sender_info = msg.get('sender', {})
            sender = sender_info.get('card') or sender_info.get('nickname') or str(sender_info.get('user_id', '未知'))
            ts = self._fmt_ts(msg.get('time', 0))
            msg_content = msg.get('message', [])
            text = ""
            images = []
            forward_texts = []

            for seg_idx, seg in enumerate(msg_content):
                # if isinstance(seg, dict):
                seg_type = seg.get('type')
                data = seg.get('data', {})
                # else:
                    # seg_type = getattr(seg, 'type', '')
                    # data = getattr(seg, 'data', {})
                seg_type_str = str(seg_type) if seg_type is not None else ''
                logger.info(f"[BRANCH] _fetch_and_filter 消息#{idx+1} 段#{seg_idx+1} type='{seg_type_str}'")

                if seg_type_str == 'text':
                    logger.info("[BRANCH] _fetch_and_filter -> text")
                    text += data.get('text', '')
                elif seg_type_str == 'image':
                    logger.info("[BRANCH] _fetch_and_filter -> image")
                    url = self._extract_image_urls([seg])
                    if url:
                        images.extend(url)
                elif seg_type_str == 'forward':
                    logger.info("[BRANCH] _fetch_and_filter -> forward")
                    forward_id = None
                    # if isinstance(seg, dict):
                    data = seg.get('data', {})
                    forward_id = data.get('id')
                    # else:
                        # if hasattr(seg, 'data'):
                            # data = seg.data if hasattr(seg, 'data') else {}
                            # if hasattr(data, 'id'):
                                # forward_id = data.id
                            # elif isinstance(data, dict):
                                # forward_id = data.get('id')
                    if forward_id:
                        logger.info(f"[BRANCH] _fetch_and_filter forward_id={forward_id}, 调用 _extract_forward_msg")
                        expanded = await self._extract_forward_msg(client, forward_id, image_counter=image_counter,
                                                                   image_urls=all_images, image_meta=image_meta,
                                                                   url_index=url_index)
                        forward_texts.extend(expanded)
                    else:
                        logger.info("[BRANCH] _fetch_and_filter forward 无 id，跳过")
                elif seg_type_str == 'reply':
                    logger.info("[BRANCH] _fetch_and_filter -> reply")
                    data = seg.get('data', {}) if isinstance(seg, dict) else getattr(seg, 'data', {})
                    reply_seq = data.get('seq')
                    reply_id = data.get('id')
                    reply_key = None
                    if reply_seq is not None:
                        try:
                            reply_key = int(reply_seq)
                            logger.info(f"[BRANCH] _fetch_and_filter 使用 seq={reply_seq} 作为 key")
                        except:
                            pass
                    if reply_key is None and reply_id is not None:
                        try:
                            reply_key = int(reply_id)
                            logger.info(f"[BRANCH] _fetch_and_filter 使用 id={reply_id} 作为 key")
                        except:
                            pass
                    if reply_key is not None:
                        matched_msg = None
                        # 尝试从 seq_cache（当前层）查找（但在 _fetch_and_filter 里没有 seq_cache，所以直接查 msg_cache）
                        if reply_key in msg_cache:
                            matched_msg = msg_cache[reply_key]
                            logger.info(f"[BRANCH] _fetch_and_filter 在 msg_cache 中命中 key={reply_key}")
                        if matched_msg:
                            sender_info_reply = matched_msg.get('sender', {})
                            sender_reply = sender_info_reply.get('card') or sender_info_reply.get('nickname') or str(sender_info_reply.get('user_id', '未知'))
                            content_reply = matched_msg.get('message', [])
                            texts = []
                            for seg_inner in content_reply:
                                if isinstance(seg_inner, dict):
                                    seg_type_inner = seg_inner.get('type')
                                    data_inner = seg_inner.get('data', {})
                                else:
                                    seg_type_inner = getattr(seg_inner, 'type', '')
                                    data_inner = getattr(seg_inner, 'data', {})
                                if seg_type_inner == 'text':
                                    texts.append(data_inner.get('text', ''))
                                elif seg_type_inner == 'image':
                                    texts.append('[图片]')
                            text_content = ''.join(texts).strip() or '[空消息]'
                            reply_text = f"回复 @{sender_reply}: {text_content}"
                        else:
                            logger.info("[BRANCH] _fetch_and_filter reply 缓存未命中，调用 API 降级")
                            fallback_id = reply_id or reply_seq
                            reply_text = await self._get_reply_text(client, fallback_id)
                        text += reply_text
                    else:
                        logger.info("[BRANCH] _fetch_and_filter reply 无有效 key")
                        text += "[回复消息ID无效]"
                else:
                    logger.info(f"[BRANCH] _fetch_and_filter 其他类型: {seg_type_str}")

            # 处理合并转发文本
            if forward_texts:
                forward_summary = " [合并转发] " + " | ".join(forward_texts)
                if text:
                    text += forward_summary
                else:
                    text = forward_summary

            msg_parts = []
            if text:
                msg_parts.append(text)
            for img_url in images:
                n = self._register_image(img_url, image_counter, all_images, image_meta,
                                         url_index, sender=sender, ts=ts,
                                         ctx=text[:100] or '(无文字)')
                if n > 0:
                    msg_parts.append(f"[图#{n}]")

            if msg_parts:
                msg_texts.append(f"[{ts}] {sender}: {' '.join(msg_parts)}")
                logger.info(f"[BRANCH] _fetch_and_filter 消息#{idx+1} 组装成功: {msg_parts[0][:30]}...")
            else:
                msg_texts.append(f"[{ts}] {sender}: [空消息]")
                logger.info(f"[BRANCH] _fetch_and_filter 消息#{idx+1} 为空消息")

        if not msg_texts:
            logger.info("[BRANCH] _fetch_and_filter msg_texts 为空，返回空")
            return "", [], 0, []

        full_text = "\n".join(msg_texts)
        if len(full_text) > self.max_text_chars:
            full_text = full_text[-self.max_text_chars:]
            full_text = "（消息过多已截断）\n" + full_text
            logger.info("[BRANCH] _fetch_and_filter 截断消息")

        logger.info("[BRANCH] _fetch_and_filter 返回成功")
        return full_text, all_images, len(msg_texts), image_meta

    # ---------- 图片本地化：先把图下载到本地，避免临时直链失效/跳图导致编号错位 ----------
    @staticmethod
    def _ext_of_bytes(data: bytes) -> str:
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
    async def _prepare_messages(self, event: AstrMessageEvent, start_time: Optional[int] = None, end_time: Optional[int] = None):
        logger.info("[BRANCH] _prepare_messages 进入")
        if not event.message_obj.group_id:
            logger.info("[BRANCH] _prepare_messages 非群聊，抛出异常")
            raise ValueError("此指令只能在群聊中使用。")
        group_id = event.message_obj.group_id
        logger.info(f"[BRANCH] _prepare_messages group_id={group_id}")

        platform = self.context.get_platform('aiocqhttp')
        # if not platform:
            # logger.info("[BRANCH] _prepare_messages 未找到 aiocqhttp 平台，抛出异常")
            # raise ValueError("未找到 QQ 平台适配器。")
        # client = None
        # if hasattr(platform, 'get_client'):
        client = platform.get_client()
            # logger.info("[BRANCH] _prepare_messages 通过 get_client 获取 client")
        # elif hasattr(platform, 'client'):
            # client = platform.client
            # logger.info("[BRANCH] _prepare_messages 通过 platform.client 获取 client")
        # elif hasattr(event, 'bot'):
            # client = event.bot
            # logger.info("[BRANCH] _prepare_messages 通过 event.bot 获取 client")
        # if not client:
            # logger.info("[BRANCH] _prepare_messages client 获取失败，抛出异常")
            # raise ValueError("无法获取 QQ 协议端 API 客户端。")

        reply_seg = None
        for seg in event.message_obj.message:
            if isinstance(seg, Reply):
                reply_seg = seg
                logger.info("[BRANCH] _prepare_messages 检测到 Reply 引用段")
                break
        if reply_seg:
            try:
                logger.info(f"[BRANCH] _prepare_messages 尝试获取引用消息详情, id={reply_seg.id}")
                msg_resp = await client.api.call_action('get_msg', message_id=int(reply_seg.id))
                if msg_resp and 'time' in msg_resp:
                    start_time = msg_resp['time']
                    logger.info(f"[BRANCH] _prepare_messages 引用消息时间戳={start_time}")
                else:
                    logger.info("[BRANCH] _prepare_messages 引用消息响应无 time 字段")
            except Exception as e:
                logger.warning(f"[BRANCH] _prepare_messages 获取引用消息详情失败: {e}")
        else:
            logger.info("[BRANCH] _prepare_messages 无引用消息")

        self_id = event.message_obj.self_id
        logger.info(f"[BRANCH] _prepare_messages 获取 self_id={self_id}")
        return await self._fetch_and_filter(client, group_id, start_time, end_time, self_id)

    # ---------- 灵活的时间解析 ----------
    def _parse_time_str(self, time_str: str) -> Dict[str, Optional[int]]:
        logger.info(f"[BRANCH] _parse_time_str 进入, time_str='{time_str}'")
        if not time_str:
            logger.info("[BRANCH] _parse_time_str 空字符串，返回全 None")
            return {'year': None, 'month': None, 'day': None, 'hour': None, 'minute': None}
        s = time_str.replace('_', ' ').replace('T', ' ')
        logger.info(f"[BRANCH] _parse_time_str 替换后: '{s}'")
        try:
            if ' ' in s:
                date_part, time_part = s.split(' ', 1)
                logger.info(f"[BRANCH] _parse_time_str 日期部分='{date_part}', 时间部分='{time_part}'")
                if '-' in date_part:
                    parts = date_part.split('-')
                    if len(parts) == 3:
                        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                        logger.info(f"[BRANCH] _parse_time_str 完整日期: year={year}, month={month}, day={day}")
                    elif len(parts) == 2:
                        month, day = int(parts[0]), int(parts[1])
                        year = None
                        logger.info(f"[BRANCH] _parse_time_str 月日: month={month}, day={day}")
                    else:
                        raise ValueError
                else:
                    raise ValueError
                if ':' in time_part:
                    hour, minute = map(int, time_part.split(':'))
                    logger.info(f"[BRANCH] _parse_time_str 时间: hour={hour}, minute={minute}")
                else:
                    hour = minute = None
                return {'year': year, 'month': month, 'day': day, 'hour': hour, 'minute': minute}
            else:
                if '-' in s:
                    parts = s.split('-')
                    if len(parts) == 3:
                        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                        logger.info(f"[BRANCH] _parse_time_str 仅日期 (完整): year={year}, month={month}, day={day}")
                        return {'year': year, 'month': month, 'day': day, 'hour': None, 'minute': None}
                    elif len(parts) == 2:
                        month, day = int(parts[0]), int(parts[1])
                        logger.info(f"[BRANCH] _parse_time_str 仅日期 (月日): month={month}, day={day}")
                        return {'year': None, 'month': month, 'day': day, 'hour': None, 'minute': None}
                    else:
                        raise ValueError
                elif ':' in s:
                    hour, minute = map(int, s.split(':'))
                    logger.info(f"[BRANCH] _parse_time_str 仅时间: hour={hour}, minute={minute}")
                    return {'year': None, 'month': None, 'day': None, 'hour': hour, 'minute': minute}
                else:
                    logger.info("[BRANCH] _parse_time_str 无法匹配格式，抛出异常")
                    raise ValueError
        except Exception as e:
            logger.info(f"[BRANCH] _parse_time_str 解析异常: {e}，返回全 None")
            return {'year': None, 'month': None, 'day': None, 'hour': None, 'minute': None}

    def _tzinfo(self):
        """返回配置项 timezone 对应的 tzinfo。

        服务器系统时区通常是 UTC，而用户输入的「开始/结束」时间默认是本地作息时间
        （如 Asia/Shanghai），若不指定时区会整整错位 8 小时、导致消息取不全。
        支持 IANA 时区名（Asia/Shanghai）与固定偏移（+8 / UTC+8 / -5）。
        解析失败时返回 None（即沿用服务器本地时区，与旧行为一致）。
        """
        name = (self.config.get("timezone", "") or "").strip()
        if not name:
            name = "Asia/Shanghai"
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception as e:
            logger.info(f"[TZ] 无法用时区名 {name!r}（{e}），尝试按固定偏移解析")
        m = re.search(r'([+-]?\d{1,2})(?::?(\d{2}))?$', name.replace('UTC', '').strip())
        if m:
            try:
                from datetime import timedelta, timezone as _tz
                hours = int(m.group(1))
                minutes = int(m.group(2)) if m.group(2) else 0
                if hours < 0:
                    minutes = -minutes
                return _tz(timedelta(hours=hours, minutes=minutes))
            except Exception as e:
                logger.warning(f"[TZ] 固定偏移解析失败 {name!r}: {e}")
        logger.warning(f"[TZ] 时区配置 {name!r} 无法解析，回退服务器本地时区")
        return None

    def _fmt_ts(self, ts: int) -> str:
        """按配置时区把时间戳格式化为可读字符串（日志与提示都用它，避免时区歧义）。"""
        tz = self._tzinfo()
        try:
            return datetime.fromtimestamp(int(ts), tz).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            return str(ts)

    def _normalize_times(self, start_dict: dict, end_dict: dict) -> Tuple[int, int]:
        logger.info("[BRANCH] _normalize_times 进入")
        tz = self._tzinfo()
        today = datetime.now(tz).date() if tz is not None else date.today()
        current_year = today.year
        current_month = today.month
        current_day = today.day
        logger.info(f"[BRANCH] _normalize_times 当前日期: {today} tz={tz}")

        # 补全年份
        if start_dict['year'] is None and end_dict['year'] is None:
            start_year = end_year = current_year
            logger.info("[BRANCH] _normalize_times 两者年份均 None，使用当前年份")
        elif start_dict['year'] is not None and end_dict['year'] is None:
            start_year = end_year = start_dict['year']
            logger.info(f"[BRANCH] _normalize_times start 有年份，end 无，使用 start 年份 {start_year}")
        elif start_dict['year'] is None and end_dict['year'] is not None:
            start_year = end_year = end_dict['year']
            logger.info(f"[BRANCH] _normalize_times end 有年份，start 无，使用 end 年份 {end_year}")
        else:
            start_year = start_dict['year']
            end_year = end_dict['year']
            logger.info(f"[BRANCH] _normalize_times 两者均有年份: start={start_year}, end={end_year}")

        # 补全月日
        if start_dict['month'] is None and start_dict['day'] is None:
            if end_dict['month'] is not None and end_dict['day'] is not None:
                start_month, start_day = end_dict['month'], end_dict['day']
                logger.info(f"[BRANCH] _normalize_times start 无月日，使用 end 月日: {start_month}-{start_day}")
            else:
                start_month, start_day = current_month, current_day
                logger.info(f"[BRANCH] _normalize_times start 无月日，end 也无，使用当前月日: {start_month}-{start_day}")
        else:
            start_month, start_day = start_dict['month'], start_dict['day']
            logger.info(f"[BRANCH] _normalize_times start 月日: {start_month}-{start_day}")

        if end_dict['month'] is None and end_dict['day'] is None:
            if start_dict['month'] is not None and start_dict['day'] is not None:
                end_month, end_day = start_dict['month'], start_dict['day']
                logger.info(f"[BRANCH] _normalize_times end 无月日，使用 start 月日: {end_month}-{end_day}")
            else:
                end_month, end_day = current_month, current_day
                logger.info(f"[BRANCH] _normalize_times end 无月日，start 也无，使用当前月日: {end_month}-{end_day}")
        else:
            end_month, end_day = end_dict['month'], end_dict['day']
            logger.info(f"[BRANCH] _normalize_times end 月日: {end_month}-{end_day}")

        # 补全时分
        start_hour = start_dict['hour'] if start_dict['hour'] is not None else 0
        start_minute = start_dict['minute'] if start_dict['minute'] is not None else 0
        end_hour = end_dict['hour'] if end_dict['hour'] is not None else 0
        end_minute = end_dict['minute'] if end_dict['minute'] is not None else 0
        logger.info(f"[BRANCH] _normalize_times start 时间: {start_hour}:{start_minute}, end 时间: {end_hour}:{end_minute}")

        # 结束时间若只给了日期（没给时分），默认按当天 23:59:59 处理，
        # 否则「到今天为止」会被截到当天 00:00，丢掉一整天的消息。
        end_has_time = end_dict['hour'] is not None
        try:
            start_dt = datetime(start_year, start_month, start_day, start_hour, start_minute,
                                tzinfo=tz)
            if end_has_time:
                end_dt = datetime(end_year, end_month, end_day, end_hour, end_minute, tzinfo=tz)
            else:
                end_dt = datetime(end_year, end_month, end_day, 23, 59, 59, tzinfo=tz)
            logger.info(f"[BRANCH] _normalize_times 构造 datetime: start={start_dt}, end={end_dt}")
        except ValueError as e:
            logger.info(f"[BRANCH] _normalize_times ValueError: {e}")
            raise ValueError(f"日期时间无效: {e}")

        return int(start_dt.timestamp()), int(end_dt.timestamp())

    # ---------- 参数解析 ----------
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
    async def _execute_summary(self, event: AstrMessageEvent,
                               parsed_override: Optional[dict] = None,
                               attached: bool = True) -> Optional[tuple]:
        """attached=True：把图片本体附给 LLM（费 token，但模型能看见图）。
        attached=False：只给「编号 + 上下文」清单，不附图（省 token，用于「回顾上传」）。"""
        logger.info("[BRANCH] _execute_summary 进入")
        if parsed_override is not None:
            # 调用方已自行解析参数（如「回顾上传」需要额外摘出页面名），直接复用
            parsed = parsed_override
            logger.info(f"[BRANCH] _execute_summary 使用外部 parsed = {parsed}")
        else:
            raw = event.message_str.strip()
            parts = raw.split()
            if not parts:
                logger.info("[BRANCH] _execute_summary 空指令，返回 None")
                await event.send(event.plain_result("无效指令。"))
                return None
            args = parts[1:]
            try:
                parsed = self._parse_args(args)
                logger.info(f"[BRANCH] _execute_summary parsed = {parsed}")
            except ValueError as e:
                logger.info(f"[BRANCH] _execute_summary 参数解析异常: {e}")
                await event.send(event.plain_result(f"参数错误：{str(e)}"))
                return None

        mode = parsed.get('mode')
        keyword = parsed.get('keyword')
        if not keyword:
            logger.info("[BRANCH] _execute_summary keyword 为空，返回 None")
            await event.send(event.plain_result("关键词不能为空。"))
            return None

        if mode == 'quote':
            has_reply = any(isinstance(seg, Reply) for seg in event.message_obj.message)
            if not has_reply:
                logger.info("[BRANCH] _execute_summary 引用模式下无引用，返回 None")
                await event.send(event.plain_result("引用模式下必须引用一条群消息。"))
                return None
            else:
                logger.info("[BRANCH] _execute_summary 引用模式有效")

        start_time = parsed.get('start') if mode == 'time' else None
        end_time = parsed.get('end') if mode == 'time' else None

        try:
            logger.info("[BRANCH] _execute_summary 调用 _prepare_messages")
            full_text, all_images, msg_count, image_meta = await self._prepare_messages(event, start_time, end_time)
            logger.info(f"[BRANCH] _execute_summary _prepare_messages 返回: msg_count={msg_count}, 图片数={len(all_images)}")
        except Exception as e:
            logger.info(f"[BRANCH] _execute_summary _prepare_messages 异常: {e}")
            await event.send(event.plain_result(f"准备消息失败：{str(e)}"))
            return None

        if msg_count == 0:
            logger.info("[BRANCH] _execute_summary msg_count=0，无消息")
            logger.info("=" * 50)
            logger.info(f"【回顾/调试】关键词：{keyword}，消息数：0")
            if parsed.get('mode') == 'time':
                start_dt = datetime.fromtimestamp(parsed['start']).strftime('%Y-%m-%d %H:%M:%S')
                end_dt = datetime.fromtimestamp(parsed['end']).strftime('%Y-%m-%d %H:%M:%S')
                logger.info(f"时间范围：{start_dt} 至 {end_dt}")
            logger.info("=" * 50)
            await event.send(event.plain_result("在指定时间范围内没有找到任何消息。"))
            return None

        # 图片先落地本地：QQ 直链有 rkey 时效，交给 LLM 前必须自己下载好，
        # 否则任何一张抓取失败都会被静默跳过、造成编号整体错位（贴错图）。
        try:
            await self._materialize_images(image_meta)
        except Exception as e:
            logger.warning(f"[BRANCH] _execute_summary 图片本地化异常: {e}")

        system_prompt = """请将以下议题对应的群聊记录整理为一份详细的 WikiText 归档。请严格遵守以下要求：

== 语法要求 ==

必须使用纯 WikiText 语法。

* 议题标题：使用 == 标题 ==
* 板块标题：使用 === 标题 ===
* 分支标题：使用 ==== 标题 ====
* 列表项：使用 *
* 粗体：使用 '''粗体'''
* 上下标：数字和加减号的上下标使用 Unicode 字符，如 ₂、¹、⁻²等。其他上下标使用 HTML 标签 <sub>...</sub> 和 <sup>...</sup>。
* '''仅限'''矩阵、多行对齐公式，使用 <math>TEX</math> 标签。'''注意'''其他任何情况都不要用！
* 不要使用任何 Markdown 语法，例如 #、##、**、>、- 等。

聊天记录中的人名用'''粗体'''。

== 格式要求 ==
无需呈现时间戳。由于读者都是化学专业的，不要有关于常识的冗余注释。

== 内容组织要求 ==

绝对忠实于原始记录的归档。讨论过程和逻辑链同样重要，必须完整复现，不能只给结果。剔除闲聊和无关内容，不允许随意添加内容，只使用记录中出现的对话、数据和结论。最后得到一份逻辑连贯、忠实于原始讨论脉络、干净整洁的Wiki归档，让读者能清晰看到每个议题如何从疑问一步步走向结论。严格遵循以下四板块结构：

=== 问题 ===

用一到两句话概括该议题的起点，即讨论者最初提出的具体问题。

=== 讨论 ===

将原始对话中的逻辑链条串联成连贯的段落，不要逐句罗列。按照讨论的自然顺序组织：从初步猜测到核心论证，再到质疑、澄清，最后达成共识或明确分歧。如果同一议题中存在多个平行分支，需要进行区分。

=== 结论 ===

总结经过讨论达成的共识或最终解释。如果议题未达成共识，需明确写出“未达成统一结论”。

=== 待解 ===

列出讨论过程中明确提及、但未得到完整解答的具体问题。该板块只能使用原始记录中实际出现的内容，绝对不能添加任何脑补信息。如果议题已完全解决，则该板块取消。

== 图片引用要求 ==

聊天记录中的图片用 “[图#N]” 标记（N 为图片编号，同一个编号自始至终指代同一张图）。

* 编号只能'''原样照抄'''：需要插图时，把记录里出现的那个 [图#N] 一字不改地搬过来（保留方括号与井号）。'''严禁'''凭印象重新编号、'''严禁'''按“第几张图”去推算编号、'''严禁'''把两个编号互换或合并。编号错了就等于贴错图。
* 只能引用记录里真实出现过、且列在文末图片清单中的编号。清单外的编号一律不许写。
* 插入位置：在与之相关的那一段叙述'''之后'''单独成行，不要塞进句子中间。
* 选取原则：只引用与议题内容直接相关的图。表情包、纯闲聊灌水的图不要用。
* 不要用“见上图”“如下图所示”之类含糊表述代替 [图#N] 标记。

== 语言风格要求 ==

使用连贯、精炼的语言，确保逻辑清晰。"""
        manifest = self._build_image_manifest(image_meta, attached=attached)
        user_prompt = (f"关键词：{keyword}\n\n最近消息：\n{full_text}\n"
                       f"请总结与“{keyword}”相关的讨论。{manifest}")

        # 日志输出
        logger.info("=" * 50)
        logger.info(f"【回顾/调试】关键词：{keyword}，消息数：{msg_count}，图片数：{len(all_images)}")
        if parsed.get('mode') == 'time':
            start_dt = datetime.fromtimestamp(parsed['start']).strftime('%Y-%m-%d %H:%M:%S')
            end_dt = datetime.fromtimestamp(parsed['end']).strftime('%Y-%m-%d %H:%M:%S')
            logger.info(f"时间范围：{start_dt} 至 {end_dt}")
        logger.info(f"User Prompt:\n{user_prompt}")
        logger.info(f"消息内容：\n{full_text}")
        if image_meta:
            logger.info("图片清单（编号 -> 来源 / 本地文件）：\n" + "\n".join(
                f"图#{m['n']} <- {m.get('ts','')} {m.get('sender','')} | {m.get('local')}"
                for m in image_meta))
        logger.info("=" * 50)

        return full_text, all_images, msg_count, keyword, system_prompt, user_prompt, image_meta
    @filter.command("测试合并转发")
    async def test_forward(self, event: AstrMessageEvent):
        """测试合并转发功能：发送插件自身的源代码"""
        logger.info("[BRANCH] test_forward 进入")
        
        # 检查是否在群聊中（可选）
        if not event.message_obj.group_id:
            await event.send(event.plain_result("此命令只能在群聊中使用。"))
            return
        
        # 读取当前文件（main.py）的内容
        import os
        file_path = os.path.abspath(__file__)
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            logger.error(f"[BRANCH] test_forward 读取文件失败: {e}")
            await event.send(event.plain_result(f"读取文件失败: {e}"))
            return
        
        # 发送合并转发消息
        await self._send_as_forward(event, content)
        logger.info("[BRANCH] test_forward 完成")

    # ---------- 指令：测试wiki上传 ----------
    @filter.command("测试wiki上传")
    async def test_wiki_upload(self, event: AstrMessageEvent):
        """测试：把本插件的 main.py 写入 Miraheze 页面（账号密码取自插件配置项）"""
        logger.info("[WIKI] test_wiki_upload 进入")

        import os
        file_path = os.path.abspath(__file__)
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            logger.error(f"[WIKI] 读取 main.py 失败: {e}")
            await event.send(event.plain_result(f"读取 main.py 失败: {e}"))
            return

        # 从插件配置项读取账号密码（不在代码中硬编码，避免提交到 GitHub 泄露）
        wiki_username = (self.config.get("wiki_username", "") or "").strip()
        wiki_password = self.config.get("wiki_password", "") or ""
        wiki_api_url = (self.config.get("wiki_api_url", "") or "").strip()
        wiki_target_page = (self.config.get("wiki_target_page", "") or "").strip()
        if not wiki_username or not wiki_password:
            await event.send(event.plain_result(
                "尚未配置 wiki 账号或密码。请在 AstrBot 管理面板 → 插件配置中填写 "
                "wiki_username 与 wiki_password（密码项已遮罩）。"))
            return
        if not wiki_api_url:
            wiki_api_url = 'https://lumorganix.miraheze.org/w/api.php'
        if not wiki_target_page:
            wiki_target_page = 'User:LumorganixBot/src'

        try:
            msg = await asyncio.to_thread(
                self._wiki_write_main_py, content,
                wiki_api_url, wiki_username, wiki_password, wiki_target_page)
        except Exception as e:
            logger.error(f"[WIKI] 上传异常: {e}", exc_info=True)
            await event.send(event.plain_result(f"wiki 上传失败：{e}"))
            return

        await event.send(event.plain_result(msg))
        logger.info("[WIKI] test_wiki_upload 完成")

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
    @filter.command("测试wiki打包上传")
    async def test_wiki_pack_upload(self, event: AstrMessageEvent):
        """引用一条合并转发消息，发送「/测试wiki打包上传 <页面名称>」，
        把转发里的文字与图片上传到 Miraheze 的指定页面（只考虑文字/图片，
        忽略嵌套合并转发与引用）。"""
        logger.info("[WIKI-PACK] test_wiki_pack_upload 进入")

        page_name = self._wiki_pack_parse_page_name(event)
        logger.info(f"[WIKI-PACK] 解析页面名={page_name!r}")
        if not page_name:
            await event.send(event.plain_result(
                "用法：先「引用/回复」一条合并转发消息，再发送\n"
                "/测试wiki打包上传 <页面名称>"))
            return

        client = self._get_cq_client(event)
        if not client:
            await event.send(event.plain_result("未找到 OneBot 客户端，无法读取合并转发。"))
            return

        forward_id = await self._wiki_pack_get_forward_id(event, client)
        logger.info(f"[WIKI-PACK] 提取 forward_id={forward_id!r} reply_seg存在={any(isinstance(s, Reply) for s in event.message_obj.message)}")
        if not forward_id:
            await event.send(event.plain_result(
                "未找到被引用的合并转发消息。请「引用/回复」一条合并转发消息后再试。"))
            return

        try:
            nodes = await self._collect_forward_nodes(client, forward_id)
        except Exception as e:
            logger.error(f"[WIKI-PACK] 提取合并转发失败: {e}", exc_info=True)
            await event.send(event.plain_result(f"读取合并转发失败：{e}"))
            return

        if not nodes:
            await event.send(event.plain_result("该合并转发没有可提取的内容。"))
            return

        total_img = sum(len(n['image_urls']) for n in nodes)
        logger.info(f"[WIKI-PACK] 节点数={len(nodes)} 图片数={total_img}")

        # 先把所有图片字节取回来（优先用 OneBot get_image，避免服务器直连 QQ CDN 超时）
        image_data = {}
        dl_jobs = []
        for n in nodes:
            for url, fid in zip(n['image_urls'], n['image_files']):
                if url not in image_data:
                    dl_jobs.append((url, fid))
        if dl_jobs:
            logger.info(f"[WIKI-PACK] 开始下载 {len(dl_jobs)} 张图片")
            logger.info(f"[WIKI-PACK] 待下载 file_id 列表: {[f[:30] for _, f in dl_jobs]}")
            await event.send(event.plain_result(f"正在下载 {len(dl_jobs)} 张图片…"))
            results = await asyncio.gather(
                *(self._wiki_pack_download_image(client, u, f) for u, f in dl_jobs),
                return_exceptions=True)
            for (url, _), res in zip(dl_jobs, results):
                if isinstance(res, Exception):
                    logger.warning(f"[WIKI-PACK] 图片下载异常 url={url[:60]}: {res}")
                elif res:
                    image_data[url] = res
            logger.info(f"[WIKI-PACK] 图片下载完成: 成功 {len(image_data)}/{len(dl_jobs)}")

        wiki_username = (self.config.get("wiki_username", "") or "").strip()
        wiki_password = self.config.get("wiki_password", "") or ""
        wiki_api_url = (self.config.get("wiki_api_url", "") or "").strip()
        if not wiki_username or not wiki_password:
            await event.send(event.plain_result(
                "尚未配置 wiki 账号或密码。请在 AStrBot 管理面板 → 插件配置中填写 "
                "wiki_username 与 wiki_password。"))
            return
        if not wiki_api_url:
            wiki_api_url = 'https://lumorganix.miraheze.org/w/api.php'

        await event.send(event.plain_result(
            f"开始打包上传：页面 {page_name}，{len(nodes)} 个节点、{total_img} 张图片…"))

        try:
            msg = await asyncio.to_thread(
                self._wiki_pack_upload, wiki_api_url, wiki_username,
                wiki_password, page_name, nodes, image_data)
        except Exception as e:
            logger.error(f"[WIKI-PACK] 上传异常: {e}", exc_info=True)
            await event.send(event.plain_result(f"wiki 打包上传失败：{e}"))
            return

        await event.send(event.plain_result(msg))
        logger.info("[WIKI-PACK] test_wiki_pack_upload 完成")

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
    def _read_if_exists(path: str):
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
    @filter.command("回顾")
    async def summarize_by_keyword(self, event: AstrMessageEvent):
        logger.info("[BRANCH] summarize_by_keyword 进入")
        result = await self._execute_summary(event)
        if result is None:
            logger.info("[BRANCH] summarize_by_keyword _execute_summary 返回 None，退出")
            return
        full_text, all_images, msg_count, keyword, system_prompt, user_prompt, image_meta = result
        # 用本地化后的图片（跳过失效的），顺序与附图清单一致
        img_refs = [m['local'] for m in image_meta if m.get('local')]

        try:
            summary = await self._run_summary_llm(
                event, system_prompt, user_prompt, img_refs, tag="BRANCH/summarize")
            await self._send_as_forward(event, summary or "LLM 未返回有效总结。")
        except Exception as e:
            logger.error(f"[BRANCH] summarize_by_keyword 异常: {e}", exc_info=True)
            await event.send(event.plain_result(f"调用 LLM 失败：{str(e)}"))

# ---------- 指令：回顾上传 ----------
    @filter.command("回顾上传")
    async def review_and_upload(self, event: AstrMessageEvent):
        """把「回顾」生成的归档正文写入 wiki 页面，并把正文引用的图片一并上传到 wiki。

        用法：
          /回顾上传 <主题> <页面名称> <开始时间> <结束时间>
          /回顾上传 <主题> <页面名称>            （配合引用一条群消息）
        页面名称中的空格请用下划线 _ 代替。
        """
        logger.info("[REVIEW-UP] review_and_upload 进入")

        page_name = ""
        parsed = None
        try:
            page_name, parsed = self._review_up_parse_args(event)
        except ValueError as e:
            logger.info(f"[REVIEW-UP] 参数解析失败: {e}")
            await event.send(event.plain_result(
                f"参数错误：{e}\n\n"
                "用法：\n"
                "  /回顾上传 <主题> <页面名称> <开始时间> <结束时间>\n"
                "  /回顾上传 <主题> <页面名称>   （需引用一条群消息）\n"
                "页面名称中的空格请用下划线 _ 代替。"))
            return
        logger.info(f"[REVIEW-UP] 页面名={page_name!r} parsed={parsed}")

        wiki_api_url = (self.config.get("wiki_api_url", "") or "").strip() or \
            'https://lumorganix.miraheze.org/w/api.php'
        wiki_username = (self.config.get("wiki_username", "") or "").strip()
        wiki_password = self.config.get("wiki_password", "") or ""
        if not wiki_username or not wiki_password:
            await event.send(event.plain_result(
                "尚未配置 wiki 账号或密码。请在 AstrBot 管理面板 → 插件配置中填写 "
                "wiki_username 与 wiki_password。"))
            return

        # attached=True：图片本体随正文生成一起发给 LLM（本地 file:// 路径，顺序稳定）。
        # 只调用一次 LLM（不再逐张问图注），token 开销主要在「一次性的图片输入」上。
        result = await self._execute_summary(event, parsed_override=parsed, attached=True)
        if result is None:
            logger.info("[REVIEW-UP] _execute_summary 返回 None，退出")
            return
        full_text, all_images, msg_count, keyword, system_prompt, user_prompt, image_meta = result

        await event.send(event.plain_result(
            f"已取到 {msg_count} 条消息、{len(all_images)} 张图片，正在生成归档并上传到 "
            f"{page_name} ……"))

        # 全部可用图（按编号升序），与文末清单顺序严格一致
        img_refs = [m['local'] for m in image_meta if m.get('local')]
        logger.info(f"[REVIEW-UP] 本次附带 {len(img_refs)} 张图给 LLM")

        try:
            summary = await self._run_summary_llm(
                event, system_prompt, user_prompt, img_refs,
                tag="REVIEW-UP/llm", stream=False)
        except Exception as e:
            logger.error(f"[REVIEW-UP] LLM 调用失败: {e}", exc_info=True)
            await event.send(event.plain_result(f"调用 LLM 失败：{str(e)}"))
            return

        if not summary.strip():
            await event.send(event.plain_result("LLM 未返回有效内容，已取消上传。"))
            return

        refs = re.findall(r'\[图#(\d+)\]', summary)
        logger.info(f"[REVIEW-UP] 正文长度={len(summary)}，引用图片编号={refs}")
        # 兜底：正文引用了清单外（或已失效）的编号时，直接剔除
        allowed = {m['n'] for m in image_meta if m.get('local')}
        dropped = sorted({int(r) for r in refs} - allowed)
        if dropped:
            logger.warning(f"[REVIEW-UP] 正文引用了清单外编号 {dropped}，已剔除对应标记")
            for d in dropped:
                summary = re.sub(r'\[图#%d\][ \t]*\n?([ \t]*图注[:：].*)?\n?' % d, '', summary)

        try:
            msg, final_text, mapping = await asyncio.to_thread(
                self._review_up_publish, summary, list(all_images),
                wiki_api_url, wiki_username, wiki_password, page_name,
                list(image_meta))
        except Exception as e:
            logger.error(f"[REVIEW-UP] wiki 上传异常: {e}", exc_info=True)
            await event.send(event.plain_result(f"wiki 上传失败：{e}"))
            return

        # 原文照例发一份给群里，便于人工核对（上传版已将 [图#N] 换成 File: 引用）
        await event.send(event.plain_result(msg))
        # 「编号 -> 原图来源 -> wiki 文件」对照表只写日志，不再发群（避免刷屏）
        try:
            table = [f"图#{n} <- {info['src']} -> {info['file']}"
                     for n, info in sorted(mapping.items())]
            if table:
                logger.info("[REVIEW-UP] 图片对照表（图#N <- 来源 -> wiki 文件）：\n"
                            + "\n".join(table))
        except Exception as e:
            logger.warning(f"[REVIEW-UP] 记录对照表失败: {e}")
        try:
            await self._send_as_forward(event, summary)
        except Exception as e:
            logger.warning(f"[REVIEW-UP] 转发原文失败（不影响已上传结果）: {e}")
        logger.info("[REVIEW-UP] review_and_upload 完成")

    # ---------- 回顾上传：参数解析 ----------
    def _review_up_parse_args(self, event: AstrMessageEvent) -> tuple:
        """返回 (page_name, parsed)。识别规则：
        - 末尾连续两个「像时间」的 token → 时间模式，其余前两个依次为 主题、页面名
        - 没有时间 token → 引用模式，两个 token 依次为 主题、页面名
        """
        raw = event.message_str.strip()
        parts = raw.split()
        tokens = parts[1:]
        if len(tokens) < 2:
            raise ValueError("至少需要「主题」和「页面名称」两个参数")

        time_idx = [i for i, t in enumerate(tokens) if self._looks_like_time(t)]
        if len(time_idx) == 2 and time_idx[1] == time_idx[0] + 1:
            start_str, end_str = tokens[time_idx[0]], tokens[time_idx[1]]
            rest = [t for i, t in enumerate(tokens) if i not in time_idx]
            if len(rest) < 2:
                raise ValueError("缺少主题或页面名称")
            keyword, page_name = rest[0], rest[1]
            if len(rest) > 2:
                raise ValueError(f"多余的参数：{' '.join(rest[2:])}")
            start_dict = self._parse_time_str(start_str)
            end_dict = self._parse_time_str(end_str)
            if all(v is None for v in start_dict.values()) or all(v is None for v in end_dict.values()):
                raise ValueError("时间格式无法解析，请使用 YYYY-MM-DD、YYYY-MM-DD_HH:MM、MM-DD 或 HH:MM")
            try:
                start_ts, end_ts = self._normalize_times(start_dict, end_dict)
            except Exception as e:
                raise ValueError(f"时间补全失败: {e}")
            if start_ts >= end_ts:
                raise ValueError("开始时间必须早于结束时间")
            parsed = {'mode': 'time', 'start': start_ts, 'end': end_ts, 'keyword': keyword}
        elif time_idx:
            raise ValueError("时间参数必须成对出现（开始时间 结束时间）")
        else:
            if len(tokens) > 2:
                raise ValueError(f"多余的参数：{' '.join(tokens[2:])}")
            keyword, page_name = tokens[0], tokens[1]
            parsed = {'mode': 'quote', 'keyword': keyword}

        page_name = page_name.strip()
        if not page_name:
            raise ValueError("页面名称不能为空")
        return page_name, parsed

    def _looks_like_time(self, token: str) -> bool:
        """判断 token 是否像时间（复用「回顾」的时间解析器，不能解析即返回全 None）。"""
        try:
            d = self._parse_time_str(token)
        except Exception:
            return False
        return bool(d) and not all(v is None for v in d.values())

    # ---------- 回顾上传：上传图片并写页面（在线程中同步执行） ----------
    def _review_up_publish(self, wikitext: str, all_images: List[str],
                           api_url: str, username: str, password: str,
                           page_title: str, image_meta: list = None) -> tuple:
        """把正文里的 [图#N] 替换成 wiki 文件引用并写入页面。

        返回 (结果说明, 最终页面文本, 编号->{'file','src'} 对照表)。
        图片优先使用已落到本地的字节（image_meta[n]['local']），
        不再依赖可能失效的 QQ 临时直链。整个过程在同一个登录会话里完成。
        """
        from curl_cffi.requests import Session

        sess = Session(impersonate='chrome', timeout=180)
        sess.headers['User-Agent'] = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                                      'Chrome/120.0.0.0 Safari/537.36')

        j = sess.get(api_url, params={'action': 'query', 'meta': 'tokens',
                                      'type': 'login', 'format': 'json'}).json()
        lt = j['query']['tokens']['logintoken']
        j = sess.post(api_url, data={'action': 'login', 'lgname': username,
                                     'lgpassword': password, 'lgtoken': lt,
                                     'format': 'json'}).json()
        if j.get('login', {}).get('result') != 'Success':
            raise RuntimeError(f"登录失败: {json.dumps(j, ensure_ascii=False)}")
        j = sess.get(api_url, params={'action': 'query', 'meta': 'tokens',
                                      'type': 'csrf', 'format': 'json'}).json()
        csrf = j['query']['tokens']['csrftoken']
        logger.info("[REVIEW-UP] wiki 登录成功")

        # 收集正文引用的图片编号（去重、保序）
        wanted = []
        for num in re.findall(r'\[图#(\d+)\]', wikitext):
            n = int(num)
            if n not in wanted:
                wanted.append(n)
        logger.info(f"[REVIEW-UP] 需要上传的图片编号: {wanted}")

        # 上限保护：超出部分直接从正文里把标记去掉，避免页面上留一排「图片上传失败」
        cap = getattr(self, 'max_upload_images', 12)
        if len(wanted) > cap:
            over = wanted[cap:]
            logger.warning(f"[REVIEW-UP] 引用图片 {len(wanted)} 张超过上限 {cap}，丢弃编号 {over}")
            for n in over:
                wikitext = re.sub(r'\[图#%d\][ \t]*\n?([ \t]*图注[:：].*)?\n?' % n, '', wikitext)
            wanted = wanted[:cap]

        file_map = {}
        failed = []
        meta_by_n = {m['n']: m for m in (image_meta or [])}
        # 文件名带页面名+时间戳，避免不同归档的图互相覆盖
        safe = re.sub(r'[^0-9A-Za-z\u4e00-\u9fff_-]', '_', page_title.split(':')[-1])[:40] or 'page'
        stamp = datetime.now().strftime('%Y%m%d%H%M%S')
        for pos, n in enumerate(wanted, start=1):
            name_base = f"ReviewImg_{safe}_{stamp}_{pos}"
            if n < 1 or n > len(all_images):
                logger.warning(f"[REVIEW-UP] 图#{n} 超出图片列表范围（共 {len(all_images)} 张），跳过")
                failed.append(n)
                continue
            url = all_images[n - 1]
            meta = meta_by_n.get(n) or {}
            # 优先用本地已下载好的字节（url 传空串 -> 跳过直传，直接用 data 上传）
            data = b''
            local = (meta.get('local') or '').replace('file://', '')
            if local and os.path.exists(local):
                try:
                    with open(local, 'rb') as f:
                        data = f.read()
                    logger.info(f"[REVIEW-UP] 图#{n} 使用本地文件 {local}（{len(data)}B）")
                except Exception as e:
                    logger.warning(f"[REVIEW-UP] 图#{n} 读取本地文件失败: {e}")
            try:
                fname = self._wiki_upload_one_image(sess, api_url, '' if data else url,
                                                    data, pos, csrf, name_base=name_base)
                file_map[n] = fname
                logger.info(f"[REVIEW-UP] 图#{n} 上传成功 -> {fname}")
            except Exception as e:
                logger.warning(f"[REVIEW-UP] 图#{n} 上传失败: {e}，尝试重新下载后重传")
                try:
                    if not data:
                        r = sess.get(url, timeout=120)
                        if r.status_code != 200 or not r.content:
                            raise RuntimeError(f"下载 HTTP {r.status_code}")
                        data = r.content
                    fname = self._wiki_upload_one_image(sess, api_url, '', data, pos, csrf,
                                                        name_base=name_base)
                    file_map[n] = fname
                    logger.info(f"[REVIEW-UP] 图#{n} 重传成功 -> {fname}")
                except Exception as e2:
                    logger.error(f"[REVIEW-UP] 图#{n} 最终失败: {e2}")
                    failed.append(n)

        # 图注优先取模型写在 [图#N] 下一行的「图注：…」，其次用系统生成的图注
        def _clean_cap(s: str) -> str:
            s = (s or '').strip().replace('|', '／').replace(']]', '］］').replace('\n', ' ')
            return s[:120]

        cap_in_text = {}
        for m in re.finditer(r'\[图#(\d+)\][ \t]*\n[ \t]*图注[:：][ \t]*(.*)', wikitext):
            cap_in_text[int(m.group(1))] = _clean_cap(m.group(2))

        def _sub(m):
            n = int(m.group(1))
            fn = file_map.get(n)
            if not fn:
                return "[图片上传失败]"
            cap = cap_in_text.get(n) or _clean_cap((meta_by_n.get(n) or {}).get('cap', ''))
            return f"[[File:{fn}|thumb|center|{cap}]]" if cap else f"[[File:{fn}|thumb|center]]"

        # 先吃掉 [图#N] + 下一行图注，再补回 [[File:...|图注]]，避免页面上留两行
        final_text = re.sub(r'\[图#(\d+)\][ \t]*\n[ \t]*图注[:：][ \t]*.*', r'[图#\1]', wikitext)
        final_text = re.sub(r'\[图#(\d+)\]', _sub, final_text)

        j = sess.post(api_url, data={
            'action': 'edit', 'title': page_title, 'text': final_text,
            'summary': f'review upload: 群聊回顾归档（{len(wanted)} 张图）',
            'token': csrf, 'assert': 'user', 'format': 'json',
        }).json()
        if j.get('edit', {}).get('result') != 'Success':
            raise RuntimeError(f"编辑失败: {json.dumps(j, ensure_ascii=False)}")

        newrev = j['edit'].get('newrevid')
        info = f"已写入 {page_title}（{len(final_text)} 字节，新版本 {newrev}）。"
        info += f" 引用图片 {len(wanted)} 张，成功 {len(file_map)} 张"
        if failed:
            info += f"，失败编号：{','.join(str(x) for x in failed)}"

        mapping = {}
        for n, fn in file_map.items():
            m = (meta_by_n.get(n) or {})
            src = f"{m.get('ts','')} {m.get('sender','')}".strip() or f"图#{n}"
            mapping[n] = {'file': fn, 'src': src}
        return info, final_text, mapping

    # ---------- 指令：回debug顾 ----------
    @filter.command("回debug顾")
    async def debug_summarize(self, event: AstrMessageEvent):
        logger.info("[BRANCH] debug_summarize 进入")
        result = await self._execute_summary(event)
        if result is None:
            logger.info("[BRANCH] debug_summarize _execute_summary 返回 None，退出")
        else:
            logger.info("[BRANCH] debug_summarize 完成，所有信息已在日志中")

    async def terminate(self):
        if hasattr(self, 'cov'):
            self.cov.stop()
            self.cov.save()
            self.cov.html_report(directory='cov_html')
            logger.info("[BRANCH] Coverage report saved to cov_html/")
        logger.info("插件 astrbot_plugin_summary 已卸载")
# wiki 上传指令（配置项驱动，凭据见 _conf_schema.json / 面板）
