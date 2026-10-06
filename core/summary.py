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

class SummaryMixin:
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
