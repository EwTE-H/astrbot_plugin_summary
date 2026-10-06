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

class TimeUtilsMixin:
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
