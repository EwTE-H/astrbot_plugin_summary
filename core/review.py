import os
import time
import json
import re
import asyncio
from datetime import datetime, date
from typing import List, Optional, Tuple, Dict
from urllib.parse import quote, urlparse
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Reply

class ReviewMixin:
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

        # 检查页面是否已存在：存在则追加到末尾（带分隔线），不存在则新建（避免覆盖历史归档）
        q = sess.post(api_url, data={
            'action': 'query', 'titles': page_title, 'prop': 'info', 'format': 'json',
        }).json()
        pages = (q.get('query') or {}).get('pages') or {}
        exists = not any(p.get('missing') is not None for p in pages.values())
        edit_data = {
            'action': 'edit', 'title': page_title,
            'summary': f'review upload: 群聊回顾归档（{len(wanted)} 张图）',
            'token': csrf, 'assert': 'user', 'format': 'json',
        }
        if exists:
            # 追加时在前面补一个空行，避免新内容直接贴在旧内容末尾（否则标题/段落粘连）
            edit_data['appendtext'] = "\n\n" + final_text
            logger.info(f"[REVIEW-UP] 页面 {page_title} 已存在，追加到末尾（前置空行）")
        else:
            edit_data['text'] = final_text
            logger.info(f"[REVIEW-UP] 页面 {page_title} 不存在，新建")
        j = sess.post(api_url, data=edit_data).json()
        if j.get('edit', {}).get('result') != 'Success':
            raise RuntimeError(f"编辑失败: {json.dumps(j, ensure_ascii=False)}")

        newrev = j['edit'].get('newrevid')
        info = f"已写入 {page_title}（{len(final_text)} 字节，新版本 {newrev}）。"
        info += f" 引用图片 {len(wanted)} 张，成功 {len(file_map)} 张"
        if failed:
            info += f"，失败编号：{','.join(str(x) for x in failed)}"

        # 文章访问链接：域名取自 api_url，标题做百分号编码（中文 -> %XX，避免链接含非 ASCII）
        _p = urlparse(api_url)
        article_url = f"{_p.scheme}://{_p.netloc}/wiki/{quote(page_title.replace(' ', '_'))}"

        mapping = {}
        for n, fn in file_map.items():
            m = (meta_by_n.get(n) or {})
            src = f"{m.get('ts','')} {m.get('sender','')}".strip() or f"图#{n}"
            mapping[n] = {'file': fn, 'src': src}
        return info, final_text, mapping, article_url

    # ---------- 指令：回debug顾 ----------
