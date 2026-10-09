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
        logger.info("[BRANCH] PluginSummary __init__ 完成")
        # 启动 coverage，只统计本插件目录
        self.cov = coverage.Coverage(source=['.'])  # 或者写插件所在目录的绝对路径
        self.cov.start()
        logger.info("[BRANCH] Coverage started")

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

    @filter.command("回顾上传backup")
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
                "  /回顾上传backup <主题> <页面名称> <开始时间> <结束时间>\n"
                "  /回顾上传backup <主题> <页面名称>   （需引用一条群消息）\n"
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

        logger.info(f"[REVIEW-UP] 已取到 {msg_count} 条消息、{len(all_images)} 张图片，页面={page_name}")
        await event.send(event.plain_result("正在生成归档并上传，请稍候…"))

        # 全部可用图（按编号升序），与文末清单顺序严格一致；直传 QQ 直链，不下本地
        img_refs = [m['url'] for m in image_meta if m.get('url')]
        logger.info(f"[REVIEW-UP] 本次附带 {len(img_refs)} 张图给 LLM")

        try:
            summary = await self._run_summary_llm(
                event, system_prompt, user_prompt, img_refs,
                tag="REVIEW-UP/llm", stream=False)
        except Exception as e:
            logger.error(f"[REVIEW-UP] LLM 调用失败: {e}", exc_info=True)
            await event.send(event.plain_result("生成失败，详见日志。"))
            return

        if not summary.strip():
            await event.send(event.plain_result("LLM 未返回有效内容，已取消上传。"))
            return

        refs = re.findall(r'\[图#(\d+)\]', summary)
        logger.info(f"[REVIEW-UP] 正文长度={len(summary)}，引用图片编号={refs}")
        # 兜底：正文引用了清单外（或已失效）的编号时，直接剔除
        allowed = {m['n'] for m in image_meta if m.get('url')}
        dropped = sorted({int(r) for r in refs} - allowed)
        if dropped:
            logger.warning(f"[REVIEW-UP] 正文引用了清单外编号 {dropped}，已剔除对应标记")
            for d in dropped:
                summary = re.sub(r'\[图#%d\][ \t]*\n?([ \t]*图注[:：].*)?\n?' % d, '', summary)

        try:
            msg, final_text, mapping, article_url = await asyncio.to_thread(
                self._review_up_publish, summary, list(all_images),
                wiki_api_url, wiki_username, wiki_password, page_name,
                list(image_meta))
        except Exception as e:
            logger.error(f"[REVIEW-UP] wiki 上传异常: {e}", exc_info=True)
            await event.send(event.plain_result("上传失败，详见日志。"))
            return

        # 技术信息（字节数/版本号/图片张数）只写日志，不刷群；群里只发归档链接，对群友有用
        logger.info(f"[REVIEW-UP] {msg}")
        await event.send(event.plain_result(f"回顾已归档：{article_url}"))
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
            # 合并转发里把 [图#N] 渲染成真实图片（与 wiki 上 [[File:...|thumb]] 对应）
            img_map = {m['n']: m['url'] for m in image_meta if m.get('url')}
            if img_map:
                await self._send_as_forward_with_images(event, summary, img_map)
                logger.info(f"[REVIEW-UP] 合并转发已附图，编号数={len(img_map)}")
            else:
                await self._send_as_forward(event, summary)
        except Exception as e:
            logger.warning(f"[REVIEW-UP] 转发原文失败（不影响已上传结果）: {e}")
        logger.info("[REVIEW-UP] review_and_upload 完成")

    @filter.command("回顾上传")
    async def review_and_upload_test(self, event: AstrMessageEvent):
        """同 /回顾上传，但 LLM 直连 GLM API（图文混排），绕过 AStrBot。
        API Key 取自配置项 glm_api_key（secret，不在代码中硬编码）。"""
        logger.info("[REVIEW-UP-TEST] review_and_upload_test 进入")

        page_name = ""
        parsed = None
        try:
            page_name, parsed = self._review_up_parse_args(event)
        except ValueError as e:
            await event.send(event.plain_result(
                f"参数错误：{e}\n\n"
                "用法：\n"
                "  /回顾上传 <主题> <页面名> <开始时间> <结束时间>\n"
                "  /回顾上传 <主题> <页面名>   （需引用一条群消息）\n"
                "页面名称中的空格请用下划线 _ 代替。"))
            return

        glm_api_key = (self.config.get("glm_api_key", "") or "").strip()
        glm_model = (self.config.get("glm_model", "") or "").strip() or "glm-5.3-flash"
        glm_base_url = (self.config.get("glm_base_url", "") or "").strip() or \
            "https://open.bigmodel.cn/api/paas/v4"
        if not glm_api_key:
            await event.send(event.plain_result(
                "未配置 glm_api_key。请在 AStrBot 管理面板 → 插件配置中填写 glm_api_key（secret 项）。"))
            return

        wiki_api_url = (self.config.get("wiki_api_url", "") or "").strip() or \
            'https://lumorganix.miraheze.org/w/api.php'
        wiki_username = (self.config.get("wiki_username", "") or "").strip()
        wiki_password = self.config.get("wiki_password", "") or ""
        if not wiki_username or not wiki_password:
            await event.send(event.plain_result(
                "尚未配置 wiki 账号或密码。请在 AStrBot 管理面板 → 插件配置中填写 "
                "wiki_username 与 wiki_password。"))
            return

        result = await self._execute_summary(event, parsed_override=parsed, attached=True)
        if result is None:
            return
        full_text, all_images, msg_count, keyword, system_prompt, user_prompt, image_meta = result

        # 「回顾上传test」prompt 可配置化：配置非空则覆盖内置默认，仅作用于本指令
        glm_sys_cfg = (self.config.get("glm_system_prompt") or "").strip()
        glm_usr_tpl = (self.config.get("glm_user_prompt_template") or "").strip()
        if glm_sys_cfg:
            system_prompt = glm_sys_cfg
            logger.info("[REVIEW-UP-TEST] 使用配置 glm_system_prompt 覆盖内置 system 提示")
        if glm_usr_tpl:
            try:
                manifest = self._build_image_manifest(image_meta, attached=True)
                user_prompt = glm_usr_tpl.format(keyword=keyword, full_text=full_text, manifest=manifest)
                logger.info("[REVIEW-UP-TEST] 使用配置 glm_user_prompt_template 重建 user 提示")
            except (KeyError, IndexError) as e:
                logger.warning(f"[REVIEW-UP-TEST] user 模板占位符错误({e})，回退内置拼接")

        logger.info(f"[REVIEW-UP-TEST] 已取到 {msg_count} 条消息、{len(all_images)} 张图片，页面={page_name}")
        await event.send(event.plain_result("正在生成归档并上传，请稍候…"))

        try:
            summary = await self._run_summary_glm_direct(
                system_prompt, user_prompt, image_meta, glm_api_key, glm_model, glm_base_url)
        except Exception as e:
            logger.error(f"[REVIEW-UP-TEST] GLM 调用失败: {e}", exc_info=True)
            await event.send(event.plain_result("生成失败，详见日志。"))
            return

        if not summary.strip():
            await event.send(event.plain_result("GLM 未返回有效内容，已取消上传。"))
            return

        refs = re.findall(r'\[图#(\d+)\]', summary)
        allowed = {m['n'] for m in image_meta if m.get('url')}
        dropped = sorted({int(r) for r in refs} - allowed)
        if dropped:
            logger.warning(f"[REVIEW-UP-TEST] 正文引用了清单外编号 {dropped}，已剔除对应标记")
            for d in dropped:
                summary = re.sub(r'\[图#%d\][ \t]*\n?([ \t]*图注[:：].*)?\n?' % d, '', summary)

        try:
            msg, final_text, mapping, article_url = await asyncio.to_thread(
                self._review_up_publish, summary, list(all_images),
                wiki_api_url, wiki_username, wiki_password, page_name,
                list(image_meta))
        except Exception as e:
            logger.error(f"[REVIEW-UP-TEST] wiki 上传异常: {e}", exc_info=True)
            await event.send(event.plain_result("上传失败，详见日志。"))
            return

        logger.info(f"[REVIEW-UP-TEST] {msg}")
        await event.send(event.plain_result(f"回顾已归档：{article_url}"))
        try:
            table = [f"图#{n} <- {info['src']} -> {info['file']}"
                     for n, info in sorted(mapping.items())]
            if table:
                logger.info("[REVIEW-UP-TEST] 图片对照表（图#N <- 来源 -> wiki 文件）：\n"
                            + "\n".join(table))
        except Exception as e:
            logger.warning(f"[REVIEW-UP-TEST] 记录对照表失败: {e}")
        try:
            img_map = {m['n']: m['url'] for m in image_meta if m.get('url')}
            if img_map:
                await self._send_as_forward_with_images(event, summary, img_map)
                logger.info(f"[REVIEW-UP-TEST] 合并转发已附图，编号数={len(img_map)}")
            else:
                await self._send_as_forward(event, summary)
        except Exception as e:
            logger.warning(f"[REVIEW-UP-TEST] 转发原文失败（不影响已上传结果）: {e}")
        logger.info("[REVIEW-UP-TEST] review_and_upload_test 完成")

    # ---------- 回顾上传：参数解析 ----------

    @filter.command("回debug顾")
    async def debug_summarize(self, event: AstrMessageEvent):
        """调试：扫描区间内图片的 subType，验证「表情包(表情)筛选」可行性。
        用法与 /回顾 完全一致：
          /回debug顾 <开始时间> <结束时间>        （时间模式）
          /回debug顾 <关键词>                     （需引用一条群消息，用其时间）
          /回debug顾                              （默认最近 default_days 天）
        不调用 LLM、不上传 wiki，只输出筛选统计，并限制拉取量防止刷爆日志。"""
        logger.info("[DBG-SIEVE] debug_summarize 进入")
        if not event.message_obj.group_id:
            await event.send(event.plain_result("此命令只能在群聊中使用。"))
            return

        # 复用 /回顾 的参数解析（语法一致）；debug 不需要 keyword，但时间模式照样支持
        raw = event.message_str.strip()
        parts = raw.split()
        args = parts[1:]
        start_time = end_time = None
        if args:
            try:
                parsed = self._parse_args(args)
            except ValueError as e:
                await event.send(event.plain_result(f"参数错误：{str(e)}"))
                return
            if parsed.get('mode') == 'time':
                start_time = parsed.get('start')
                end_time = parsed.get('end')
            # quote 模式：start_time 留空，下面用引用消息时间填充

        platform = self.context.get_platform('aiocqhttp')
        client = platform.get_client() if platform else None
        if not client:
            await event.send(event.plain_result("无法获取 QQ 协议端客户端。"))
            return

        self_id = event.message_obj.self_id
        group_id = event.message_obj.group_id

        # 引用消息 → 以引用时间作为起点（与 /回顾 一致）
        if start_time is None:
            for seg in event.message_obj.message:
                if isinstance(seg, Reply):
                    try:
                        msg_resp = await client.api.call_action('get_msg', message_id=int(seg.id))
                        if msg_resp and 'time' in msg_resp:
                            start_time = msg_resp['time']
                    except Exception as e:
                        logger.warning(f"[DBG-SIEVE] 获取引用消息时间失败: {e}")
                    break

        # 限制拉取量：活跃群一次拉几千条会把 terminal.log 冲爆
        max_dbg = 800
        try:
            full_text, all_images, msg_count, image_meta = await self._fetch_and_filter(
                client, group_id, start_time, end_time, self_id,
                collect_subtype=True, max_messages=max_dbg, filter_meme=False)
        except Exception as e:
            logger.error(f"[DBG-SIEVE] 拉取失败: {e}", exc_info=True)
            await event.send(event.plain_result(f"拉取消息失败：{e}"))
            return

        if not image_meta:
            await event.send(event.plain_result(
                f"区间内未检测到图片（共扫描 {msg_count} 条消息，上限 {max_dbg}）。"))
            logger.info("[DBG-SIEVE] 无图片，结束")
            return

        # 统计 subType 分布，并分别记录被剔除 / 保留的具体图片（回答「哪些被筛掉了」）
        dropped = []   # 表情包(subType=1)，即会被筛掉的
        kept = []     # 其余保留的
        for m in image_meta:
            st = m.get('subType')
            ctx = (m.get('ctx', '') or '').replace('\n', ' ').strip()
            entry = (m['n'], st, m.get('sender', ''), m.get('ts', ''), ctx)
            if st == 1:
                dropped.append(entry)
            else:
                kept.append(entry)
        total = len(image_meta)
        normal = sum(1 for m in image_meta if m.get('subType') == 0)
        meme = len(dropped)
        gif = sum(1 for m in image_meta if m.get('subType') == 2)
        unknown = sum(1 for m in image_meta if m.get('subType') is None)

        def _tag(st):
            return '表情包' if st == 1 else '普通' if st == 0 else 'GIF' if st == 2 else '未知'

        lines = [
            f"[调试·表情包筛选] 区间内图片共 {total} 张（群消息 {msg_count} 条）",
            f"subType 分布：普通图(0)={normal}，表情包(1)={meme}，GIF(2)={gif}，未知={unknown}",
            f"→ 按「仅剔除表情包(subType=1)」：保留 {len(kept)} 张，剔除 {meme} 张",
        ]
        # 被剔除的（用户最关心：到底哪些图被筛掉了）
        if dropped:
            lines.append("")
            lines.append(f"【被剔除的表情包（共 {len(dropped)} 张）】")
            for n, st, sender, ts, ctx in dropped:
                snippet = (ctx[:40] + '…') if len(ctx) > 40 else ctx
                lines.append(f"  图#{n} [表情包] {sender} {ts} {snippet}")
        # 保留的（紧凑一行，过长截断）
        if kept:
            lines.append("")
            lines.append(f"【保留的（共 {len(kept)} 张）】")
            kept_str = "  " + " ".join(f"图#{n}[{_tag(st)}]" for n, st, *_ in kept)
            if len(kept_str) > 1500:
                kept_str = kept_str[:1500] + " …"
            lines.append(kept_str)

        summary = "\n".join(lines)
        await event.send(event.plain_result(summary))
        logger.info(f"[DBG-SIEVE] 完成：{summary}")

    async def terminate(self):
        if hasattr(self, 'cov'):
            self.cov.stop()
            self.cov.save()
            self.cov.html_report(directory='cov_html')
            logger.info("[BRANCH] Coverage report saved to cov_html/")
        logger.info("插件 astrbot_plugin_summary 已卸载")
