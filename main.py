import time
import json
from datetime import datetime, date
from typing import List, Optional, Tuple, Dict

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.message_components import Reply
import coverage
class PluginSummary(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.default_days = 2
        self.max_messages = 1000
        self.max_text_chars = 50000
        self.max_images = 20
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

    # ---------- 消息解析（递归展开合并转发） ----------
    async def _parse_messages(self, messages: list, client, depth: int, image_counter: list, image_urls: list) -> List[str]:
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
                    if url:
                        img_num = image_counter[0]
                        image_counter[0] += 1
                        text_parts.append(f"[图#{img_num}]")
                        if image_urls is not None:
                            image_urls.append(url)
                    else:
                        logger.info("[BRANCH] _parse_messages image 无 url")
                elif seg_type_str == 'forward':
                    logger.info("[BRANCH] _parse_messages -> 分支: forward")
                    if 'content' in data and data['content']:
                        logger.info("[BRANCH] _parse_messages forward 有 content 字段，递归解析")
                        nested = await self._parse_messages(data['content'], client, depth + 1, image_counter, image_urls)
                        if nested:
                            nested_str = " | ".join(nested)
                            nested_parts.append(f"( {nested_str} )")
                    elif 'id' in data:
                        logger.info(f"[BRANCH] _parse_messages forward 有 id 字段，调用 _extract_forward_msg, id={data['id']}")
                        nested = await self._extract_forward_msg(client, data['id'], depth + 1, image_counter, image_urls)
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
    async def _extract_forward_msg(self, client, forward_id: str, depth: int = 0, image_counter: list = None, image_urls: list = None) -> List[str]:
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
            result = await self._parse_messages(messages, client, depth + 1, image_counter, image_urls)
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
            return "", [], 0
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
        image_counter = [1]

        for idx, msg in enumerate(filtered):
            logger.info(f"[BRANCH] _fetch_and_filter 解析消息 #{idx+1}")
            sender_info = msg.get('sender', {})
            sender = sender_info.get('card') or sender_info.get('nickname') or str(sender_info.get('user_id', '未知'))
            ts = datetime.fromtimestamp(msg.get('time', 0)).strftime('%Y-%m-%d %H:%M:%S')
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
                        expanded = await self._extract_forward_msg(client, forward_id, image_counter=image_counter, image_urls=all_images)
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
                if len(all_images) >= self.max_images:
                    logger.info(f"[BRANCH] _fetch_and_filter 图片已达上限 {self.max_images}，停止添加")
                    break
                all_images.append(img_url)
                msg_parts.append(f"[图#{image_counter[0]}]")
                image_counter[0] += 1

            if msg_parts:
                msg_texts.append(f"[{ts}] {sender}: {' '.join(msg_parts)}")
                logger.info(f"[BRANCH] _fetch_and_filter 消息#{idx+1} 组装成功: {msg_parts[0][:30]}...")
            else:
                msg_texts.append(f"[{ts}] {sender}: [空消息]")
                logger.info(f"[BRANCH] _fetch_and_filter 消息#{idx+1} 为空消息")

        if not msg_texts:
            logger.info("[BRANCH] _fetch_and_filter msg_texts 为空，返回空")
            return "", [], 0

        full_text = "\n".join(msg_texts)
        if len(full_text) > self.max_text_chars:
            full_text = full_text[-self.max_text_chars:]
            full_text = "（消息过多已截断）\n" + full_text
            logger.info("[BRANCH] _fetch_and_filter 截断消息")

        logger.info("[BRANCH] _fetch_and_filter 返回成功")
        return full_text, all_images, len(msg_texts)

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

    def _normalize_times(self, start_dict: dict, end_dict: dict) -> Tuple[int, int]:
        logger.info("[BRANCH] _normalize_times 进入")
        today = date.today()
        current_year = today.year
        current_month = today.month
        current_day = today.day
        logger.info(f"[BRANCH] _normalize_times 当前日期: {today}")

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

        try:
            start_dt = datetime(start_year, start_month, start_day, start_hour, start_minute)
            end_dt = datetime(end_year, end_month, end_day, end_hour, end_minute)
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
    async def _execute_summary(self, event: AstrMessageEvent) -> Optional[tuple]:
        logger.info("[BRANCH] _execute_summary 进入")
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
            full_text, all_images, msg_count = await self._prepare_messages(event, start_time, end_time)
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

== 语言风格要求 ==

使用连贯、精炼的语言，确保逻辑清晰。"""
        user_prompt = f"关键词：{keyword}\n\n最近消息：\n{full_text}\n请总结与“{keyword}”相关的讨论。"

        # 日志输出
        logger.info("=" * 50)
        logger.info(f"【回顾/调试】关键词：{keyword}，消息数：{msg_count}，图片数：{len(all_images)}")
        if parsed.get('mode') == 'time':
            start_dt = datetime.fromtimestamp(parsed['start']).strftime('%Y-%m-%d %H:%M:%S')
            end_dt = datetime.fromtimestamp(parsed['end']).strftime('%Y-%m-%d %H:%M:%S')
            logger.info(f"时间范围：{start_dt} 至 {end_dt}")
        logger.info(f"User Prompt:\n{user_prompt}")
        logger.info(f"消息内容：\n{full_text}")
        if all_images:
            logger.info("图片URL列表：\n" + "\n".join(all_images))
        logger.info("=" * 50)

        return full_text, all_images, msg_count, keyword, system_prompt, user_prompt
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
    # ---------- 指令：回顾 ----------
    @filter.command("回顾")
    async def summarize_by_keyword(self, event: AstrMessageEvent):
        logger.info("[BRANCH] summarize_by_keyword 进入")
        result = await self._execute_summary(event)
        if result is None:
            logger.info("[BRANCH] summarize_by_keyword _execute_summary 返回 None，退出")
            return
        full_text, all_images, msg_count, keyword, system_prompt, user_prompt = result

        provider = await self.context.get_using_provider_async(umo=event.unified_msg_origin)
        # if not provider:
            # logger.info("[BRANCH] summarize_by_keyword 未找到 LLM 提供商，发送错误")
            # await event.send(event.plain_result("未找到可用的 LLM 提供商。"))
            # return
        # logger.info("[BRANCH] summarize_by_keyword 获取到 provider")

        full_response = ""
        buffer = ""
        chunk_counter = 0
        log_threshold = 100

        try:
            logger.info("[BRANCH] summarize_by_keyword 开始流式调用")
            stream = provider.text_chat_stream(
                prompt=user_prompt,
                system_prompt=system_prompt,
                image_urls=all_images if all_images else None
            )

            async for chunk in stream:
                if chunk.is_chunk:
                    chunk_text = chunk.completion_text
                    if chunk_text:
                        full_response += chunk_text
                        buffer += chunk_text
                        chunk_counter += 1
                        if len(buffer) >= log_threshold:
                            logger.info(f"[BRANCH] summarize_by_keyword 流式片段 #{chunk_counter}: {buffer}")
                            buffer = ""

            if buffer:
                logger.info(f"[BRANCH] summarize_by_keyword 剩余缓冲区: {buffer}")
            logger.info(f"[BRANCH] summarize_by_keyword 流式结束，总长度 {len(full_response)}")

            if full_response.strip():
                await self._send_as_forward(event, full_response)
            else:
                logger.warning("[BRANCH] summarize_by_keyword 流式响应为空，尝试非流式")
                llm_resp = await provider.text_chat(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    image_urls=all_images if all_images else None
                )
                summary = llm_resp.completion_text or "LLM 未返回有效总结。"
                await self._send_as_forward(event, summary)
        except Exception as e:
            logger.error(f"[BRANCH] summarize_by_keyword 异常: {e}", exc_info=True)
            await event.send(event.plain_result(f"调用 LLM 失败：{str(e)}"))

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