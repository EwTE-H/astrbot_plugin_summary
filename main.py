import time
from datetime import datetime, date
from typing import List, Optional, Tuple, Dict

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.message_components import Reply

class PluginSummary(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.default_days = 2
        self.max_messages = 1000
        self.max_text_chars = 50000
        self.max_images = 20
    async def _send_as_forward(self, event: AstrMessageEvent, text: str, title: str = ""):
        """将长文本以合并转发形式发送，超出单节点上限则自动拆分"""
        if not text:
            return

        # 拼接标题和正文
        full_content = f"{title}\n\n{text}" if title else text
        max_len = 4500  # 单节点文本上限

        # 按固定长度拆分
        chunks = [full_content[i:i+max_len] for i in range(0, len(full_content), max_len)]

        # 构造合并转发节点
        nodes = []
        bot_name = "Bot"   # 可改成你的机器人名字
        bot_uin = "10086"  # 可改成你的机器人QQ号

        for chunk in chunks:
            nodes.append({
                "type": "node",
                "data": {
                    "name": bot_name,
                    "uin": bot_uin,
                    "content": [{"type": "text", "data": {"text": chunk}}]
                }
            })

        # 获取 client 用于 API 调用
        platform = self.context.get_platform('aiocqhttp')
        if not platform:
            # 降级为普通发送
            for chunk in chunks:
                await event.send(event.plain_result(chunk))
            return

        client = None
        if hasattr(platform, 'get_client'):
            client = platform.get_client()
        elif hasattr(platform, 'client'):
            client = platform.client
        elif hasattr(event, 'bot'):
            client = event.bot

        if not client:
            # 降级
            for chunk in chunks:
                await event.send(event.plain_result(chunk))
            return

        group_id = event.message_obj.group_id
        if not group_id:
            await event.send(event.plain_result(text))
            return

        # 发送合并转发
        try:
            await client.api.call_action(
                'send_group_forward_msg',
                **{
                    'group_id': int(group_id),
                    'messages': nodes
                }
            )
        except Exception as e:
            logger.error(f"发送合并转发失败: {e}")
            # 降级为逐条发送
            for chunk in chunks:
                await event.send(event.plain_result(chunk))
    # ---------- 消息解析（递归展开合并转发） ----------
    async def _parse_messages(self, messages: list, client, depth: int, image_counter: list, image_urls: list) -> List[str]:
        result = []
        for msg in messages:
            # 在 _parse_messages 方法中，找到获取 sender 的地方（大约在第 43 行）
            # 原代码：
            # sender = msg.get('sender', {}).get('nickname', msg.get('sender', {}).get('user_id', '未知'))

            # 修改为：
            sender_info = msg.get('sender', {})
            sender = sender_info.get('card') or sender_info.get('nickname') or str(sender_info.get('user_id', '未知'))
            content = msg.get('message', msg.get('content', []))
            text_parts = []
            nested_parts = []
            for seg in content:
                seg_type = seg.type if hasattr(seg, 'type') else seg.get('type')
                seg_type_str = seg_type.value if hasattr(seg_type, 'value') else str(seg_type)
                if seg_type_str == 'text':
                    data = seg.data if hasattr(seg, 'data') else seg.get('data', {})
                    text_parts.append(data.get('text', ''))
                elif seg_type_str == 'image':
                    data = seg.data if hasattr(seg, 'data') else seg.get('data', {})
                    url = data.get('url', '')
                    if url:
                        img_num = image_counter[0]
                        image_counter[0] += 1
                        text_parts.append(f"[图#{img_num}]")
                        if image_urls is not None:
                            image_urls.append(url)
                elif seg_type_str == 'forward':
                    data = seg.data if hasattr(seg, 'data') else seg.get('data', {})
                    if 'content' in data and data['content']:
                        nested = await self._parse_messages(data['content'], client, depth + 1, image_counter, image_urls)
                        if nested:
                            nested_str = " | ".join(nested)
                            nested_parts.append(f"( {nested_str} )")
                    elif 'id' in data:
                        nested_id = data['id']
                        nested = await self._extract_forward_msg(client, nested_id, depth + 1, image_counter, image_urls)
                        if nested:
                            nested_str = " | ".join(nested)
                            nested_parts.append(f"( {nested_str} )")
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

    async def _extract_forward_msg(self, client, forward_id: str, depth: int = 0, image_counter: list = None, image_urls: list = None) -> List[str]:
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
            return await self._parse_messages(messages, client, depth + 1, image_counter, image_urls)
        except Exception as e:
            logger.error(f"展开合并转发失败: {e}")
            return ["[合并转发展开失败]"]

    # ---------- 拉取消息并过滤 ----------
    async def _fetch_and_filter(self, client, group_id: int, start_time: Optional[int] = None, end_time: Optional[int] = None, self_id: Optional[int] = None):
        now = time.time()
        if start_time is None:
            start_time = now - self.default_days * 86400
        if end_time is None:
            end_time = now

        all_messages = []
        next_seq = None
        max_loops = 20
        per_page = 1000

        logger.info(f"[分页拉取] 目标区间: {datetime.fromtimestamp(start_time)} ~ {datetime.fromtimestamp(end_time)}")

        while max_loops > 0:
            params = {'group_id': int(group_id), 'count': per_page}
            if next_seq is not None:
                params['message_seq'] = next_seq
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
            all_messages.extend(msgs)

            oldest_in_batch = msgs[0]
            oldest_time = oldest_in_batch.get('time', 0)

            if oldest_time <= start_time:
                logger.info(f"[分页拉取] 最旧消息 {datetime.fromtimestamp(oldest_time)} <= 起始时间，停止")
                break

            if 'message_seq' in oldest_in_batch:
                next_seq = oldest_in_batch['message_seq']
            else:
                next_seq = oldest_in_batch.get('time')
                if next_seq is None:
                    break

            max_loops -= 1

        logger.info(f"[分页拉取] 共拉取 {len(all_messages)} 条原始消息")

        # 整体按时间升序排序
        all_messages.sort(key=lambda x: x.get('time', 0))

        #  过滤掉机器人自己的消息 
        if self_id is not None:
            all_messages = [m for m in all_messages if m.get('sender', {}).get('user_id') != self_id]
            logger.info(f"[分页拉取] 过滤后剩余 {len(all_messages)} 条消息（已排除机器人自身）")

        # 按时间范围过滤
        filtered = [m for m in all_messages if start_time <= m.get('time', 0) <= end_time]
        if not filtered:
            if all_messages:
                logger.info(f"[分页拉取] 消息时间跨度: {datetime.fromtimestamp(all_messages[0].get('time', 0))} ~ {datetime.fromtimestamp(all_messages[-1].get('time', 0))}")
                logger.info(f"[分页拉取] 目标区间内无匹配消息")
            return "", [], 0

        # 后续解析消息（保持不变）
        filtered.sort(key=lambda x: x.get('time', 0))
        msg_texts = []
        all_images = []
        image_counter = [1]

        for msg in filtered:
            # 在 _fetch_and_filter 方法中，找到获取 sender 的地方（大约在第 146 行）
            # 原代码：
            # sender = msg.get('sender', {}).get('nickname', msg.get('sender', {}).get('user_id', '未知'))

            # 修改为：
            sender_info = msg.get('sender', {})
            # 优先使用群名片(card)，如果没有则使用昵称(nickname)，最后才用 QQ号
            sender = sender_info.get('card') or sender_info.get('nickname') or str(sender_info.get('user_id', '未知'))
            ts = datetime.fromtimestamp(msg.get('time', 0)).strftime('%Y-%m-%d %H:%M:%S')
            msg_content = msg.get('message', [])
            text = ""
            images = []
            forward_texts = []

            for seg in msg_content:
                seg_type = seg.type if hasattr(seg, 'type') else seg.get('type')
                seg_type_str = seg_type.value if hasattr(seg_type, 'value') else str(seg_type)

                if seg_type_str == 'text':
                    data = seg.data if hasattr(seg, 'data') else seg.get('data', {})
                    text += data.get('text', '')
                elif seg_type_str == 'image':
                    url = self._extract_image_urls([seg])
                    if url:
                        images.extend(url)
                elif seg_type_str == 'forward':
                    forward_id = None
                    if hasattr(seg, 'data'):
                        data = seg.data if hasattr(seg, 'data') else seg.get('data', {})
                        if hasattr(data, 'id'):
                            forward_id = data.id
                        elif isinstance(data, dict) and 'id' in data:
                            forward_id = data['id']
                    elif isinstance(seg, dict):
                        data = seg.get('data', {})
                        forward_id = data.get('id')
                    if forward_id:
                        expanded = await self._extract_forward_msg(client, forward_id, image_counter=image_counter, image_urls=all_images)
                        forward_texts.extend(expanded)

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
                    break
                all_images.append(img_url)
                msg_parts.append(f"[图#{image_counter[0]}]")
                image_counter[0] += 1

            if msg_parts:
                msg_texts.append(f"[{ts}] {sender}: {' '.join(msg_parts)}")
            else:
                msg_texts.append(f"[{ts}] {sender}: [空消息]")

        if not msg_texts:
            return "", [], 0

        full_text = "\n".join(msg_texts)
        if len(full_text) > self.max_text_chars:
            full_text = full_text[-self.max_text_chars:]
            full_text = "（消息过多已截断）\n" + full_text

        return full_text, all_images, len(msg_texts)

    # ---------- 统一准备消息（含引用检测） ----------
    async def _prepare_messages(self, event: AstrMessageEvent, start_time: Optional[int] = None, end_time: Optional[int] = None):
        if not event.message_obj.group_id:
            raise ValueError("此指令只能在群聊中使用。")
        group_id = event.message_obj.group_id

        platform = self.context.get_platform('aiocqhttp')
        if not platform:
            raise ValueError("未找到 QQ 平台适配器。")
        client = None
        if hasattr(platform, 'get_client'):
            client = platform.get_client()
        elif hasattr(platform, 'client'):
            client = platform.client
        elif hasattr(event, 'bot'):
            client = event.bot
        if not client:
            raise ValueError("无法获取 QQ 协议端 API 客户端。")

        # 检测引用消息（覆盖 start_time）
        reply_seg = None
        for seg in event.message_obj.message:
            if isinstance(seg, Reply):
                reply_seg = seg
                break
        if reply_seg:
            try:
                msg_resp = await client.api.call_action('get_msg', message_id=int(reply_seg.id))
                if msg_resp and 'time' in msg_resp:
                    start_time = msg_resp['time']
                    logger.info(f"检测到引用消息，时间戳: {start_time}，将从此时间开始回溯。")
            except Exception as e:
                logger.warning(f"获取引用消息详情失败: {e}")

        #  获取机器人自己的ID并传给 _fetch_and_filter 
        self_id = event.message_obj.self_id
        return await self._fetch_and_filter(client, group_id, start_time, end_time, self_id)

    # ---------- 灵活的时间解析 ----------
    def _parse_time_str(self, time_str: str) -> Dict[str, Optional[int]]:
        """
        解析时间字符串，返回 {'year': int|None, 'month': int|None, 'day': int|None, 'hour': int|None, 'minute': int|None}
        支持格式：
          - YYYY-MM-DD
          - YYYY-MM-DD HH:MM   (空格已替换为下划线或T)
          - YYYY-MM-DD_HH:MM
          - YYYY-MM-DDTHH:MM
          - MM-DD
          - HH:MM
          - 空字符串 -> 全部为None
        """
        if not time_str:
            return {'year': None, 'month': None, 'day': None, 'hour': None, 'minute': None}
        # 统一将下划线或T替换为空格
        s = time_str.replace('_', ' ').replace('T', ' ')
        # 尝试完整日期时间
        try:
            # 如果有时间部分
            if ' ' in s:
                date_part, time_part = s.split(' ', 1)
                # 解析日期
                if '-' in date_part:
                    parts = date_part.split('-')
                    if len(parts) == 3:
                        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                    elif len(parts) == 2:
                        # 只有月日，年用None
                        month, day = int(parts[0]), int(parts[1])
                        year = None
                    else:
                        raise ValueError
                else:
                    raise ValueError
                # 解析时间
                if ':' in time_part:
                    hour, minute = map(int, time_part.split(':'))
                else:
                    hour = minute = None
                return {'year': year, 'month': month, 'day': day, 'hour': hour, 'minute': minute}
            else:
                # 只有日期或时间
                if '-' in s:
                    parts = s.split('-')
                    if len(parts) == 3:
                        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                        return {'year': year, 'month': month, 'day': day, 'hour': None, 'minute': None}
                    elif len(parts) == 2:
                        month, day = int(parts[0]), int(parts[1])
                        return {'year': None, 'month': month, 'day': day, 'hour': None, 'minute': None}
                    else:
                        raise ValueError
                elif ':' in s:
                    hour, minute = map(int, s.split(':'))
                    return {'year': None, 'month': None, 'day': None, 'hour': hour, 'minute': minute}
                else:
                    # 可能只有年份？不处理
                    raise ValueError
        except Exception:
            # 解析失败，返回全None，后续会报错
            return {'year': None, 'month': None, 'day': None, 'hour': None, 'minute': None}

    def _normalize_times(self, start_dict: dict, end_dict: dict) -> Tuple[int, int]:
        """
        根据规则补全两个时间字典，返回两个Unix时间戳（秒）
        """
        today = date.today()
        current_year = today.year
        current_month = today.month
        current_day = today.day

        # 补全年份
        if start_dict['year'] is None and end_dict['year'] is None:
            start_year = end_year = current_year
        elif start_dict['year'] is not None and end_dict['year'] is None:
            start_year = end_year = start_dict['year']
        elif start_dict['year'] is None and end_dict['year'] is not None:
            start_year = end_year = end_dict['year']
        else:
            start_year = start_dict['year']
            end_year = end_dict['year']

        # 补全月日
        if start_dict['month'] is None and start_dict['day'] is None:
            # start无月日
            if end_dict['month'] is not None and end_dict['day'] is not None:
                start_month, start_day = end_dict['month'], end_dict['day']
            else:
                start_month, start_day = current_month, current_day
        else:
            start_month, start_day = start_dict['month'], start_dict['day']

        if end_dict['month'] is None and end_dict['day'] is None:
            if start_dict['month'] is not None and start_dict['day'] is not None:
                end_month, end_day = start_dict['month'], start_dict['day']
            else:
                end_month, end_day = current_month, current_day
        else:
            end_month, end_day = end_dict['month'], end_dict['day']

        # 补全时分
        start_hour = start_dict['hour'] if start_dict['hour'] is not None else 0
        start_minute = start_dict['minute'] if start_dict['minute'] is not None else 0
        end_hour = end_dict['hour'] if end_dict['hour'] is not None else 0
        end_minute = end_dict['minute'] if end_dict['minute'] is not None else 0

        # 构造datetime对象并转时间戳
        try:
            start_dt = datetime(start_year, start_month, start_day, start_hour, start_minute)
            end_dt = datetime(end_year, end_month, end_day, end_hour, end_minute)
        except ValueError as e:
            raise ValueError(f"日期时间无效: {e}")

        return int(start_dt.timestamp()), int(end_dt.timestamp())

    # ---------- 参数解析 ----------
    def _parse_args(self, args: List[str]) -> dict:
        """
        参数格式： /回顾 开始时间 结束时间 关键词  (时间模式)
                 /回顾 关键词              (引用模式)
        时间字符串支持下划线或T代替空格。
        """
        if len(args) == 3:
            start_str, end_str, keyword = args[0], args[1], args[2]
            start_dict = self._parse_time_str(start_str)
            end_dict = self._parse_time_str(end_str)
            # 检查是否解析成功（至少应有月日或时间部分）
            if all(v is None for v in start_dict.values()) or all(v is None for v in end_dict.values()):
                raise ValueError("时间格式无法解析，请使用 YYYY-MM-DD、YYYY-MM-DD_HH:MM、MM-DD 或 HH:MM")
            try:
                start_ts, end_ts = self._normalize_times(start_dict, end_dict)
            except Exception as e:
                raise ValueError(f"时间补全失败: {e}")
            if start_ts >= end_ts:
                raise ValueError("开始时间必须早于结束时间")
            return {'mode': 'time', 'start': start_ts, 'end': end_ts, 'keyword': keyword}
        elif len(args) == 1:
            return {'mode': 'quote', 'keyword': args[0]}
        else:
            raise ValueError(f"参数数量错误（需要 1 个或 3 个，实际 {len(args)} 个）")

    # ---------- 辅助提取 ----------
    def _extract_plain_text(self, chain: List) -> str:
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
        return ''.join(texts).strip()

    def _extract_image_urls(self, chain: List) -> List[str]:
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
        return urls

    # ---------- 公共核心方法（所有共同逻辑） ----------
    async def _execute_summary(self, event: AstrMessageEvent) -> Optional[tuple]:
        """所有共同逻辑：参数解析、引用检查、消息拉取、日志输出。失败时发送错误消息并返回None。"""
        logger.info("===== _execute_summary 被调用 =====")
        raw = event.message_str.strip()
        parts = raw.split()
        if not parts:
            await event.send(event.plain_result("无效指令。"))
            return None
        args = parts[1:]
        try:
            parsed = self._parse_args(args)
        except ValueError as e:
            await event.send(event.plain_result(f"参数错误：{str(e)}"))
            return None

        mode = parsed.get('mode')
        keyword = parsed.get('keyword')
        if not keyword:
            await event.send(event.plain_result("关键词不能为空。"))
            return None

        if mode == 'quote':
            has_reply = any(isinstance(seg, Reply) for seg in event.message_obj.message)
            if not has_reply:
                await event.send(event.plain_result("引用模式下必须引用一条群消息。"))
                return None

        start_time = parsed.get('start') if mode == 'time' else None
        end_time = parsed.get('end') if mode == 'time' else None

        await event.send(event.plain_result(f"正在检索与“{keyword}”相关的消息..."))

        try:
            full_text, all_images, msg_count = await self._prepare_messages(event, start_time, end_time)
        except Exception as e:
            await event.send(event.plain_result(f"准备消息失败：{str(e)}"))
            return None

        if msg_count == 0:
            # 日志输出无消息的情况，便于调试
            logger.info("=" * 50)
            logger.info(f"【回顾/调试】关键词：{keyword}，消息数：0")
            if parsed.get('mode') == 'time':
                start_dt = datetime.fromtimestamp(parsed['start']).strftime('%Y-%m-%d %H:%M:%S')
                end_dt = datetime.fromtimestamp(parsed['end']).strftime('%Y-%m-%d %H:%M:%S')
                logger.info(f"时间范围：{start_dt} 至 {end_dt}")
            logger.info("=" * 50)
            await event.send(event.plain_result("在指定时间范围内没有找到任何消息。"))
            return None

        # 构造提示词
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

        # 完整日志输出（两种模式完全一致）
        logger.info("=" * 50)
        logger.info(f"【回顾/调试】关键词：{keyword}，消息数：{msg_count}，图片数：{len(all_images)}")
        # 时间模式额外打印起止时间
        if parsed.get('mode') == 'time':
            start_dt = datetime.fromtimestamp(parsed['start']).strftime('%Y-%m-%d %H:%M:%S')
            end_dt = datetime.fromtimestamp(parsed['end']).strftime('%Y-%m-%d %H:%M:%S')
            logger.info(f"时间范围：{start_dt} 至 {end_dt}")
        #logger.info(f"System Prompt:\n{system_prompt}")
        logger.info(f"User Prompt:\n{user_prompt}")
        logger.info(f"消息内容：\n{full_text}")
        if all_images:
            logger.info("图片URL列表：\n" + "\n".join(all_images))
        logger.info("=" * 50)

        return full_text, all_images, msg_count, keyword, system_prompt, user_prompt

    @filter.command("回顾")
    async def summarize_by_keyword(self, event: AstrMessageEvent):
        result = await self._execute_summary(event)
        if result is None:
            return
        full_text, all_images, msg_count, keyword, system_prompt, user_prompt = result

        provider = await self.context.get_using_provider_async(umo=event.unified_msg_origin)
        if not provider:
            await event.send(event.plain_result("未找到可用的 LLM 提供商。"))
            return

        full_response = ""
        buffer = ""          # 累积缓冲区
        chunk_counter = 0
        log_threshold = 100  # 每收集200字符打印一次

        try:
            stream = provider.text_chat_stream(
                prompt=user_prompt,
                system_prompt=system_prompt,
                image_urls=all_images if all_images else None
            )

            logger.info("[STREAM] 开始接收流式响应")

            async for chunk in stream:
                if chunk.is_chunk:
                    chunk_text = chunk.completion_text
                    if chunk_text:
                        full_response += chunk_text
                        buffer += chunk_text
                        chunk_counter += 1
                        if len(buffer) >= log_threshold:
                            logger.info(f"[STREAM] 累计片段 #{chunk_counter}，新增内容: {buffer}")
                            buffer = ""

            # 流结束后打印剩余缓冲
            if buffer:
                logger.info(f"[STREAM] 流结束，剩余片段内容: {buffer}")
            logger.info(f"[STREAM] 流式响应结束，共 {chunk_counter} 个片段，总长度 {len(full_response)} 字符")

            if full_response.strip():
                await event.send(event.plain_result(f"关于“{keyword}”的群聊总结\n\n{full_response}"))
            else:
                logger.warning("流式响应未返回有效内容，尝试非流式重试")
                llm_resp = await provider.text_chat(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    image_urls=all_images if all_images else None
                )
                summary = llm_resp.completion_text or "LLM 未返回有效总结。"
                await self._send_as_forward(event, summary, title=f"关于“{keyword}”的群聊总结")
        except Exception as e:
            logger.error(f"LLM 调用异常: {e}", exc_info=True)
            await event.send(event.plain_result(f"调用 LLM 失败：{str(e)}"))

    # ---------- 指令：回debug顾 ----------
    @filter.command("回debug顾")
    async def debug_summarize(self, event: AstrMessageEvent):
        result = await self._execute_summary(event)
        if result is None:
            return
        # 不发送任何额外消息，所有信息已在日志中

    async def terminate(self):
        logger.info("插件 astrbot_plugin_summary 已卸载")