"""
MaiBot 1.0.1 / sdk2.x expenses summary plugin.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

try:
    from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
except ImportError:  # Allows local syntax checks without the MaiBot SDK.
    def Command(*_args: Any, **_kwargs: Any) -> Callable:
        return lambda func: func

    def Tool(*_args: Any, **_kwargs: Any) -> Callable:
        return lambda func: func

    def Field(default: Any = None, **_kwargs: Any) -> Any:
        default_factory = _kwargs.get("default_factory")
        if default_factory:
            return default_factory()
        return default

    class PluginConfigBase:
        pass

    class MaiBotPlugin:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.ctx = kwargs.get("ctx")
            self.config = kwargs.get("config")


REPORT_MODE_DEFAULT = "default"
REPORT_MODE_MAICHENFENG = "maichenfeng"


@dataclass
class ModelCost:
    name: str
    requests: int = 0
    replies: int = 0
    cost: float = 0.0


@dataclass
class ReportData:
    date_text: str
    total_requests: int
    total_replies: int
    total_cost: float
    model_costs: list[ModelCost]


@dataclass
class FunElements:
    xiao_name: str
    location: str
    went_to: str
    poem: str


class ReportConfig(PluginConfigBase):
    mode: str = Field(
        default=REPORT_MODE_DEFAULT,
        title="财报模式",
        description="可选：default/默认、maichenfeng/麦晨风",
    )
    title: str = Field(default="今日模型调用财报", title="默认模式标题")
    llm_task: str = Field(default="utils", title="财报文案模型任务名")
    use_forward_message: bool = Field(default=True, title="使用转发消息发送")
    default_opening: str = Field(
        default="{date}模型调用财报已生成，以下是今日请求次数、回复量与模型成本汇总。",
        title="默认模式开头文本",
        description="可使用 {date} 占位符表示当天日期",
    )


class PermissionConfig(PluginConfigBase):
    query_admin_only: bool = Field(default=False, title="查询命令仅管理员可用")
    admins: list[str] = Field(default=[], title="管理员 QQ 号列表")


class SchedulerConfig(PluginConfigBase):
    enabled: bool = Field(default=False, title="启用定时发送")
    time: str = Field(default="23:30", title="定时发送时间")
    group_ids: list[str] = Field(default=[], title="定时发送群号")
    private_ids: list[str] = Field(default=[], title="定时发送私聊 QQ")


class FallbackConfig(PluginConfigBase):
    xiao_names: list[str] = Field(default=["小麦"], title="麦晨风模式小名")
    locations: list[str] = Field(
        default=["KFC", "卧室", "广州塔", "下水道"],
        title="麦晨风模式地点",
    )
    poems: list[str] = Field(
        default=[
            "How do you do, you like me and I like you.",
            "Shut up! I read this inside the book I read before.",
        ],
        title="麦晨风模式随机诗句",
    )
    thanks_list: list[str] = Field(default=["810", "艾斯比"], title="感谢名单")


# BGM support is temporarily disabled in 1.0.1 because the current sdk2.x
# public send capability does not expose send.audio.
# class AudioConfig(PluginConfigBase):
#     enabled: bool = Field(default=False, title="启用 BGM 音频")
#     file_location: str = Field(default="audio.mp3", title="音频文件路径")


class PluginMetaConfig(PluginConfigBase):
    config_version: str = Field(default="1.0.1", title="配置文件版本")


class ExpensesSummaryConfig(PluginConfigBase):
    plugin: PluginMetaConfig = Field(default_factory=PluginMetaConfig, title="插件")
    report: ReportConfig = Field(default_factory=ReportConfig, title="财报")
    permission: PermissionConfig = Field(default_factory=PermissionConfig, title="权限")
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig, title="定时发送")
    fallback: FallbackConfig = Field(default_factory=FallbackConfig, title="麦晨风素材")
    # audio: AudioConfig = Field(default_factory=AudioConfig, title="音频")


class ExpensesSummaryPlugin(MaiBotPlugin):
    """Generate daily model usage and cost reports."""

    config_model = ExpensesSummaryConfig

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._scheduler_task: Optional[asyncio.Task] = None
        self._fallback_config = ExpensesSummaryConfig()

    async def on_load(self) -> None:
        config = self._get_config()
        if config.scheduler.enabled:
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def on_unload(self) -> None:
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None

    async def on_config_update(self, *args: Any, **kwargs: Any) -> None:
        new_config = _extract_updated_config(args, kwargs)
        if new_config is not None:
            self._fallback_config = new_config
        await self.on_unload()
        await self.on_load()

    @Command(
        name="expenses",
        description="生成今日模型调用财报",
        pattern=r"^/(?:expenses|今日财报)$",
    )
    async def expenses_command(
        self,
        ctx: Any = None,
        message: Any = None,
        stream_id: Optional[str] = None,
        chat_stream: Any = None,
        *_args: Any,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        target_stream_id = _first_present(
            stream_id,
            _get_stream_id(ctx),
            _get_stream_id(message),
            _get_stream_id(chat_stream),
            _get_stream_id(kwargs),
        )
        if not _can_query(self._get_config(), ctx, message, kwargs):
            return True, "你没有权限使用财报查询命令", True
        sent = await self._send_report(self.ctx, target_stream_id)
        return sent, "已发送今日模型调用财报" if sent else "财报发送失败", True

    @Command(
        name="expenses_mode",
        description="切换财报模式",
        pattern=r"^/(?:财报模式|expensesmode)(?:\s+(?P<mode>\S+))?$",
    )
    async def expenses_mode_command(
        self,
        ctx: Any = None,
        message: Any = None,
        stream_id: Optional[str] = None,
        chat_stream: Any = None,
        *_args: Any,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        target_stream_id = _first_present(
            stream_id,
            _get_stream_id(ctx),
            _get_stream_id(message),
            _get_stream_id(chat_stream),
            _get_stream_id(kwargs),
        )
        config = self._get_config()
        if not _is_admin(config, ctx, message, kwargs):
            response = "你没有权限切换财报模式"
            await _send_command_response(self.ctx, response, target_stream_id)
            return True, response, True

        mode_text = _extract_mode_argument(message, kwargs)
        if not mode_text:
            current_mode = "麦晨风" if _normalize_mode(config.report.mode) == REPORT_MODE_MAICHENFENG else "默认"
            response = f"当前财报模式：{current_mode}。用法：/财报模式 默认 或 /财报模式 麦晨风"
            await _send_command_response(self.ctx, response, target_stream_id)
            return True, response, True

        mode = _normalize_mode(mode_text)
        if not _is_valid_mode_text(mode_text):
            response = "未知财报模式，可用：默认、麦晨风、default、maichenfeng"
            await _send_command_response(self.ctx, response, target_stream_id)
            return True, response, True

        config.report.mode = mode
        label = "麦晨风" if mode == REPORT_MODE_MAICHENFENG else "默认"
        response = f"财报模式已切换为：{label}"
        await _send_command_response(self.ctx, response, target_stream_id)
        return True, response, True

    @Tool(
        name="expenses_summary",
        description="生成并发送今日模型调用次数与成本财报，可用于公开收入、财务总结、麦晨风风格汇报等场景。",
    )
    async def expenses_tool(
        self,
        ctx: Any = None,
        stream_id: Optional[str] = None,
        *_args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        target_stream_id = _first_present(
            stream_id,
            _get_stream_id(ctx),
            _get_stream_id(kwargs),
        )
        sent = await self._send_report(self.ctx, target_stream_id)
        return {
            "success": sent,
            "content": (
                "今日模型调用财报已生成并发送。不要再额外复述财报内容，下一步等待用户新消息。"
                if sent
                else "今日模型调用财报生成完成但发送失败。"
            ),
            "metadata": {"pause_execution": sent},
        }

    async def _send_report(self, ctx: Any, stream_id: Optional[str] = None) -> bool:
        config = self._get_config()
        mode = _normalize_mode(config.report.mode)
        data = await _collect_report_data(ctx)
        image = await _render_report_image(ctx, data, mode, config.report.title)
        fun = await _generate_fun_elements(ctx, config) if mode == REPORT_MODE_MAICHENFENG else None
        nodes = _build_forward_nodes(data, image, mode, config, fun)
        if config.report.use_forward_message:
            sent = await _send_forward(ctx, nodes, stream_id)
        else:
            sent = await _send_plain_messages(ctx, nodes, stream_id)
        # BGM is disabled until sdk2.x provides a public send.audio capability.
        return sent

    async def _scheduler_loop(self) -> None:
        while True:
            config = self._get_config()
            await asyncio.sleep(_seconds_until(config.scheduler.time))
            try:
                await self._send_scheduled_reports()
            except Exception as exc:
                _log(self.ctx, "error", f"定时发送财报失败: {exc}")

    async def _send_scheduled_reports(self) -> None:
        config = self._get_config()
        targets = list(config.scheduler.group_ids) + list(config.scheduler.private_ids)
        if not targets:
            _log(self.ctx, "warning", "定时财报已启用，但没有配置目标群聊或私聊")
            return
        for target in targets:
            target_ctx = await _resolve_target_context(self.ctx, target)
            if target_ctx:
                await self._send_report(target_ctx, _get_stream_id(target_ctx))

    def _get_config(self) -> ExpensesSummaryConfig:
        try:
            config = self.config
        except RuntimeError:
            return self._fallback_config
        return config or self._fallback_config


async def _collect_report_data(ctx: Any) -> ReportData:
    local = _resolve_statistics_api(ctx)
    if local is None:
        _log(ctx, "warning", "财报统计失败: statistics API 不可用")
    costs_raw = await _maybe_await(
        _call_first(local, ["model_trend"], days=1, bucket="hour", top_models=50, metric="cost")
    )
    requests_raw = await _maybe_await(
        _call_first(local, ["model_trend"], days=1, bucket="hour", top_models=50, metric="request")
    )
    messages_raw = await _maybe_await(
        _call_first(local, ["message_trend"], days=1, bucket="hour", top_chats=50)
    )

    model_costs = _merge_model_stats(None, costs_raw, requests_raw, today_only=True)
    total_requests = sum(item.requests for item in model_costs)
    total_cost = sum(item.cost for item in model_costs)
    total_replies = _series_total(messages_raw, today_only=True)

    if total_requests <= 0:
        total_requests = _series_total(requests_raw, today_only=True)
    if total_cost <= 0:
        total_cost = _series_total(costs_raw, today_only=True)

    return ReportData(
        date_text=datetime.now().strftime("%Y年%m月%d日"),
        total_requests=total_requests,
        total_replies=total_replies,
        total_cost=total_cost,
        model_costs=sorted(model_costs, key=lambda item: item.cost, reverse=True),
    )


def _resolve_statistics_api(ctx: Any) -> Any:
    for path in ("statistics.local", "statistics", "stats.local", "stats"):
        candidate = _get_path(ctx, path)
        if candidate is None:
            continue
        if all(callable(getattr(candidate, name, None)) for name in ("models", "model_trend", "message_trend")):
            return candidate
    return None


async def _generate_fun_elements(ctx: Any, config: ExpensesSummaryConfig) -> FunElements:
    configured_xiao_name = _pick_configured_text(config.fallback.xiao_names, "小麦")
    fallback = FunElements(
        xiao_name=configured_xiao_name,
        location=random.choice(config.fallback.locations or ["KFC"]),
        went_to=_fallback_went_to(config.fallback.locations),
        poem=random.choice(config.fallback.poems or ["谢谢大家。"]),
    )
    llm = _get_path(ctx, "llm")
    generate = getattr(llm, "generate", None)
    if not callable(generate):
        _log(ctx, "warning", "财报文案模型调用失败: ctx.llm.generate 不可用，使用 fallback 素材")
        return fallback

    prompt = (
        "为麦晨风风格的模型调用财报生成三个短素材，只输出 JSON，不要解释。\n"
        "字段必须是：location、went_to、poem。\n"
        "location: 一个荒诞但不冒犯的汇报地点。\n"
        "went_to: 按“我去了：地点、地点、地点、地点 回复群员信息📱。”格式输出，地点尽量不重复，可以带 emoji。\n"
        "poem: 一句40字以内的诗句、歌词、台词或短句。\n"
        "示例：{\"location\":\"KFC\",\"went_to\":\"我去了：火星🚀、深海🐙、KFC🍗、自宅卧室😴 回复群员信息📱。\",\"poem\":\"月落乌啼霜满天，江枫渔火对愁眠。\"}"
    )
    try:
        llm_task = _get_llm_task(config)
        result = await _call_llm_generate(generate, prompt, llm_task)
    except Exception as exc:
        _log(ctx, "warning", f"财报文案模型调用失败，使用 fallback 素材: {exc}")
        return fallback

    text = _normalize_llm_text(result)
    if not text:
        _log(ctx, "warning", f"财报文案模型返回为空，使用 fallback 素材: {_summarize_value(result)}")
        return fallback
    values = _parse_fun_elements_text(text)
    if not values:
        _log(ctx, "warning", f"财报文案模型返回无法解析，使用 fallback 素材: {text[:120]}")
        return fallback
    _log(ctx, "info", f"财报文案模型调用成功，使用任务: {_get_llm_task(config)}")
    return FunElements(
        xiao_name=configured_xiao_name,
        location=values.get("location") or fallback.location,
        went_to=_normalize_went_to(values.get("went_to")) or fallback.went_to,
        poem=values.get("poem") or fallback.poem,
    )


async def _call_llm_generate(generate: Callable, prompt: str, llm_task: str) -> Any:
    call_specs = (
        ((), {"prompt": prompt, "model": llm_task, "temperature": 0.8, "max_tokens": 180}),
        ((), {"prompt": prompt, "model": llm_task, "temperature": 0.8}),
        ((prompt,), {"model": llm_task, "temperature": 0.8, "max_tokens": 180}),
        ((prompt,), {"model": llm_task, "temperature": 0.8}),
        ((prompt,), {"model": llm_task}),
    )
    last_type_error: Optional[TypeError] = None
    for args, kwargs in call_specs:
        try:
            return await _maybe_await(generate(*args, **kwargs))
        except TypeError as exc:
            last_type_error = exc
            continue
    if last_type_error:
        raise last_type_error
    return None


def _get_llm_task(config: ExpensesSummaryConfig) -> str:
    task = getattr(config.report, "llm_task", None)
    if not task:
        task = getattr(config.report, "llm_model", None)
    return str(task or "utils")


def _normalize_llm_text(result: Any) -> str:
    if isinstance(result, str):
        return result.strip()
    for key in ("response", "text", "content", "reply", "message", "result", "output"):
        value = _pick(result, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = _normalize_llm_text(value)
            if nested:
                return nested
    return str(result or "").strip()


def _parse_fun_elements_text(text: str) -> dict[str, str]:
    json_values = _parse_fun_elements_json(text)
    if json_values:
        return json_values
    if _looks_like_json_text(text):
        return {}

    values: dict[str, str] = {}
    for line in str(text or "").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
        elif "：" in line:
            key, value = line.split("：", 1)
        else:
            continue
        normalized_key = key.strip().lower()
        clean_value = _clean_fun_value(value)
        if not clean_value:
            continue
        if "去了" in normalized_key or "went" in normalized_key:
            values["went_to"] = clean_value[:120]
        elif "地点" in normalized_key or "location" in normalized_key:
            values["location"] = clean_value[:40]
        elif "诗" in normalized_key or "句" in normalized_key or "poem" in normalized_key:
            values["poem"] = clean_value[:80]
    return values


def _parse_fun_elements_json(text: str) -> dict[str, str]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return _parse_fun_elements_jsonish(raw)
    if not isinstance(data, dict):
        return {}
    mappings = {
        "location": ("location", "地点"),
        "went_to": ("went_to", "去了", "went"),
        "poem": ("poem", "诗句", "短句"),
    }
    values: dict[str, str] = {}
    for target, keys in mappings.items():
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                values[target] = _clean_fun_value(value)
                break
    return values


def _parse_fun_elements_jsonish(text: str) -> dict[str, str]:
    raw = str(text or "")
    mappings = {
        "location": ("location", "地点"),
        "went_to": ("went_to", "去了", "went"),
        "poem": ("poem", "诗句", "短句"),
    }
    values: dict[str, str] = {}
    for target, keys in mappings.items():
        for key in keys:
            match = re.search(
                rf'["“]?{re.escape(key)}["”]?\s*[:：]\s*["“](.*?)(?=["”]\s*[,，}}]|$)',
                raw,
                flags=re.DOTALL,
            )
            if match:
                value = _clean_fun_value(match.group(1))
                if value:
                    values[target] = value
                    break
    return values


def _looks_like_json_text(text: str) -> bool:
    raw = str(text or "").strip()
    return raw.startswith("{") or raw.startswith("```") or '"location"' in raw or "'location'" in raw


def _clean_fun_value(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("\\n", " ").strip()
    text = text.strip(" \t\r\n`")
    text = text.strip('"“”\'‘’')
    text = text.strip(" \t\r\n,，:：")
    text = text.strip('"“”\'‘’')
    while text.endswith((",", "，", '"', "'", "”", "’")):
        text = text[:-1].strip()
    return text


def _pick_configured_text(values: list[str], fallback: str) -> str:
    choices = [str(item).strip() for item in (values or []) if str(item).strip()]
    return random.choice(choices) if choices else fallback


def _fallback_went_to(locations: list[str]) -> str:
    choices = list(dict.fromkeys(str(item).strip() for item in (locations or []) if str(item).strip()))
    if not choices:
        choices = ["KFC", "卧室", "广州塔", "下水道"]
    picked = random.sample(choices, k=min(4, len(choices)))
    return f"我去了：{'、'.join(picked)} 回复群员信息📱。"


def _normalize_went_to(text: Optional[str]) -> str:
    raw = _clean_fun_value(text)
    if not raw:
        return ""
    if "我去了：" not in raw:
        return raw[:120]

    prefix, rest = raw.split("我去了：", 1)
    suffix = ""
    for marker in (" 回复群员信息", "回复群员信息"):
        if marker in rest:
            rest, tail = rest.split(marker, 1)
            suffix_tail = _clean_fun_value(tail)
            suffix = f" 回复群员信息{suffix_tail}"
            break

    places = [_clean_fun_value(item) for item in rest.replace("，", "、").split("、")]
    places = [item for item in places if item]
    unique_places = list(dict.fromkeys(places))
    if not unique_places:
        return raw[:120]
    if not suffix:
        suffix = " 回复群员信息📱。"
    return f"{prefix}我去了：{'、'.join(unique_places[:4])}{suffix}"[:120]


def _summarize_value(value: Any) -> str:
    text = repr(value)
    return text[:240]


def _extract_updated_config(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Optional[ExpensesSummaryConfig]:
    for key in ("new_config", "config", "updated_config"):
        candidate = kwargs.get(key)
        if _looks_like_plugin_config(candidate):
            return candidate

    for candidate in reversed(args):
        if _looks_like_plugin_config(candidate):
            return candidate
    return None


def _looks_like_plugin_config(candidate: Any) -> bool:
    if candidate is None:
        return False
    if isinstance(candidate, ExpensesSummaryConfig):
        return True
    return all(hasattr(candidate, attr) for attr in ("report", "scheduler"))


def _merge_model_stats(
    models_raw: Any,
    costs_raw: Any,
    requests_raw: Any,
    today_only: bool = False,
) -> list[ModelCost]:
    merged: dict[str, ModelCost] = {}

    for item in _iter_items(models_raw):
        name = str(_pick(item, "model_name", "model", "name", default="未知模型"))
        stat = merged.setdefault(name, ModelCost(name=name))
        stat.requests += int(_pick_number(
            item,
            "requests",
            "request_count",
            "total_requests",
            "call_count",
            "calls",
            "count",
            "total",
        ))
        stat.replies += int(_pick_number(
            item,
            "replies",
            "reply",
            "reply_count",
            "total_replies",
            "response_count",
            "responses",
            "message_count",
            "messages",
        ))
        stat.cost += _pick_number(
            item,
            "cost",
            "total_cost",
            "amount",
            "total_amount",
            "price",
            "total_price",
        )

    for name, value in _series_values_by_label(costs_raw, today_only=today_only).items():
        stat = merged.setdefault(name, ModelCost(name=name))
        if stat.cost <= 0:
            stat.cost = value

    for name, value in _series_values_by_label(requests_raw, today_only=today_only).items():
        stat = merged.setdefault(name, ModelCost(name=name))
        if stat.requests <= 0:
            stat.requests = int(value)

    return [item for item in merged.values() if item.requests or item.replies or item.cost]


async def _render_report_image(ctx: Any, data: ReportData, mode: str, title: str) -> Any:
    html_doc = _build_report_html(data, mode, title)
    renderer = _get_path(ctx, "render")
    html2png = getattr(renderer, "html2png", None)
    if callable(html2png):
        try:
            return await _maybe_await(
                html2png(
                    html_doc,
                    selector=".sheet",
                    viewport={"width": 900, "height": 1200},
                    device_scale_factor=1.0,
                    full_page=True,
                )
            )
        except TypeError:
            return await _maybe_await(html2png(html_doc))
    return html_doc


def _build_report_html(data: ReportData, mode: str, title: str) -> str:
    is_fun = mode == REPORT_MODE_MAICHENFENG
    page_title = "麦晨风公开收入财报" if is_fun else title
    subtitle = "不可以不交，但成本可以公开" if is_fun else "今日 0 点至当前的模型调用概览"
    rows = data.model_costs or [ModelCost(name="暂无模型记录")]
    max_cost = max([item.cost for item in rows] + [0.01])
    body_rows = "\n".join(
        _model_row_html(item, max_cost) for item in rows[:12]
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  width: 900px;
  min-height: 1200px;
  font-family: "Microsoft YaHei", "PingFang SC", Arial, sans-serif;
  color: #202124;
  background: #f4f7f8;
}}
.sheet {{
  min-height: 1200px;
  padding: 54px;
  background: linear-gradient(180deg, #ffffff 0%, #eef4f5 100%);
}}
.head {{
  border-left: 10px solid #0f766e;
  padding-left: 24px;
  margin-bottom: 34px;
}}
.kicker {{
  font-size: 28px;
  color: #52605f;
  margin-bottom: 8px;
}}
h1 {{
  margin: 0;
  font-size: 54px;
  line-height: 1.14;
  letter-spacing: 0;
}}
.subtitle {{
  margin-top: 12px;
  font-size: 26px;
  color: #56616a;
}}
.metrics {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 18px;
  margin: 34px 0;
}}
.metric {{
  background: #ffffff;
  border: 1px solid #d9e3e4;
  border-radius: 8px;
  padding: 22px;
}}
.label {{
  font-size: 22px;
  color: #667478;
}}
.value {{
  margin-top: 10px;
  font-size: 38px;
  font-weight: 700;
  color: #0b3b3f;
}}
.section-title {{
  font-size: 30px;
  font-weight: 700;
  margin: 40px 0 18px;
}}
.row {{
  background: #ffffff;
  border: 1px solid #dce5e6;
  border-radius: 8px;
  padding: 18px 20px;
  margin-bottom: 12px;
}}
.row-top {{
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 18px;
}}
.model {{
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: 24px;
  font-weight: 700;
}}
.cost {{
  flex: 0 0 auto;
  font-size: 24px;
  color: #0f766e;
  font-weight: 700;
}}
.bar {{
  height: 12px;
  margin: 14px 0 8px;
  border-radius: 99px;
  background: #dde7e8;
  overflow: hidden;
}}
.bar span {{
  display: block;
  height: 100%;
  background: #0f766e;
}}
.minor {{
  font-size: 20px;
  color: #657276;
}}
.footer {{
  margin-top: 36px;
  padding-top: 20px;
  border-top: 1px solid #cad7d8;
  font-size: 22px;
  color: #52605f;
}}
</style>
</head>
<body>
<main class="sheet">
  <section class="head">
    <div class="kicker">{html.escape(data.date_text)}</div>
    <h1>{html.escape(page_title)}</h1>
    <div class="subtitle">{html.escape(subtitle)}</div>
  </section>
  <section class="metrics">
    <div class="metric"><div class="label">累计请求</div><div class="value">{data.total_requests}</div></div>
    <div class="metric"><div class="label">回复消息</div><div class="value">{data.total_replies}</div></div>
    <div class="metric"><div class="label">回复成本</div><div class="value">{data.total_cost:.4f} 元</div></div>
  </section>
  <div class="section-title">各模型回复成本</div>
  {body_rows}
  <div class="footer">净收入：-{data.total_cost:.4f} 元。{"数据已经公开，股东请审阅。" if is_fun else "数据来自 MaiBot 本地统计接口。"}</div>
</main>
</body>
</html>"""


def _model_row_html(item: ModelCost, max_cost: float) -> str:
    width = max(4, min(100, int(item.cost / max_cost * 100)))
    detail = f"请求 {item.requests} 次"
    if item.replies > 0:
        detail += f" / 回复 {item.replies} 条"
    return f"""<div class="row">
  <div class="row-top">
    <div class="model">{html.escape(item.name)}</div>
    <div class="cost">{item.cost:.4f} 元</div>
  </div>
  <div class="bar"><span style="width:{width}%"></span></div>
  <div class="minor">{detail}</div>
</div>"""


def _build_forward_nodes(
    data: ReportData,
    image: Any,
    mode: str,
    config: ExpensesSummaryConfig,
    fun: Optional[FunElements] = None,
) -> list[dict[str, Any]]:
    opening = _build_opening(data, mode, config, fun)
    nodes = [
        _make_forward_node("text", opening),
        _image_node(image),
    ]
    if mode == REPORT_MODE_MAICHENFENG:
        nodes.append(_make_forward_node("text", _build_thanks(data, config, fun)))
    return nodes


def _build_opening(
    data: ReportData,
    mode: str,
    config: ExpensesSummaryConfig,
    fun: Optional[FunElements] = None,
) -> str:
    if mode == REPORT_MODE_MAICHENFENG:
        xiao_name = fun.xiao_name if fun else random.choice(config.fallback.xiao_names or ["小麦"])
        location = fun.location if fun else random.choice(config.fallback.locations or ["KFC"])
        went_to = fun.went_to if fun else _fallback_went_to(config.fallback.locations)
        return (
            f"我是{xiao_name}，我在{location}向各位网友兼股东汇报"
            f"{data.date_text}我在全网的收入情况。\n"
            f"{data.date_text}收入再次创出历史新高📈✨\n"
            f"我在{data.date_text}的税前总收入为：0万0元💸。其中：所有收入 0万0元。\n"
            "除广告收入和带货佣金外，在缴纳了约25%即 0万0元 的个人所得税之后，"
            "此为系统自动扣除，"
            "***不🙅‍♀️可🙅‍♀️能🙅‍♀️不🙅‍♀️交*** 😡💢（咬牙切齿😣），"
            "我的税后总收入为 0万0元🙃。\n\n"
            "🖕以上为我的收入情况，下面是我的支出情况👇\n\n"
            f"{data.date_text}{went_to}"
        )
    template = config.report.default_opening or ReportConfig().default_opening
    return template.replace("{date}", data.date_text)


def _build_thanks(
    data: ReportData,
    config: ExpensesSummaryConfig,
    fun: Optional[FunElements] = None,
) -> str:
    xiao_name = fun.xiao_name if fun else random.choice(config.fallback.xiao_names or ["小麦"])
    poem = fun.poem if fun else random.choice(config.fallback.poems or ["谢谢大家。"])
    thanks = "、".join(config.fallback.thanks_list or [])
    special = f"再次感谢各位群友的支持🙏尤其要感谢 {thanks} 两位的强力支持⚡🔥！\n" if thanks else "再次感谢各位群友的支持🙏\n"
    return (
        f"所以，{data.date_text}我的净收入为 -{data.total_cost:.4f} 元 📉😵💫。\n\n"
        f"{xiao_name}一路走来，是因为屏幕前各位群友的支持🤝💛才有了不一样的人生🌟。\n"
        f"{poem} 📜✨\n"
        "也正是你们的陪伴，给了我笃定前行的勇气💪🕊️。\n"
        f"{special}"
        "以及所有群员的陪伴❤️ 再次谢谢大家🙇‍♂️🙇‍♀️！"
    )


def _make_forward_node(segment_type: str, content: str) -> dict[str, Any]:
    return {
        "user_id": "0",
        "nickname": "麦麦",
        "segments": [{"type": segment_type, "content": content}],
    }


def _image_node(image: Any) -> dict[str, Any]:
    image_base64 = _extract_image_base64(image)
    if image_base64:
        return _make_forward_node("image", image_base64)
    if isinstance(image, str) and image.lstrip().startswith("<!doctype"):
        return _make_forward_node("text", image)
    return _make_forward_node("text", "图片生成失败，无法展示财报图。")


def _extract_image_base64(image: Any) -> str:
    if isinstance(image, bytes):
        return base64.b64encode(image).decode("utf-8")

    if isinstance(image, dict):
        for key in ("image_base64", "base64", "data", "content"):
            value = image.get(key)
            if isinstance(value, str) and value.strip():
                return _strip_data_url(value)
            if isinstance(value, bytes):
                return base64.b64encode(value).decode("utf-8")
        for key in ("path", "file_path", "filename"):
            value = image.get(key)
            if isinstance(value, str) and value.strip():
                encoded = _base64_from_file(value)
                if encoded:
                    return encoded
        return ""

    if isinstance(image, str):
        value = image.strip()
        if not value or value.startswith("<!doctype"):
            return ""
        if value.startswith("data:image/"):
            return _strip_data_url(value)
        encoded = _base64_from_file(value)
        if encoded:
            return encoded
        if _looks_like_base64(value):
            return value
    return ""


def _base64_from_file(value: str) -> str:
    try:
        path = Path(value)
        if path.exists() and path.is_file():
            return base64.b64encode(path.read_bytes()).decode("utf-8")
    except Exception:
        return ""
    return ""


def _strip_data_url(value: str) -> str:
    if "," in value and value.lstrip().startswith("data:image/"):
        return value.split(",", 1)[1].strip()
    return value.strip()


def _looks_like_base64(value: str) -> bool:
    clean = value.strip()
    if len(clean) < 64:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r")
    return all(char in allowed for char in clean)


def _forward_nodes_plain_text(nodes: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for node in nodes:
        for segment in node.get("segments", []):
            if segment.get("type") == "text":
                content = str(segment.get("content") or "").strip()
                if content:
                    parts.append(content)
            elif segment.get("type") == "image":
                parts.append("[图片]")
    return "\n\n".join(parts)


async def _send_forward(
    ctx: Any,
    nodes: list[dict[str, Any]],
    stream_id: Optional[str] = None,
) -> bool:
    sender = _get_path(ctx, "send")
    forward = getattr(sender, "forward", None)
    target_stream_id = stream_id or _get_stream_id(ctx)
    if not callable(forward):
        text = _forward_nodes_plain_text(nodes)
        text_method = getattr(sender, "text", None) or getattr(sender, "message", None)
        if callable(text_method):
            try:
                sent = await _maybe_await(text_method(text, target_stream_id))
            except TypeError:
                sent = await _maybe_await(text_method(text))
            return bool(sent)
        _log(ctx, "error", "当前上下文不支持发送转发消息")
        return False

    if not target_stream_id:
        _log(ctx, "error", "发送财报失败: 缺少 stream_id")
        return False

    plain_text = _forward_nodes_plain_text(nodes) or "[今日模型调用财报]"
    call_specs = (
        ((nodes, target_stream_id), {"storage_message": False, "processed_plain_text": plain_text}),
        ((nodes, target_stream_id), {}),
        ((), {"messages": nodes, "stream_id": target_stream_id, "storage_message": False, "processed_plain_text": plain_text}),
        ((), {"messages": nodes, "stream_id": target_stream_id}),
    )
    for args, kwargs in call_specs:
        try:
            sent = await _maybe_await(forward(*args, **kwargs))
            if not sent:
                _log(ctx, "error", "发送财报合并转发失败: send.forward 返回 False")
            return bool(sent)
        except TypeError:
            continue
        except Exception as exc:
            _log(ctx, "error", f"发送财报合并转发失败: {exc}")
            return False
    _log(ctx, "error", "发送财报合并转发失败: send.forward 参数不兼容")
    return False


async def _send_plain_messages(
    ctx: Any,
    nodes: list[dict[str, Any]],
    stream_id: Optional[str] = None,
) -> bool:
    sender = _get_path(ctx, "send")
    target_stream_id = stream_id or _get_stream_id(ctx)
    if not target_stream_id:
        _log(ctx, "error", "发送财报失败: 缺少 stream_id")
        return False

    sent_any = False
    for node in nodes:
        for segment in node.get("segments", []):
            segment_type = segment.get("type")
            content = str(segment.get("content") or "")
            if segment_type == "text":
                sent_any = await _send_text_segment(sender, content, target_stream_id) or sent_any
            elif segment_type == "image":
                sent_any = await _send_image_segment(sender, content, target_stream_id) or sent_any
    return sent_any


async def _send_text_segment(sender: Any, content: str, stream_id: str) -> bool:
    text_method = getattr(sender, "text", None) or getattr(sender, "message", None)
    if not callable(text_method):
        return False
    for args, kwargs in (
        ((content, stream_id), {}),
        ((content,), {"stream_id": stream_id}),
        ((content,), {}),
    ):
        try:
            sent = await _maybe_await(text_method(*args, **kwargs))
            return bool(sent)
        except TypeError:
            continue
    return False


async def _send_command_response(
    ctx: Any,
    content: str,
    stream_id: Optional[str] = None,
) -> bool:
    target_stream_id = stream_id or _get_stream_id(ctx)
    if not target_stream_id:
        _log(ctx, "warning", f"发送命令回应失败: 缺少 stream_id，回应内容: {content}")
        return False
    sender = _get_path(ctx, "send")
    sent = await _send_text_segment(sender, content, target_stream_id)
    if not sent:
        _log(ctx, "warning", f"发送命令回应失败: {content}")
    return sent


async def _send_image_segment(sender: Any, content: str, stream_id: str) -> bool:
    image_method = getattr(sender, "image", None)
    if not callable(image_method):
        return await _send_text_segment(sender, "[图片]", stream_id)
    for args, kwargs in (
        ((content, stream_id), {}),
        ((content,), {"stream_id": stream_id}),
        ((), {"image_base64": content, "stream_id": stream_id}),
        ((), {"base64": content, "stream_id": stream_id}),
        ((), {"content": content, "stream_id": stream_id}),
        ((content,), {}),
    ):
        try:
            sent = await _maybe_await(image_method(*args, **kwargs))
            return bool(sent)
        except TypeError:
            continue
    return False


# async def _try_send_audio(
#     ctx: Any,
#     file_location: str,
#     stream_id: Optional[str] = None,
# ) -> None:
#     sender = _get_path(ctx, "send")
#     audio = getattr(sender, "audio", None) or getattr(sender, "voice", None)
#     if callable(audio):
#         target_stream_id = stream_id or _get_stream_id(ctx)
#         try:
#             await _maybe_await(audio(file_location, stream_id=target_stream_id))
#         except TypeError:
#             await _maybe_await(audio(file_location))


async def _resolve_target_context(ctx: Any, target: str) -> Any:
    chat = _get_path(ctx, "chat")
    for method_name in (
        "get_stream",
        "get_context",
        "get_group_stream_by_group_id",
        "get_private_stream_by_user_id",
    ):
        method = getattr(chat, method_name, None)
        if callable(method):
            try:
                return await _maybe_await(method(target))
            except Exception:
                continue
    return None


def _can_query(
    config: ExpensesSummaryConfig,
    ctx: Any = None,
    message: Any = None,
    kwargs: Optional[dict[str, Any]] = None,
) -> bool:
    if not config.permission.query_admin_only:
        return True
    return _is_admin(config, ctx, message, kwargs)


def _is_admin(
    config: ExpensesSummaryConfig,
    ctx: Any = None,
    message: Any = None,
    kwargs: Optional[dict[str, Any]] = None,
) -> bool:
    user_id = _get_user_id(ctx, message, kwargs)
    admins = {str(item).strip() for item in (config.permission.admins or []) if str(item).strip()}
    return bool(user_id and user_id in admins)


def _get_user_id(
    ctx: Any = None,
    message: Any = None,
    kwargs: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    for obj in (message, kwargs, ctx):
        for path in (
            "user_id",
            "sender.user_id",
            "sender.id",
            "sender.qq",
            "sender_id",
            "from_user_id",
            "operator_id",
            "user_info.user_id",
            "user_info.qq",
            "message_info.user_id",
            "message_info.sender_id",
            "message_info.platform_user_id",
            "message_info.sender.user_id",
            "message_info.user_info.user_id",
            "event.user_id",
            "event.sender.user_id",
            "event.message.user_id",
            "event.message.sender.user_id",
            "event.message.message_info.user_id",
        ):
            value = _get_path(obj, path)
            if value:
                return str(value)
    return None


def _extract_mode_argument(message: Any, kwargs: dict[str, Any]) -> str:
    for key in ("mode", "arg", "args", "text", "content"):
        value = kwargs.get(key)
        if isinstance(value, str) and value.strip():
            return _last_command_part(value)

    for path in ("matched_groups.mode", "groups.mode", "message.content", "content", "text", "raw_message"):
        value = _get_path(kwargs, path) or _get_path(message, path)
        if isinstance(value, str) and value.strip():
            return _last_command_part(value)

    return ""


def _last_command_part(text: str) -> str:
    parts = text.strip().split()
    if len(parts) <= 1:
        return "" if text.strip().startswith("/") else text.strip()
    return parts[-1]


def _is_valid_mode_text(mode: str) -> bool:
    normalized = (mode or "").strip().lower()
    return normalized in {
        "默认",
        "default",
        "normal",
        "麦晨风",
        "maichenfeng",
        "mai-chenfeng",
        "huchenfeng",
        "fun",
    }


def _normalize_mode(mode: str) -> str:
    normalized = (mode or "").strip().lower()
    if normalized in {"麦晨风", "maichenfeng", "mai-chenfeng", "huchenfeng", "fun"}:
        return REPORT_MODE_MAICHENFENG
    return REPORT_MODE_DEFAULT


def _seconds_until(time_text: str) -> float:
    now = datetime.now()
    try:
        hour, minute = [int(part) for part in time_text.split(":", 1)]
    except ValueError:
        hour, minute = 23, 30
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max((target - now).total_seconds(), 1)


def _iter_items(raw: Any) -> Iterable[Any]:
    raw = _unwrap_payload(raw)
    if raw is None:
        return []
    if isinstance(raw, dict):
        for key in ("models", "data", "items", "records", "result"):
            if isinstance(raw.get(key), list):
                return raw[key]
        return [dict({"name": key}, **value) if isinstance(value, dict) else {"name": key, "value": value}
                for key, value in raw.items()]
    if isinstance(raw, list):
        return raw
    return [raw]


def _extract_total(raw: Any, keys: tuple[str, ...]) -> float:
    series_total = _series_total(raw)
    if series_total:
        return series_total
    total = 0.0
    for item in _iter_items(raw):
        total += _pick_number(item, *keys)
    return total


def _series_total(raw: Any, today_only: bool = False) -> float:
    raw = _unwrap_payload(raw)
    if raw is None:
        return 0.0
    timestamps = _pick(raw, "timestamps", "time_labels", "labels")
    direct_total = _pick_number(raw, "total")
    if direct_total and not today_only:
        return direct_total
    values_by_key = _pick(raw, "values_by_key", "series", "data_by_key")
    if isinstance(values_by_key, dict):
        return sum(
            _sum_numeric_sequence(values, timestamps=timestamps, today_only=today_only)
            for values in values_by_key.values()
        )
    values = _pick(raw, "values", "data")
    if isinstance(values, (list, tuple)):
        return _sum_numeric_sequence(values, timestamps=timestamps, today_only=today_only)
    return 0.0


def _series_values_by_label(raw: Any, today_only: bool = False) -> dict[str, float]:
    raw = _unwrap_payload(raw)
    values_by_key = _pick(raw, "values_by_key", "series", "data_by_key")
    if not isinstance(values_by_key, dict):
        return {}
    labels_by_key = _pick(raw, "labels_by_key", "label_by_key", "names_by_key") or {}
    timestamps = _pick(raw, "timestamps", "time_labels", "labels")
    result: dict[str, float] = {}
    for key, values in values_by_key.items():
        label = str(
            labels_by_key.get(key)
            if isinstance(labels_by_key, dict) and labels_by_key.get(key)
            else key
        )
        result[label] = _sum_numeric_sequence(values, timestamps=timestamps, today_only=today_only)
    return result


def _sum_numeric_sequence(
    values: Any,
    timestamps: Any = None,
    today_only: bool = False,
) -> float:
    if isinstance(values, dict):
        total = 0.0
        for timestamp, item in values.items():
            if today_only and not _is_today_timestamp(timestamp):
                continue
            if isinstance(item, (int, float)):
                total += float(item)
            else:
                total += _pick_number(item, "value", "count", "total", "cost")
        return total
    if isinstance(values, (list, tuple)):
        total = 0.0
        timestamp_list = timestamps if isinstance(timestamps, (list, tuple)) else None
        if today_only and timestamp_list is None:
            return 0.0
        for index, item in enumerate(values):
            if today_only and timestamp_list is not None and not _is_today_timestamp(timestamp_list[index] if index < len(timestamp_list) else None):
                continue
            if isinstance(item, (int, float)):
                total += float(item)
            else:
                total += _pick_number(item, "value", "count", "total", "cost")
        return total
    if today_only and timestamps is not None and not _is_today_timestamp(timestamps):
        return 0.0
    try:
        return float(values or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_today_timestamp(value: Any) -> bool:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return False
    return parsed.date() == date.today()


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _pick(item: Any, *keys: str, default: Any = None) -> Any:
    item = _unwrap_payload(item)
    for key in keys:
        if isinstance(item, dict) and key in item:
            return item[key]
        if hasattr(item, key):
            return getattr(item, key)
    return default


def _unwrap_payload(raw: Any) -> Any:
    current = raw
    seen = 0
    while isinstance(current, dict) and seen < 4:
        seen += 1
        if any(key in current for key in (
            "values_by_key",
            "items",
            "models",
            "records",
            "timestamps",
            "total",
        )):
            return current
        for key in ("data", "result", "payload"):
            value = current.get(key)
            if isinstance(value, (dict, list)):
                current = value
                break
        else:
            return current
    return current


def _pick_number(item: Any, *keys: str) -> float:
    value = _pick(item, *keys, default=0)
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _call_first(target: Any, names: list[str], *args: Any, **kwargs: Any) -> Any:
    for name in names:
        method = getattr(target, name, None)
        if callable(method):
            try:
                return method(*args, **kwargs)
            except TypeError:
                return method()
    return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _get_stream_id(obj: Any) -> Optional[str]:
    for path in (
        "stream_id",
        "session_id",
        "chat_id",
        "chat_stream.stream_id",
        "message.stream_id",
        "message.session_id",
        "message.chat_id",
        "message.chat_stream.stream_id",
        "message.message_info.stream_id",
        "message.message_info.session_id",
        "message.message_info.chat_id",
        "event.stream_id",
        "event.session_id",
        "event.chat_id",
        "event.chat_stream.stream_id",
        "event.message.stream_id",
        "event.message.session_id",
        "event.message.chat_id",
        "event.message.chat_stream.stream_id",
        "event.message.message_info.stream_id",
        "event.message.message_info.session_id",
        "event.message.message_info.chat_id",
        "context.stream_id",
        "context.session_id",
        "context.chat_id",
        "ctx.stream_id",
        "ctx.session_id",
        "ctx.chat_id",
    ):
        value = _get_path(obj, path)
        if value:
            return str(value)
    return None


def _get_path(obj: Any, path: str) -> Any:
    current = obj
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
        if current is None:
            return None
    return current


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _log(ctx: Any, level: str, message: str) -> None:
    logger = getattr(ctx, "logger", None)
    method = getattr(logger, level, None)
    if callable(method):
        method(message)


def create_plugin() -> ExpensesSummaryPlugin:
    return ExpensesSummaryPlugin()
