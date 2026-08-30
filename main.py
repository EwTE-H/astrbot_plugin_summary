import time
from datetime import datetime
from typing import List, Dict, Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.message_components import Reply, Plain, Image

class PluginSummary(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.default_days = 2
        self.max_messages = 1000
        self.max_text_chars = 50000
        self.max_images = 20
    async def _extract_forward_msg(self, client, forward_id: str, depth: int = 0) -> List[str]:
        if depth > 5:
            return ["[合并转发嵌套过深]"]
        try:
            resp = await client.api.call_action('get_forward_msg', id=forward_id)
            messages = resp.get('messages', []) if isinstance(resp, dict) else []
            if not messages:
                return ["[合并转发内容为空]"]
            result = []
            for msg in messages:
                sender = msg.get('sender', {}).get('nickname', '未知')
                content = msg.get('message', [])
                text = self._extract_plain_text(content)
                if text:
                    result.append(f"[{sender}] {text}")
                images = self._extract_image_urls(content)
                for img_url in images[:2]:
                    result.append(f"[{sender} 图片] {img_url}")
                # 递归嵌套
                for seg in content:
                    seg_type = seg.type if hasattr(seg, 'type') else seg.get('type')
                    seg_type_str = seg_type.value if hasattr(seg_type, 'value') else str(seg_type)
                    if seg_type_str in ('node', 'nodes'):
                        nested_id = seg.id if hasattr(seg, 'id') else seg.get('id')
                        if nested_id:
                            nested_result = await self._extract_forward_msg(client, nested_id, depth + 1)
                            result.extend(nested_result)
            return result if result else ["[合并转发内容为空]"]
        except Exception as e:
            logger.warning(f"展开合并转发失败: {e}")
            return ["[合并转发展开失败]"]
    async def _fetch_and_filter(self, client, group_id: int, keyword: str, truncate: bool = False, start_time: int = None):
        """拉取消息并过滤，返回 (full_text, all_images, msg_count)
        all_images 的顺序与文本中的 [图#N] 编号一一对应。
        """
        response = await client.api.call_action(
            'get_group_msg_history',
            group_id=int(group_id),
            count=self.max_messages
        )
        if isinstance(response, dict) and 'messages' in response:
            messages = response['messages']
        elif isinstance(response, dict) and 'data' in response and isinstance(response['data'], dict):
            messages = response['data'].get('messages', [])
        else:
            messages = []

        if not messages:
            return "", [], 0

        now = time.time()
        cutoff = start_time if start_time else now - self.default_days * 86400
        filtered = [m for m in messages if m.get('time', 0) >= cutoff]
        if not filtered:
            return "", [], 0

        filtered.sort(key=lambda x: x.get('time', 0))

        msg_texts = []
        all_images = []
        image_counter = 1  # 全局编号，保证所有图片连续编号

        for msg in filtered:
            sender = msg.get('sender', {}).get('nickname', 
                     msg.get('sender', {}).get('user_id', '未知'))
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
                elif seg_type_str in ('node', 'nodes'):
                    forward_id = seg.id if hasattr(seg, 'id') else seg.get('id')
                    if forward_id:
                        expanded = await self._extract_forward_msg(client, forward_id)
                        forward_texts.extend(expanded)
            msg_parts = []
            if text:
                if truncate and len(text) > 20:
                    display_text = text[:20] + "..."
                else:
                    display_text = text
                msg_parts.append(display_text)

            # 为每张图片分配编号并记录
            for img_url in images:
                # 限制最多取 2 张（原逻辑保留）
                if len(all_images) >= self.max_messages:  # 防止无限增长，但原逻辑有 max_messages 限制
                    break
                all_images.append(img_url)
                msg_parts.append(f"[图#{image_counter}]")
                image_counter += 1

            if msg_parts:
                msg_texts.append(f"[{ts}] {sender}: {' '.join(msg_parts)}")
            else:
                # 既无文本也无图片（不可能，但安全处理）
                msg_texts.append(f"[{ts}] {sender}: [空消息]")

        if not msg_texts:
            return "", [], 0

        full_text = "\n".join(msg_texts)
        # 限制文本长度（截断末尾）
        if len(full_text) > self.max_text_chars:
            full_text = full_text[-self.max_text_chars:]
            full_text = "（消息过多已截断）\n" + full_text

        return full_text, all_images, len(msg_texts)
    async def _prepare_messages(self, event: AstrMessageEvent, keyword: str, truncate: bool = False):
        """公共逻辑：获取平台、客户端、检测引用消息、拉取、过滤、拼接。"""
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

        # 检测引用消息（增加详细日志）
        start_time = None
        reply_seg = None
        logger.info("开始检测引用消息...")
        for seg in event.message_obj.message:
            seg_type = seg.type if hasattr(seg, 'type') else seg.get('type')
            logger.info(f"消息段类型: {seg_type}")
            # 兼容枚举或字符串
            seg_type_str = seg_type.value if hasattr(seg_type, 'value') else str(seg_type)
            if seg_type_str == 'reply':
                reply_seg = seg
                break
        # 检测引用消息
        start_time = None
        reply_seg = None
        for seg in event.message_obj.message:
            # 直接判断类型
            if isinstance(seg, Reply):
                reply_seg = seg
                break
        if reply_seg:
            # 直接访问 id 属性
            reply_id = reply_seg.id
            if reply_id:
                try:
                    msg_resp = await client.api.call_action('get_msg', message_id=int(reply_id))
                    if msg_resp and 'time' in msg_resp:
                        start_time = msg_resp['time']
                except Exception as e:
                    logger.warning(f"获取引用消息详情失败: {e}")

        full_text, all_images, msg_count = await self._fetch_and_filter(
            client, group_id, keyword, truncate=truncate, start_time=start_time
        )
        return full_text, all_images, msg_count
    @filter.command("回顾")
    async def summarize_by_keyword(self, event: AstrMessageEvent, keyword: str):
        """输入 /回顾 关键词，回溯最近两天的群聊记录并总结"""
        if not keyword:
            yield event.plain_result("请提供关键词，例如：/回顾 项目进度")
            return
        if not event.message_obj.group_id:
            yield event.plain_result("此指令只能在群聊中使用。")
            return

        group_id = event.message_obj.group_id
        yield event.plain_result(f"正在检索群聊中与“{keyword}”相关的消息...")

        try:
            full_text, all_images, _ = await self._prepare_messages(event, keyword, truncate=False)
        except Exception as e:
            yield event.plain_result(f"准备消息失败：{str(e)}")
            return

        provider = await self.context.get_using_provider_async(umo=event.unified_msg_origin)
        if not provider:
            yield event.plain_result("未找到可用的 LLM 提供商。")
            return

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

        try:
            llm_resp = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
                image_urls=all_images if all_images else None
            )
            summary = llm_resp.completion_text or "LLM 未返回有效总结。"
            yield event.plain_result(f"关于“{keyword}”的群聊总结\n\n{summary}")
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            yield event.plain_result(f"调用 LLM 失败：{str(e)}")
                
 

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
    @filter.command("回debug顾")
    async def debug_summarize(self, event: AstrMessageEvent, keyword: str):
        """调试版：不调用LLM，直接输出本应输入给LLM的消息内容（每条消息截取前2个字）"""
        if not keyword:
            yield event.plain_result("请提供关键词，例如：/回debug顾 项目进度")
            return
        if not event.message_obj.group_id:
            yield event.plain_result("此指令只能在群聊中使用。")
            return

        group_id = event.message_obj.group_id
        yield event.plain_result(f"调试模式：正在检索与“{keyword}”相关的消息...")

        try:
            full_text, all_images, msg_count = await self._prepare_messages(event, keyword, truncate=True)
        except Exception as e:
            yield event.plain_result(f"准备消息失败：{str(e)}")
            return

        debug_output = f"调试输出（共 {msg_count} 条匹配消息，{len(all_images)} 张图片）\n\n{full_text}"

        # ====== 新增：输出所有图片 URL ======
        if all_images:
            debug_output += "\n\n图片URL列表：\n" + "\n".join(all_images)
        # =================================

        yield event.plain_result(debug_output)
    async def terminate(self):
        logger.info("插件 astrbot_plugin_summary 已卸载")