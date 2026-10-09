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

class MessageMixin:
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
                        sender: str = '', ts: str = '', ctx: str = '',
                        sub_type=None, filter_meme: bool = True) -> int:
        """登记一张图片并返回它的编号。

        所有产生 [图#N] 标记的地方（普通消息、合并转发递归）都必须走这里，
        保证「编号 N」永远等于 image_urls[N-1]，不会出现编号与图片错位。
        - 相同 URL 只登记一次（复用同一编号），避免同一张图被编成多个号；
        - 不设上限：所有图片都会登记并编号，避免纯图片消息因超量被误判成 [空消息]。
        - filter_meme=True 时，表情包（OneBot subType==1）直接跳过不登记，
          即「取消息时就过滤表情包」，使其不进入正文标记、不上传 wiki。
        """
        if not url:
            return -1
        if filter_meme and sub_type in (1, '1'):
            return -1
        if url_index is not None and url in url_index:
            return url_index[url]
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
                'subType': sub_type,
            })
        return n

    # ---------- 消息解析（递归展开合并转发） ----------

    async def _parse_messages(self, messages: list, client, depth: int, image_counter: list, image_urls: list,
                              image_meta: list = None, url_index: dict = None,
                              filter_meme: bool = True) -> List[str]:
        result = []
        for msg in messages:
            sender_info = msg.get('sender', {})
            sender = sender_info.get('card') or sender_info.get('nickname') or str(sender_info.get('user_id', '未知'))
            content = msg.get('message', msg.get('content', []))
            text_parts = []
            nested_parts = []

            for seg in content:
                seg_type = None
                data = {}
                if isinstance(seg, dict):
                    seg_type = seg.get('type')
                    data = seg.get('data', {})
                else:
                    if hasattr(seg, 'type'):
                        seg_type = seg.type
                        data = seg.data if hasattr(seg, 'data') else {}
                    else:
                        seg_type = getattr(seg, 'type', None)
                        data = getattr(seg, 'data', {})
                seg_type_str = str(seg_type) if seg_type is not None else ''

                if seg_type_str == 'text':
                    text_parts.append(data.get('text', ''))
                elif seg_type_str == 'image':
                    url = data.get('url', '')
                    if not url:
                        fv = data.get('file', '')
                        if isinstance(fv, str) and fv.startswith('http'):
                            url = fv
                    if url:
                        sub_type = data.get('subType')
                        if sub_type is None:
                            sub_type = data.get('sub_type')
                        img_num = self._register_image(
                            url, image_counter, image_urls, image_meta, url_index,
                            sender=sender, ts=self._fmt_ts(msg.get('time', 0)),
                            ctx=(' '.join(text_parts)[:100] or '(合并转发内)'),
                            sub_type=sub_type, filter_meme=filter_meme)
                        if img_num > 0:
                            text_parts.append(f"[图#{img_num}]")
                elif seg_type_str == 'forward':
                    if 'content' in data and data['content']:
                        nested = await self._parse_messages(data['content'], client, depth + 1,
                                                            image_counter, image_urls, image_meta, url_index,
                                                            filter_meme=filter_meme)
                        if nested:
                            nested_str = " | ".join(nested)
                            nested_parts.append(f"( {nested_str} )")
                    elif 'id' in data:
                        nested = await self._extract_forward_msg(client, data['id'], depth + 1, image_counter,
                                                                 image_urls, image_meta, url_index,
                                                                 filter_meme=filter_meme)
                        if nested:
                            nested_str = " | ".join(nested)
                            nested_parts.append(f"( {nested_str} )")
                elif seg_type_str == 'reply':
                    text_parts.append("[引用]")
                else:
                    pass

            has_self = bool(text_parts)
            self_str = ""
            if has_self:
                self_str = f"[{sender}] " + " ".join(text_parts)
            nested_combined = " ".join(nested_parts) if nested_parts else ""
            if has_self and nested_combined:
                result.append(f"{self_str} {nested_combined}")
            elif has_self:
                result.append(self_str)
            elif nested_combined:
                result.append(nested_combined)
        return result

    async def _extract_forward_msg(self, client, forward_id: str, depth: int = 0, image_counter: list = None,
                                   image_urls: list = None, image_meta: list = None, url_index: dict = None,
                                   filter_meme: bool = True) -> List[str]:
        if depth > 5:
            return ["[合并转发嵌套过深]"]
        try:
            resp = await client.api.call_action('get_forward_msg', id=str(forward_id))
            messages = []
            if isinstance(resp, dict):
                if 'messages' in resp:
                    messages = resp['messages']
                elif 'message' in resp:
                    messages = resp['message']
                elif 'data' in resp and isinstance(resp['data'], dict):
                    messages = resp['data'].get('messages', resp['data'].get('message', []))
            if not messages:
                return ["[合并转发内容为空]"]
            result = await self._parse_messages(messages, client, depth + 1, image_counter,
                                                image_urls, image_meta, url_index,
                                                filter_meme=filter_meme)
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

    async def _send_as_forward_with_images(self, event: AstrMessageEvent, text: str,
                                           image_map: Dict[int, str], title: str = ""):
        """发送合并转发，并把正文里的 [图#N] 渲染成真实图片节点（与 wiki 上 [[File:...|thumb]] 对应）。

        image_map: 编号(int) -> 图片 URL（来自 image_meta[n]['url']，即消息记录里的原始图片直链；
                  也兼容 file:// 本地路径）。
        有可用 URL 的图渲染成图片段；否则原样保留 [图#N] 文字标记。
        """
        logger.info("[BRANCH] _send_as_forward_with_images 进入")
        if not text:
            logger.info("[BRANCH] text 为空，直接返回")
            return

        # 1) 把正文切成 文本段 / 图片标记 序列，顺序与原文严格一致
        parts = []  # ('t', str) 或 ('i', int)
        pos = 0
        for mo in re.finditer(r'\[图#(\d+)\]', text):
            if mo.start() > pos:
                parts.append(('t', text[pos:mo.start()]))
            parts.append(('i', int(mo.group(1))))
            pos = mo.end()
        if pos < len(text):
            parts.append(('t', text[pos:]))

        # 2) 组装转发节点：单节点文本上限 3000 字，图片紧跟其前文
        nodes = []
        bot_user_id = 10086
        cur_content = []
        cur_len = 0
        max_len = 3000

        def flush():
            nonlocal cur_content, cur_len
            if not cur_content:
                return
            nodes.append({
                "type": "node",
                "data": {
                    "user_id": bot_user_id,
                    "nickname": "Bot",
                    "content": cur_content,
                }
            })
            cur_content = []
            cur_len = 0

        for kind, val in parts:
            if kind == 't':
                seg_text = val
                if cur_len + len(seg_text) > max_len and cur_content:
                    flush()
                cur_content.append({"type": "text", "data": {"text": seg_text}})
                cur_len += len(seg_text)
            else:  # 图片：直接用消息记录里的图片 URL（QQ 直链，协议端会自动下载）
                url = image_map.get(val)
                if url:
                    # http(s) 直链或 file:// 本地路径都交给协议端处理；本地化路径已带 file:// 前缀
                    cur_content.append({"type": "image", "data": {"file": url}})
                else:
                    logger.warning(f"[BRANCH] 图#{val} 无可用图片URL，降级为文字标记")
                    cur_content.append({"type": "text", "data": {"text": f"[图#{val}]"}})
        flush()

        if not nodes:
            logger.info("[BRANCH] 没有可发送节点，降级普通发送")
            await event.send(event.plain_result(text[:3000]))
            return

        logger.info(f"[BRANCH] _send_as_forward_with_images 组装 {len(nodes)} 个节点")

        # 3) 发送（平台/client 获取逻辑与 _send_as_forward 一致）
        platform = self.context.get_platform('aiocqhttp')
        if not platform:
            logger.info("[BRANCH] 未找到 aiocqhttp 平台，降级普通发送")
            await event.send(event.plain_result(text[:3000]))
            return
        client = None
        if hasattr(platform, 'get_client'):
            client = platform.get_client()
            logger.info("[BRANCH] 通过 get_client 获取 client")
        elif hasattr(platform, 'client'):
            client = platform.client
            logger.info("[BRANCH] 通过 platform.client 获取 client")
        elif hasattr(event, 'bot'):
            client = event.bot
            logger.info("[BRANCH] 通过 event.bot 获取 client")
        if not client:
            logger.info("[BRANCH] client 获取失败，降级普通发送")
            await event.send(event.plain_result(text[:3000]))
            return
        group_id = event.message_obj.group_id
        if not group_id:
            logger.info("[BRANCH] 无 group_id，降级普通发送")
            await event.send(event.plain_result(text[:3000]))
            return

        try:
            logger.info("[BRANCH] _send_as_forward_with_images 调用 send_group_forward_msg")
            await client.api.call_action(
                'send_group_forward_msg',
                **{'group_id': int(group_id), 'message': nodes}
            )
            logger.info("[BRANCH] _send_as_forward_with_images 发送成功")
        except Exception as e:
            logger.error(f"[BRANCH] _send_as_forward_with_images 发送失败: {e}")
            await event.send(event.plain_result(text[:3000]))

    # ---------- 拉取消息并过滤 ----------

    async def _fetch_and_filter(self, client, group_id: int, start_time: Optional[int] = None, end_time: Optional[int] = None, self_id: Optional[int] = None, collect_subtype: bool = False, max_messages: Optional[int] = None, filter_meme: bool = True):
        logger.info(f"[BRANCH] _fetch_and_filter 进入, group_id={group_id}, start_time={start_time}, end_time={end_time}, self_id={self_id}")
        now = time.time()
        if start_time is None:
            start_time = now - self.default_days * 86400
        if end_time is None:
            end_time = now

        all_messages = []
        seen_message_ids = set()   # 分页去重用
        next_anchor = None         # SnowLuma 用 message_id 做历史锚点
        max_loops = 20
        per_page = 200             # SnowLuma 单次上限约 200，写 1000 也会被截断


        while max_loops > 0:
            params = {'group_id': int(group_id), 'count': per_page}
            if next_anchor is not None:
                # SnowLuma 锚点参数名是 message_id（可能为负），不是 message_seq
                params['message_id'] = next_anchor
                params['reverse_order'] = True

            resp = await client.api.call_action('get_group_msg_history', **params)

            if isinstance(resp, dict) and 'messages' in resp:
                msgs = resp['messages']
            elif isinstance(resp, dict) and 'data' in resp and isinstance(resp['data'], dict):
                msgs = resp['data'].get('messages', [])
            else:
                break

            if not msgs:
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

            if max_messages is not None and len(all_messages) >= max_messages:
                break

            oldest_in_batch = msgs[0]
            oldest_time = oldest_in_batch.get('time', 0)
            if oldest_time <= start_time:
                break

            oldest_mid = oldest_in_batch.get('message_id')
            if oldest_mid is None:
                break

            # 若本批最旧消息 == 上一轮锚点，说明已经没有更旧的消息了
            if next_anchor is not None and oldest_mid == next_anchor:
                break

            next_anchor = oldest_mid

            max_loops -= 1

        logger.info(f"[BRANCH] _fetch_and_filter 共拉取 {len(all_messages)} 条原始消息")

        if self_id is not None:
            original_len = len(all_messages)
            all_messages = [m for m in all_messages if m.get('sender', {}).get('user_id') != self_id]
            logger.info(f"[BRANCH] _fetch_and_filter 过滤机器人自身，过滤前 {original_len}，过滤后 {len(all_messages)}")

        filtered = [m for m in all_messages if start_time <= m.get('time', 0) <= end_time]
        if not filtered:
            logger.info("[BRANCH] _fetch_and_filter filtered 为空")
            return "", [], 0, []
        else:
            logger.info(f"[BRANCH] _fetch_and_filter filtered 数量: {len(filtered)}")

        filtered.sort(key=lambda x: x.get('time', 0))
        # 打印完整消息列表（保留）

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
        if msg_cache:
            sample_keys = list(msg_cache.keys())[:5]
            sample_msg = msg_cache[sample_keys[0]]
        else:
            logger.warning("[BRANCH] _fetch_and_filter 缓存为空")

        # 解析每条消息
        msg_texts = []
        all_images = []
        image_meta = []      # 与 all_images 一一对应：{'n','url','sender','ts','ctx','local','cap'}
        url_index = {}       # url -> 编号，用于同一张图复用编号
        image_counter = [1]

        for idx, msg in enumerate(filtered):
            sender_info = msg.get('sender', {})
            sender = sender_info.get('card') or sender_info.get('nickname') or str(sender_info.get('user_id', '未知'))
            ts = self._fmt_ts(msg.get('time', 0))
            msg_content = msg.get('message', [])
            text = ""
            images = []
            seg_subtypes = {}   # url -> subType（仅 collect_subtype 时填充，供调试区分表情包）
            forward_texts = []

            for seg_idx, seg in enumerate(msg_content):
                # if isinstance(seg, dict):
                seg_type = seg.get('type')
                data = seg.get('data', {})
                # else:
                    # seg_type = getattr(seg, 'type', '')
                    # data = getattr(seg, 'data', {})
                seg_type_str = str(seg_type) if seg_type is not None else ''

                if seg_type_str == 'text':
                    text += data.get('text', '')
                elif seg_type_str == 'image':
                    url = self._extract_image_urls([seg])
                    if url:
                        for u in url:
                            images.append(u)
                            st = data.get('subType')
                            if st is None:
                                st = data.get('sub_type')
                            seg_subtypes[u] = st
                elif seg_type_str == 'forward':
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
                        expanded = await self._extract_forward_msg(client, forward_id, image_counter=image_counter,
                                                                   image_urls=all_images, image_meta=image_meta,
                                                                   url_index=url_index, filter_meme=filter_meme)
                        forward_texts.extend(expanded)
                elif seg_type_str == 'reply':
                    data = seg.get('data', {}) if isinstance(seg, dict) else getattr(seg, 'data', {})
                    reply_seq = data.get('seq')
                    reply_id = data.get('id')
                    reply_key = None
                    if reply_seq is not None:
                        try:
                            reply_key = int(reply_seq)
                        except:
                            pass
                    if reply_key is None and reply_id is not None:
                        try:
                            reply_key = int(reply_id)
                        except:
                            pass
                    if reply_key is not None:
                        matched_msg = None
                        # 尝试从 seq_cache（当前层）查找（但在 _fetch_and_filter 里没有 seq_cache，所以直接查 msg_cache）
                        if reply_key in msg_cache:
                            matched_msg = msg_cache[reply_key]
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
                            text_content = ''.join(texts).strip()
                            reply_text = f"回复 @{sender_reply}: {text_content}" if text_content else ""
                        else:
                            fallback_id = reply_id or reply_seq
                            reply_text = await self._get_reply_text(client, fallback_id)
                        text += reply_text
                    else:
                        text += "[回复消息ID无效]"
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
                st = seg_subtypes.get(img_url)
                n = self._register_image(img_url, image_counter, all_images, image_meta,
                                         url_index, sender=sender, ts=ts,
                                         ctx=text[:100] or '(无文字)',
                                         sub_type=st, filter_meme=filter_meme)
                if n > 0:
                    msg_parts.append(f"[图#{n}]")

            if msg_parts:
                msg_texts.append(f"[{ts}] {sender}: {' '.join(msg_parts)}")
            # 空消息（既无文字也无有效图片）不进入拼接字符串，无意义

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
