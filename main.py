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

from .core.time_utils import TimeUtilsMixin
from .core.args import ArgsMixin
from .core.message import MessageMixin
from .core.images import ImageMixin
from .core.wiki import WikiMixin
from .core.pack import PackMixin
from .core.summary import SummaryMixin
from .core.llm import LLMMixin
from .core.review import ReviewMixin


class PluginSummary(TimeUtilsMixin, ArgsMixin, MessageMixin, ImageMixin, WikiMixin,
                   PackMixin, SummaryMixin, LLMMixin, ReviewMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.default_days = 2
        self.max_messages = 1000
        self.max_text_chars = 50000
        self.max_images = 20
        self.max_upload_images = 20   # 单次归档最多往 wiki 传几张图（与 max_images 一致）
        logger.info("[BRANCH] PluginSummary __init__ 完成")

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

    @filter.command("回debug顾")
    async def debug_summarize(self, event: AstrMessageEvent):
        logger.info("[BRANCH] debug_summarize 进入")
        result = await self._execute_summary(event)
        if result is None:
            logger.info("[BRANCH] debug_summarize _execute_summary 返回 None，退出")
        else:
            logger.info("[BRANCH] debug_summarize 完成，所有信息已在日志中")

    async def terminate(self):
        logger.info("插件 astrbot_plugin_summary 已卸载")
