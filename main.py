import re
import time
import asyncio
import contextvars
import ipaddress
import math
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote, urljoin, urlparse

from PIL import Image
import aiohttp

from astrbot.api import star, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, AtAll, Plain
from astrbot.api.star import StarTools

Image.MAX_IMAGE_PIXELS = 10_000_000

# 平台接口返回的具体失败提示（如飞书缺通讯录权限）。
# 用 contextvars 而不是实例属性：同一插件实例会并发处理多个群/多个平台的请求，
# 实例级变量会让提示互相清空或串味（PR #18 审查：shared-mutable-state-race）。
_AVATAR_HINT: contextvars.ContextVar = contextvars.ContextVar(
    "touch_head_avatar_hint", default=""
)


class TouchHeadPlugin(star.Star):
    """摸头杀插件主类。"""

    # 头像下载最多跟随几跳重定向，以及哪些状态码算重定向。
    # 关闭 aiohttp 自动跟随，改成逐跳重新过白名单（PR #18 审查：redirect-ssrf-bypass）。
    _MAX_AVATAR_REDIRECTS = 3
    _REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

    def __init__(self, context: star.Context, config: Optional[dict] = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        if config is None:
            logger.warning("AstrBot 未注入摸头杀插件配置，将使用默认速度 1.0；请检查 AstrBot 版本及插件配置。")
        logger.info("摸头杀插件正在初始化...")

        self.data_dir = StarTools.get_data_dir()
        self.output_dir = self.data_dir / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 模板帧实际存放在 data/petpet/frame*.png（随仓库一起发布）。
        self.assets_dir = Path(__file__).parent / "data" / "petpet"
        if not self.assets_dir.is_dir():
            logger.error(
                f"模板资源目录缺失: {self.assets_dir}，摸头杀功能将不可用；"
                "请确认插件包完整（需包含 data/petpet/frame*.png）"
            )

        self._cleanup_task: Optional[asyncio.Task] = None
        self._is_terminating = False
        self._session: Optional[aiohttp.ClientSession] = None

        logger.info("摸头杀插件初始化完成。")

    async def on_astrbot_loaded(self):
        logger.info("摸头杀插件已加载，启动后台清理任务...")
        self._cleanup_task = asyncio.create_task(self._cleanup_old_gifs())

    async def terminate(self):
        logger.info("摸头杀插件正在终止...")
        self._is_terminating = True

        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                logger.info("后台清理任务已取消。")
            except Exception as e:
                logger.error(f"取消清理任务时出错: {e}")

        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("aiohttp session已关闭。")

        logger.info("摸头杀插件已终止。")

    # --- 发送者解析（独立、可测试） ---

    def _resolve_sender(self, event: AstrMessageEvent) -> Optional[Tuple[str, str]]:
        """
        解析发送者的 (user_id, nickname)。
        优先使用通用接口 get_sender_id() / get_sender_name()，
        失败时回退到 message_obj.sender。
        """
        user_id: Optional[str] = None
        nickname: str = ""

        # 1) 通用接口
        try:
            uid = event.get_sender_id()
            if uid:
                user_id = str(uid)
            nickname = event.get_sender_name() or ""
        except Exception as e:
            logger.debug(f"get_sender_id/get_sender_name 不可用: {e}")

        # 2) 回退 message_obj.sender
        if not user_id:
            try:
                sender = getattr(event.message_obj, "sender", None)
                if sender is not None:
                    uid = getattr(sender, "user_id", None)
                    if uid:
                        user_id = str(uid)
                    if not nickname:
                        nickname = getattr(sender, "nickname", "") or ""
            except Exception as e:
                logger.debug(f"从 message_obj.sender 回退解析失败: {e}")

        # 3) 最终校验
        if not user_id:
            logger.error(
                "无法解析发送者 user_id，该平台适配器可能未提供发送者信息。"
                f"event type={type(event).__name__}"
            )
            return None

        return user_id, nickname

    # --- 平台识别（issue #16 多平台适配） ---

    def _platform_name(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_platform_name() or "").strip().lower()
        except Exception:
            return ""

    def _platform_kind(self, event: AstrMessageEvent) -> str:
        """把 AstrBot 平台名归一为头像获取策略：qq/telegram/discord/slack/
        feishu/weixin/unknown。用子串匹配兼容各版本的命名差异。

        平台名为空时返回 "unknown"（不代表 QQ）：AstrBot 里并非所有适配器都
        实现 get_platform_name，早期版本把"拿不到平台名"当成 QQ，会让非 QQ
        适配器误走 QQ 号校验并报"无法识别被 @ 用户的 QQ 号"
        （PR #18 审查：platform-detection-fallback）。真正的兼容判断在
        _effective_kind / _resolve_mentioned_user 里按 @ 标识形态做。
        """
        name = self._platform_name(event)
        if not name:
            return "unknown"
        if "qq" in name or "aiocqhttp" in name:
            return "qq"
        if "telegram" in name or name == "tg":
            return "telegram"
        if "discord" in name:
            return "discord"
        if "slack" in name:
            return "slack"
        if "lark" in name or "feishu" in name:
            return "feishu"
        if "weixin" in name or "wechat" in name or name.startswith("wx"):
            return "weixin"
        return "unknown"

    def _effective_kind(self, event: AstrMessageEvent, target_id: str = "") -> str:
        """实际使用的平台策略：@ 解析与头像解析共用同一份判定。

        - 适配器有明确平台名 → 按名字走（不认识就是 unknown，不猜）；
        - 适配器不上报平台名 → 以本次最终目标标识为准：纯数字按 QQ，其余按
          平台用户标识；没有目标（分析消息里的 @ 之前）才退回看 @ 标识形态。
        - 只有平台名缺失时才会用标识形态兜底，所以具名平台不会因为发送者 ID
          是数字被误判成 QQ（PR #18 审查：platform-detection-fallback）。
        """
        kind = self._platform_kind(event)
        if kind != "unknown":
            return kind
        if self._platform_name(event):
            return "unknown"
        # 目标标识优先：同一事件里带不带 @、@ 了几个，都不影响最终目标的判定，
        # 避免"有 @ 时按 QQ、没有 @ 时不是 QQ"这种同一事件两种策略
        # （PR #18 审查：target-id-ignored-in-platform-fallback）。
        if target_id:
            return (
                "qq"
                if re.fullmatch(r"[0-9]+", str(target_id).strip())
                else "unknown"
            )
        at_ids = [
            str(component.qq).strip()
            for component in event.get_messages()
            if isinstance(component, At)
        ]
        if at_ids and all(re.fullmatch(r"[0-9]+", value) for value in at_ids):
            return "qq"
        return "unknown"

    def _platform_config(self, key: str) -> str:
        """读取多平台凭据配置（platform_tokens 段），未配置返回空串。"""
        try:
            section = self.config.get("platform_tokens") or {}
            return str(section.get(key, "") or "").strip()
        except AttributeError:
            return ""
        except Exception as e:
            logger.debug(f"读取多平台凭据 {key} 失败: {e}")
            return ""

    @staticmethod
    def _normalize_user_key(value) -> str:
        """把平台用户标识归一化后再比较。

        同一用户在「机器人自身标识」与「At 组件字段」两处的表示形态经常不同：
        Telegram 可能是 ``@My_Bot`` / ``my_bot`` / ``MyBot``，飞书是 ``ou_xxx``，
        这里去掉 @ 前缀、所有空白与下划线/连字符等分隔符并转小写，
        避免因表示形态差异把机器人自己当成被摸对象
        （PR #18 审查：self-mention-detection-gap）。
        """
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = re.sub(r"\s+", "", text).lstrip("@")
        return re.sub(r"[-_.:]", "", text)

    def _is_same_user(self, left, right) -> bool:
        left_key = self._normalize_user_key(left)
        right_key = self._normalize_user_key(right)
        return bool(left_key) and left_key == right_key

    @staticmethod
    async def _json_or_none(resp, action: str):
        """读取接口返回的 JSON；网关错误页 / HTML 等非 JSON 响应不抛异常。

        各平台接口在 5xx、代理拦截、限流页下会返回 HTML，直接
        ``await resp.json()`` 会抛 ContentTypeError 并冒泡成笼统的
        “无法获取头像”（PR #18 审查：unhandled-non-json-response）。
        """
        try:
            return await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"{action} 返回非 JSON 响应，已按失败处理: {e}")
            return None

    @staticmethod
    def _redact_secrets(text) -> str:
        """日志脱敏：抹掉 URL / 异常文本中的 Bot Token 等凭据。"""
        if text is None:
            return ""
        result = str(text)
        for pattern, repl in (
            (r"(bot)[0-9]+:[A-Za-z0-9_-]+", r"\1***"),
            (r"([?&](?:access_token|token)=)[^&\s'\"<>]+", r"\1***"),
            (r"(xox[abprs]-)[A-Za-z0-9-]+", r"\1***"),
        ):
            result = re.sub(pattern, repl, result, flags=re.IGNORECASE)
        return result

    # 各平台头像 CDN 的白名单（子域匹配）。平台接口返回的 URL 会直接交给
    # _download_image 下载，必须限定域名，避免被指向内网/本机
    # （PR #18 审查：incomplete-ssrf-protection）。
    # 只放必需域名：飞书/Lark 的头像走 feishucdn/lark 系列 CDN 且带签名参数，
    # 因此按后缀放行而不是写死完整域名，兼容各区域节点。
    _AVATAR_HOST_SUFFIXES = {
        "qq": ("qlogo.cn", "qpic.cn"),
        "telegram": ("api.telegram.org", "cdn.telegram.org"),
        "discord": ("discordapp.com", "discordapp.net", "discord.com"),
        # Slack 只放自家 CDN；头像走 Gravatar 时也只允许 secure.gravatar.com，
        # 该域下不允许任意用户子域（PR #18 审查：allowlist-too-broad）。
        "slack": (
            "slack.com",
            "slack-edge.com",
            "slack-imgs.com",
            "secure.gravatar.com",
        ),
        "feishu": (
            "feishu.cn",
            "feishucdn.com",
            "larksuite.com",
            "larkoffice.com",
            "larksuitecdn.com",
        ),
    }

    @classmethod
    def _host_allowed(cls, host: str, kind: str) -> bool:
        suffixes = cls._AVATAR_HOST_SUFFIXES.get(kind)
        if not suffixes:
            # 未知/未识别的平台策略：不放行任何域名。_resolve_avatar_url 对这类
            # 平台本就不返回地址，这里拒绝可避免"任一已知 CDN 都能被跨平台使用"
            # 的弱化白名单（PR #18 审查：allowlist-too-broad）。
            return False
        return any(host == s or host.endswith("." + s) for s in suffixes)

    @classmethod
    def _sanitize_avatar_url(cls, url, kind: str = "") -> Optional[str]:
        """校验平台返回的头像地址：协议、凭据、域名白名单、受限 IP。

        Slack、飞书、Telegram 等接口返回的 URL 会直接交给 `_download_image`
        下载，必须挡住 127.0.0.1、169.254.x、内网段以及任意第三方域名——仅比对
        IP 字面量不够（域名可以指向内网），因此按平台 CDN 白名单放行
        （PR #18 审查：incomplete-ssrf-protection）。
        """
        if not isinstance(url, str) or not url.strip():
            return None
        raw = url.strip()
        try:
            parsed = urlparse(raw)
        except Exception:
            logger.warning("头像地址解析失败，已忽略")
            return None
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            logger.warning(f"头像地址协议不受支持，已忽略: {parsed.scheme or '(空)'}")
            return None
        if parsed.username or parsed.password:
            logger.warning("头像地址包含用户凭据信息，已忽略")
            return None
        host = parsed.hostname.strip("[]").strip().lower().rstrip(".")
        if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
            logger.warning(f"头像地址指向本机/内网主机名，已忽略: {host}")
            return None
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            logger.warning(f"头像地址指向受限 IP 段，已忽略: {ip}")
            return None
        if not cls._host_allowed(host, kind):
            logger.warning(
                f"头像地址域名不在 {kind or '已知'} 平台白名单内，已忽略: {host}"
            )
            return None
        return raw

    def _resolve_mentioned_user(
        self, event: AstrMessageEvent
    ) -> Optional[Tuple[str, str]]:
        """返回首个被 @ 用户；无目标返回 None，无效提及抛出 ValueError。"""
        # 平台策略只算一次，@ 解析与头像解析共用，避免两边判定不一致
        is_qq = self._effective_kind(event) == "qq"
        self_id = ""
        # 机器人自身标识可能有多种来源/形态（get_self_id、message_obj.self_id），
        # 全部归一化后一起用于「是否 @ 了机器人自己」的判定。
        self_keys = set()

        def _add_self_key(value):
            key = self._normalize_user_key(value)
            if key:
                self_keys.add(key)

        try:
            raw_self = str(event.get_self_id() or "").strip()
        except Exception as e:
            logger.debug(f"get_self_id 不可用: {e}")
            raw_self = ""
        message_self = ""
        try:
            message_obj = getattr(event, "message_obj", None)
            message_self = str(getattr(message_obj, "self_id", "") or "").strip()
        except Exception as e:
            logger.debug(f"从 message_obj.self_id 解析失败: {e}")
            message_self = ""

        if is_qq:
            # QQ 平台：保持原有纯数字语义（前导零归一）。
            # 两个来源都要计入 self_keys——get_self_id 有可能返回的不是机器人 QQ 号
            # （例如群号），只取第一个合法值会让真正的 self_id 进不了自身判定，
            # 仍可能出现"自己摸自己"（PR #18 审查：incomplete-self-id-resolution）。
            for candidate in (raw_self, message_self):
                normalized = candidate.lstrip("0")
                if re.fullmatch(r"[0-9]+", normalized):
                    self_id = self_id or normalized
                    _add_self_key(normalized)
        else:
            # 非 QQ 平台（telegram/lark/slack 等）：self_id 是平台用户标识，
            # 不要求纯数字（telegram 是 bot 用户名）。
            self_id = raw_self or message_self
            for candidate in (raw_self, message_self):
                _add_self_key(candidate)

        has_command_text = False
        # 非 QQ 平台命令前的 @ 无法确认是不是机器人自身，先记为候选目标：
        # 命令文本之后若没有更明确的 @，就用回它，避免目标被静默丢弃。
        fallback = None
        for component in event.get_messages():
            if isinstance(component, Plain) and component.text.strip():
                has_command_text = True
            if isinstance(component, AtAll):
                raise ValueError("请 @ 一位具体用户，暂不支持 @全体成员。")
            if not isinstance(component, At):
                continue

            target_id = str(component.qq).strip()
            if target_id == "all":
                raise ValueError("请 @ 一位具体用户，暂不支持 @全体成员。")
            if is_qq:
                if not re.fullmatch(r"[0-9]+", target_id) or not target_id.strip("0"):
                    logger.warning(f"无法解析被 @ 用户的 QQ 号: {component.qq!r}")
                    raise ValueError("无法识别被 @ 用户的 QQ 号，请重新 @ 一位用户。")
                target_id = target_id.lstrip("0")
            else:
                # issue #16：各平台 At 的 qq 字段是平台用户标识
                # （telegram 为 @用户名、飞书为 open_id、slack 为成员 ID），
                # 只要求非空，不做 QQ 号校验。
                if not target_id:
                    logger.warning(f"被 @ 用户的平台标识为空: {component.qq!r}")
                    raise ValueError("无法识别被 @ 用户的平台标识，请重新 @ 一位用户。")

            # 命令前用于唤醒机器人的 @ 会保留在消息链中。
            if not has_command_text:
                if not self_id and not self_keys:
                    raise ValueError("无法识别机器人账号，请使用 /摸摸 @用户 明确指定目标。")
                if self._is_self_key(target_id, self_keys):
                    logger.debug(f"命令前的提及 {target_id} 是机器人自身，已跳过")
                    continue
                if not is_qq:
                    # 非 QQ 平台的 At 标识与 get_self_id 可能使用不同表示
                    # （@用户名 vs 数字 ID），无法可靠判定是否为机器人自身；
                    # 为避免把机器人当作被摸对象，命令前的提及不直接采用，
                    # 记为候选目标——后面没有更明确的 @ 时仍会用它。
                    if fallback is None:
                        fallback = (target_id, component.name or target_id)
                    logger.debug(
                        f"命令前的平台提及 {target_id} 记为候选目标（无法确认是否为机器人自身）"
                    )
                    continue
            return target_id, component.name or target_id

        if fallback is not None and self._is_self_key(fallback[0], self_keys):
            # 采用候选目标前再比对一次：命令前唯一那个 @ 若就是机器人自身，
            # 宁可返回“未指定目标”，也不要生成“自己摸自己”的 GIF。
            logger.debug(f"候选目标 {fallback[0]} 判定为机器人自身，已丢弃")
            return None
        return fallback

    def _is_self_key(self, value, self_keys) -> bool:
        """判断某个 @ 标识是否指向机器人自身（按归一化后的多种形态比对）。"""
        key = self._normalize_user_key(value)
        if not key:
            return False
        return key in self_keys

    # --- 核心功能实现 ---

    @filter.command("摸摸")
    async def handle_command(self, event: AstrMessageEvent):
        """处理“摸摸”命令。返回 MessageEventResult 由框架统一发送。"""
        # 1. 解析发送者
        sender = self._resolve_sender(event)
        if sender is None:
            return event.plain_result("无法识别您的用户信息，暂时无法处理摸头杀命令。")

        sender_id, sender_name = sender
        if not sender_name:
            sender_name = str(sender_id)

        # 2. 解析 @ 目标
        try:
            target_user = self._resolve_mentioned_user(event)
        except ValueError as e:
            return event.plain_result(str(e))

        # 3. 确定最终目标
        if target_user:
            target_id, target_name = target_user
            logger.info(f"检测到@用户: {target_name} ({target_id})")
            action_desc = f"{sender_name} 摸了摸 {target_name}"
        else:
            target_name = sender_name
            target_id = sender_id
            action_desc = f"{sender_name} 摸了摸自己"

        logger.info(f"收到摸头杀命令: {action_desc}")

        # 4. 目标确定后统一判定一次平台策略，头像获取与失败提示都用这同一个值，
        #    避免 @ 解析与头像解析各自算出不同平台
        #    （PR #18 审查：platform-kind-inconsistency）。
        kind = self._effective_kind(event, target_id)

        try:
            user_image = await self._get_user_avatar_by_id(event, target_id, kind)
            if user_image is None:
                return event.plain_result(
                    "抱歉，无法获取头像，无法生成摸头杀图片"
                    f"（{self._avatar_unavailable_reason(kind, target_id, consume=True)}）。"
                )

            gif_path = await asyncio.to_thread(
                self._build_petpet_gif, user_image, target_name
            )

            if gif_path and gif_path.exists():
                # 关键修复：使用 event.image_result() 而不是 await event.send_message()
                return event.image_result(str(gif_path))
            if not sorted(self.assets_dir.glob("frame*.png")):
                return event.plain_result(
                    "摸头杀模板资源缺失，请联系管理员补齐 data/petpet/frame*.png。"
                )
            return event.plain_result("生成摸头杀图片失败，请稍后再试。")

        except Exception as e:
            logger.error(f"处理摸头杀命令时发生错误: {e}", exc_info=True)
            return event.plain_result("发生内部错误，无法处理您的请求。")

    # --- 头像获取与 GIF 生成 ---

    async def _get_user_avatar_by_id(
        self, event: AstrMessageEvent, user_id: str, kind: str = ""
    ) -> Optional[Image.Image]:
        # 每次取头像前清空失败提示：contextvars 在任务内是复用的，只在
        # _resolve_avatar_url 里清空会漏掉 unknown 等提前返回的分支，导致上一次
        # 的飞书权限提示残留到本次回复（PR #18 审查：avatar-hint-leaks-across-tasks）。
        _AVATAR_HINT.set("")
        # kind 由调用方（handle_command）算好传进来，保证整个命令只用一份判定；
        # 直接调用本函数时按本次目标标识现算（PR #18 审查：platform-kind-inconsistency）。
        if not kind:
            kind = self._effective_kind(event, user_id)
        try:
            url = await self._resolve_avatar_url(event, kind, user_id)
        except Exception as e:
            logger.error(
                f"解析平台 {kind or '未知'} 用户 {user_id} 的头像地址失败: "
                f"{self._redact_secrets(e)}"
            )
            return None
        if not url:
            logger.warning(
                f"平台 {kind or '未知'} 暂无法获取用户 {user_id} 的头像"
                f"（{self._avatar_unavailable_reason(kind, user_id)}）"
            )
            return None
        # Telegram 的文件下载地址里带有 Bot Token，日志里必须脱敏。
        logger.info(f"下载用户头像（平台 {kind or '未知'}）: {self._redact_secrets(url)}")
        return await self._download_image(url, kind)

    def _avatar_unavailable_reason(
        self, kind: str, target_id: str = "", consume: bool = False
    ) -> str:
        """头像不可用时给管理员的配置指引。

        提示的「读取」与「清空」分开：日志路径只读不消费，构造用户回复时才
        consume=True，否则日志先读一次就把提示清掉，用户回复里反而看不到
        （PR #18 审查：consumed-error-hint）。
        """
        reason = self._avatar_unavailable_base_reason(kind, target_id)
        hint = str(_AVATAR_HINT.get() or "").strip()
        if consume:
            _AVATAR_HINT.set("")
        return f"{reason}；{hint}" if hint else reason

    def _avatar_unavailable_base_reason(self, kind: str, target_id: str = "") -> str:
        if kind == "qq":
            return "QQ 头像接口异常"
        if kind == "telegram":
            if target_id and not re.fullmatch(r"[0-9]+", str(target_id).strip()):
                # 目标不是数字 user_id：这是用法问题，不是配置问题，提示要说清楚。
                # Telegram 的 user_id 只可能是正整数，负数是 chat_id，别把它当成
                # 合法用户标识（PR #18 审查：telegram-user-id-sign）。
                return (
                    "Telegram 目标需为数字 user_id（Bot API 不支持用 @用户名 查询），"
                    "请改用数字 ID @ 目标，或先让对方在本群发言后再指定"
                )
            return "请在插件配置 platform_tokens.telegram_bot_token 填写 Bot Token"
        if kind == "discord":
            return "请在插件配置 platform_tokens.discord_bot_token 填写 Bot Token"
        if kind == "slack":
            return "请在插件配置 platform_tokens.slack_bot_token 填写 Bot Token（需 users:read）"
        if kind == "feishu":
            return "请在插件配置 platform_tokens 里填写 feishu_app_id 与 feishu_app_secret"
        if kind == "weixin":
            return "微信官方平台不提供用户头像，暂无法生成摸头 GIF"
        return "未知平台，未实现头像获取"

    async def _resolve_avatar_url(
        self, event: AstrMessageEvent, kind: str, user_id: str
    ) -> Optional[str]:
        """按平台解析用户头像的公开下载地址；无法获取返回 None（issue #16）。

        所有平台的头像地址统一经 `_sanitize_avatar_url` 出口校验（含 QQ /
        Telegram 这类由本插件拼出来的地址），新增平台时不会漏掉域名约束
        （PR #18 审查：incomplete-ssrf-protection）。
        """
        # 每次解析前清空本次任务的失败提示（contextvars 按任务隔离，互不影响）。
        _AVATAR_HINT.set("")
        if kind == "qq":
            # QQ 号必须是纯数字，避免把任意标识拼进请求参数。
            if not re.fullmatch(r"[0-9]+", str(user_id).strip()):
                logger.warning(f"QQ 目标标识非法，跳过头像获取: {user_id!r}")
                return None
            return self._sanitize_avatar_url(
                f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640", kind
            )
        if kind == "telegram":
            return self._sanitize_avatar_url(
                await self._telegram_avatar_url(event, user_id), kind
            )
        if kind == "discord":
            return self._sanitize_avatar_url(await self._discord_avatar_url(user_id), kind)
        if kind == "slack":
            return self._sanitize_avatar_url(await self._slack_avatar_url(user_id), kind)
        if kind == "feishu":
            return self._sanitize_avatar_url(await self._feishu_avatar_url(user_id), kind)
        return None

    def _require_session(self) -> aiohttp.ClientSession:
        """取共享 HTTP 会话。插件终止后不再新建，避免 terminate 之后出现
        无人关闭的会话（PR #18 审查：resource-leak）。"""
        if getattr(self, "_is_terminating", False):
            raise RuntimeError("插件正在停止，已跳过头像请求")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    @staticmethod
    def _telegram_result(data, status: int, action: str):
        """校验 Telegram Bot API 响应（HTTP 200 且 ok=true），返回 result。

        只记录 error_code / description，不打印原始响应，避免把带 Token 的
        请求信息带进日志（PR #18 审查：missing-response-validation）。
        """
        ok = isinstance(data, dict) and data.get("ok") is True
        if status != 200 or not ok:
            error_code = data.get("error_code") if isinstance(data, dict) else None
            description = data.get("description") if isinstance(data, dict) else None
            logger.warning(
                f"Telegram {action} 失败: status={status} "
                f"error_code={error_code} description={description}"
            )
            return None
        return data.get("result")

    async def _telegram_avatar_url(
        self, event: AstrMessageEvent, user_id: str
    ) -> Optional[str]:
        """Telegram：getUserProfilePhotos → getFile → 公开文件地址。需 Bot Token。

        Telegram Bot API 的 ``user_id`` 只接受数字 ID（int64），At 组件里的
        ``@用户名`` 无法直接使用（PR #18 审查：invalid-api-parameter）。
        """
        token = self._platform_config("telegram_bot_token")
        if not token:
            return None
        base = f"https://api.telegram.org/bot{token}"
        session = self._require_session()
        # At 里通常是 @用户名，先换算成数字 user_id。
        # user_id 只接受正整数：负数是 chat_id，其它平台的用户标识也都不带符号，
        # 统一按无符号数字校验（PR #18 审查：telegram-user-id-sign）。
        if not re.fullmatch(r"[0-9]+", user_id):
            # Telegram Bot API 的 getUserProfilePhotos 只接受数字 user_id（int64），且没有
            # 公开的「@用户名 → user_id」换算接口，直接传 @用户名 会 400。
            # 这里不发无效请求，改为给出可操作的提示（PR #18 审查：invalid-api-parameter）。
            logger.warning(
                f"Telegram 目标 {user_id} 不是数字 user_id，无法解析头像；"
                "请改为 @数字ID，或让对方先在本群发言后用数字 ID 指定"
            )
            return None
        async with session.get(
            f"{base}/getUserProfilePhotos",
            params={"user_id": user_id, "limit": 1},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            status = resp.status
            data = await self._json_or_none(resp, "Telegram getUserProfilePhotos")
        result = self._telegram_result(data, status, "getUserProfilePhotos")
        if result is None:
            return None
        photos = result.get("photos") or []
        if not photos:
            return None
        file_id = photos[0][-1].get("file_id") if photos[0] else None
        if not file_id:
            return None
        async with session.get(
            f"{base}/getFile", params={"file_id": file_id},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            status = resp.status
            data = await self._json_or_none(resp, "Telegram getFile")
        result = self._telegram_result(data, status, "getFile")
        if result is None:
            return None
        file_path = result.get("file_path")
        # file_path 会被拼进下载 URL，先校验形态，避免带出 ../../ 或额外参数。
        if (
            not isinstance(file_path, str)
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", file_path)
            or ".." in file_path
        ):
            logger.warning(f"Telegram 返回的文件路径异常，已忽略: {file_path!r}")
            return None
        return f"https://api.telegram.org/file/bot{token}/{file_path}"

    async def _discord_avatar_url(self, user_id: str) -> Optional[str]:
        """Discord：GET /users/{id}（需 Bot Token）→ cdn 头像；无自定义头像
        时回退默认头像（按 user_id 计算 discriminator index）。"""
        token = self._platform_config("discord_bot_token")
        if not token:
            return None
        # Discord 用户标识是数字 snowflake，非法标识不拼进请求路径。
        if not re.fullmatch(r"[0-9]{5,25}", str(user_id)):
            logger.warning(f"Discord 用户标识格式非法，跳过头像获取: {user_id!r}")
            return None
        session = self._require_session()
        async with session.get(
            f"https://discord.com/api/v10/users/{quote(str(user_id), safe='')}",
            headers={"Authorization": f"Bot {token}"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            status = resp.status
            data = await self._json_or_none(resp, "Discord 用户查询")
        # 只有拿到带 id 的用户对象才继续，错误响应不能当成用户处理。
        if status != 200 or not isinstance(data, dict) or not data.get("id"):
            code = data.get("code") if isinstance(data, dict) else None
            logger.warning(f"Discord 用户查询失败: status={status} code={code}")
            return None
        uid = str(data["id"])
        avatar = data.get("avatar")
        if avatar:
            ext = ".gif" if str(avatar).startswith("a_") else ".png"
            return f"https://cdn.discordapp.com/avatars/{uid}/{quote(str(avatar), safe='')}{ext}?size=256"
        # 默认头像索引按 Discord 官方规则分两套（2023-05-03 unique usernames 变更日志）：
        # 已迁移到新用户名体系的用户（discriminator 为 "0"）用 (user_id >> 22) % 6；
        # 旧体系用户仍用 discriminator % 5。只按前者算会让旧账号（以及仍留在旧
        # 体系的 bot 账号）拿到与实际不一致的默认头像
        # （PR #18 审查：discord-default-avatar-index）。
        discriminator = str(data.get("discriminator") or "0").strip()
        index = 0
        try:
            if discriminator.isdigit() and discriminator != "0":
                index = int(discriminator) % 5
            else:
                index = (int(uid) >> 22) % 6
        except ValueError:
            index = 0
        return f"https://cdn.discordapp.com/embed/avatars/{index}.png"

    async def _slack_avatar_url(self, user_id: str) -> Optional[str]:
        """Slack：users.info（需 Bot Token，scopes: users:read）→
        profile 里的 image_* 公开 CDN 地址。"""
        token = self._platform_config("slack_bot_token")
        if not token:
            return None
        session = self._require_session()
        async with session.post(
            "https://slack.com/api/users.info",
            headers={"Authorization": f"Bearer {token}"},
            data={"user": user_id},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            status = resp.status
            data = await self._json_or_none(resp, "Slack users.info")
        # Slack 用 HTTP 200 + ok=false 表达失败（无效 Token / 缺 users:read）。
        if status != 200 or not isinstance(data, dict) or data.get("ok") is not True:
            error = data.get("error") if isinstance(data, dict) else None
            logger.warning(f"Slack users.info 失败: status={status} error={error}")
            return None
        profile = ((data or {}).get("user") or {}).get("profile") or {}
        for key in ("image_192", "image_512", "image_240", "image_original"):
            url = self._sanitize_avatar_url(profile.get(key), "slack")
            if url:
                return url
        return None

    async def _feishu_avatar_url(self, user_id: str) -> Optional[str]:
        """飞书：tenant_access_token（需 app_id/app_secret）→
        contact/v3/users/{open_id} → avatar 公开地址。"""
        app_id = self._platform_config("feishu_app_id")
        app_secret = self._platform_config("feishu_app_secret")
        if not app_id or not app_secret:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", str(user_id)):
            logger.warning(f"飞书用户标识格式非法，跳过头像获取: {user_id!r}")
            return None
        session = self._require_session()
        async with session.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await self._json_or_none(resp, "飞书 tenant_access_token")
        token = (data or {}).get("tenant_access_token")
        if not token:
            logger.warning(f"飞书 tenant_access_token 获取失败: code={((data or {}).get('code'))}")
            return None
        async with session.get(
            "https://open.feishu.cn/open-apis/contact/v3/users/"
            + quote(str(user_id), safe=""),
            params={"user_id_type": "open_id"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            status = resp.status
            data = await self._json_or_none(resp, "飞书用户查询")
        if status != 200 or not isinstance(data, dict) or data.get("code") not in (0, None):
            code = (data or {}).get("code") if isinstance(data, dict) else None
            logger.warning(f"飞书用户查询失败: status={status} code={code}")
            if status == 403 or code in (403, 41050, 99991672, 99991679):
                # 自建应用默认没有通讯录权限，返回 403/41050 时提示要开通权限，
                # 否则用户只会看到笼统的「去填 app_id/app_secret」
                # （PR #18 审查：feishu-permission-hint）。
                _AVATAR_HINT.set(
                    "飞书应用缺少通讯录权限：请在开放平台开通 "
                    "contact:user.base:readonly 等权限并发布版本"
                )
            return None
        user = (((data or {}).get("data") or {}).get("user")) or {}
        avatar = user.get("avatar")
        # 飞书不同版本/接口返回的头像形态不一致：可能是 avatar 字典
        # （avatar_240 等分辨率字段），也可能是 avatar 字符串或 avatar_url。
        if isinstance(avatar, str) and avatar.strip():
            return self._sanitize_avatar_url(avatar, "feishu")
        direct_url = user.get("avatar_url")
        if isinstance(direct_url, str) and direct_url.strip():
            return self._sanitize_avatar_url(direct_url, "feishu")
        if isinstance(avatar, dict):
            # 按分辨率从高到低兜底。
            for key in (
                "avatar_origin",
                "avatar_640",
                "avatar_big",
                "avatar_240",
                "avatar_middle",
                "avatar_thumb",
                "avatar_96",
                "avatar_72",
            ):
                url = self._sanitize_avatar_url(avatar.get(key), "feishu")
                if url:
                    return url
        return None

    async def _download_image(self, url: str, kind: str = "") -> Optional[Image.Image]:
        # 真正发起请求的边界上也复核一次：调用方可能漏掉出口校验，这里不信任
        # 传进来的地址（PR #18 审查：missing-url-validation-at-download-boundary）。
        safe_url = self._sanitize_avatar_url(url, kind)
        if not safe_url:
            logger.warning("头像下载地址未通过校验，已放弃下载")
            return None
        try:
            session = self._require_session()

            # 关闭自动重定向并逐跳复核：白名单域若返回 302/307 指向 127.0.0.1、
            # 169.254.169.254 或内网地址，aiohttp 默认会直接跟随，白名单只校验
            # 首跳等于没校验（PR #18 审查：redirect-ssrf-bypass）。
            target = safe_url
            response = None
            for _hop in range(self._MAX_AVATAR_REDIRECTS + 1):
                response = await session.get(
                    target,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                location = response.headers.get("Location")
                if response.status not in self._REDIRECT_STATUSES:
                    break
                response.close()
                next_url = self._sanitize_avatar_url(
                    urljoin(target, str(location or "")), kind
                )
                if not next_url:
                    logger.warning(
                        "头像地址重定向到非白名单目标，已放弃下载："
                        f"{self._redact_secrets(location)}"
                    )
                    return None
                target = next_url
            else:
                logger.warning("头像地址重定向层数过多，已放弃下载")
                return None

            async with response:
                if response.status != 200:
                    logger.error(f"下载图片失败，HTTP状态码: {response.status}")
                    return None

                content_type = response.headers.get("Content-Type", "")
                if "image" not in content_type:
                    logger.warning(f"非图片 Content-Type: {content_type}")
                    return None

                max_size = 5 * 1024 * 1024
                if response.content_length and response.content_length > max_size:
                    logger.warning(f"图片过大: {response.content_length} bytes")
                    return None

                image_data = b""
                async for chunk in response.content.iter_chunked(8192):
                    image_data += chunk
                    if len(image_data) > max_size:
                        logger.warning("下载图片超过大小限制，已中止")
                        return None

                try:
                    with Image.open(BytesIO(image_data)) as img:
                        img.verify()
                except Exception:
                    logger.error("下载图片验证失败")
                    return None

                with Image.open(BytesIO(image_data)) as img:
                    if img.width * img.height > Image.MAX_IMAGE_PIXELS:
                        logger.warning(f"图片像素过大: {img.width}x{img.height}")
                        return None
                    return img.copy()

        except asyncio.TimeoutError:
            logger.error("下载头像超时。")
        except aiohttp.ClientError as e:
            logger.error(f"下载头像网络错误: {self._redact_secrets(e)}")
        except Exception as e:
            logger.error(f"处理下载头像时发生意外错误: {self._redact_secrets(e)}")

        return None

    def _get_frame_duration(self) -> int:
        config = self.config if isinstance(self.config, dict) else {}
        patpat = config.get("patpat")
        speed = patpat.get("speed", 1.0) if isinstance(patpat, dict) else 1.0
        if isinstance(speed, bool) or not isinstance(speed, (int, float)):
            logger.warning(f"无效的摸头速度 {speed!r}，使用默认速度 1.0")
            speed = 1.0
        elif not 0.5 <= speed <= 3.0 or not math.isfinite(speed):
            logger.warning(f"无效的摸头速度 {speed!r}，使用默认速度 1.0")
            speed = 1.0

        # GIF 帧时长以 10 毫秒为单位，提前四舍五入避免编码时直接截断。
        return int(100 / speed / 10 + 0.5) * 10

    def _build_petpet_gif(
        self, user_image: Image.Image, username: str
    ) -> Optional[Path]:
        if self._is_terminating:
            return None

        logger.info(f"开始为用户 {username} 生成 GIF...")

        timestamp = int(time.time() * 1000)
        safe_username = re.sub(r'[/\\<>:"|?*\x00-\x1f]', "_", username)
        safe_username = safe_username[:50]
        output_filename = f"petpet_{safe_username}_{timestamp}.gif"
        output_path = self.output_dir / output_filename

        try:
            avatar_size = (100, 100)
            user_image = user_image.convert("RGBA").resize(
                avatar_size, Image.Resampling.LANCZOS
            )

            frames = []
            frame_files = sorted(self.assets_dir.glob("frame*.png"))

            if not frame_files:
                logger.error(f"在 {self.assets_dir} 中未找到任何模板帧(frame*.png)。")
                return None

            for i, frame_path in enumerate(frame_files):
                with Image.open(frame_path) as hand_frame:
                    hand_frame = hand_frame.convert("RGBA")
                    canvas = Image.new(
                        "RGBA", hand_frame.size, (255, 255, 255, 0)
                    )

                    avatar_x = (canvas.width - avatar_size[0]) // 2
                    avatar_y = (canvas.height - avatar_size[1]) // 2 + 10

                    deformation = self._calculate_deformation(i, len(frame_files))
                    if deformation != 1.0:
                        new_width = int(avatar_size[0] * deformation)
                        avatar_frame = user_image.resize(
                            (new_width, avatar_size[1]), Image.Resampling.LANCZOS
                        )
                        avatar_x = (canvas.width - new_width) // 2
                    else:
                        avatar_frame = user_image

                    canvas.paste(avatar_frame, (avatar_x, avatar_y), avatar_frame)
                    canvas.alpha_composite(hand_frame, dest=(0, -5))
                    frames.append(canvas.convert("RGB"))

            if frames:
                frames[0].save(
                    output_path,
                    save_all=True,
                    append_images=frames[1:],
                    duration=self._get_frame_duration(),
                    loop=0,
                    optimize=True,
                    disposal=2,
                )
                logger.info(f"GIF 已生成并保存到: {output_path}")
                return output_path
            else:
                logger.error("未能生成任何帧。")
                return None

        except Exception as e:
            logger.error(f"生成 GIF 过程中发生错误: {e}", exc_info=True)
            if output_path.exists():
                try:
                    output_path.unlink()
                except Exception:
                    pass
            return None

    def _calculate_deformation(self, frame_index: int, total_frames: int) -> float:
        if total_frames <= 1:
            return 1.0
        progress = frame_index / (total_frames - 1)
        deformation_factor = math.sin(progress * math.pi)
        return 0.85 + 0.15 * (1 - deformation_factor)

    async def _cleanup_old_gifs(self):
        while not self._is_terminating:
            try:
                await asyncio.sleep(3600)
                logger.info("执行 GIF 清理任务...")

                cutoff_time = 24 * 3600
                count = 0
                for gif_file in self.output_dir.glob("*.gif"):
                    try:
                        stat = gif_file.stat()
                        age = time.time() - stat.st_mtime
                        if age > cutoff_time:
                            gif_file.unlink()
                            count += 1
                            logger.debug(f"已清理旧文件: {gif_file.name}")
                    except Exception as e:
                        logger.error(f"清理文件 {gif_file.name} 时出错: {e}")

                if count > 0:
                    logger.info(f"本次清理了 {count} 个旧 GIF 文件。")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"清理任务发生错误: {e}", exc_info=True)