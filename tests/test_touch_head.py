import asyncio
from dataclasses import dataclass
from enum import Enum
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]


class ComponentType(str, Enum):
    At = "At"
    Plain = "Plain"


@dataclass
class At:
    qq: int | str
    name: str | None = ""
    type = ComponentType.At


@dataclass
class AtAll(At):
    qq: str = "all"


@dataclass
class Plain:
    text: str
    type = ComponentType.Plain


class FakeEvent:
    def __init__(self, messages, sender_id="123456", self_id="100001"):
        self.message_obj = SimpleNamespace(
            message=messages,
            sender=SimpleNamespace(user_id=sender_id, nickname="发送者"),
            self_id=self_id,
        )

    def get_messages(self):
        return self.message_obj.message

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_sender_name(self):
        return self.message_obj.sender.nickname

    def get_self_id(self):
        return self.message_obj.self_id

    def get_platform_name(self):
        return "aiocqhttp"

    def plain_result(self, text):
        return "text", text

    def image_result(self, path):
        return "image", path


def load_plugin():
    # Unit tests use AstrBot's public component shape without starting the bot runtime.
    modules = {
        name: ModuleType(name)
        for name in (
            "astrbot",
            "astrbot.api",
            "astrbot.api.event",
            "astrbot.api.message_components",
            "astrbot.api.star",
        )
    }
    star = modules["astrbot.api.star"]
    class Star:
        def __init__(self, context):
            self.context = context

    star.Star = Star
    star.Context = object
    star.StarTools = SimpleNamespace()
    modules["astrbot.api"].star = star
    modules["astrbot.api"].logger = Mock()
    event_api = modules["astrbot.api.event"]
    event_api.AstrMessageEvent = FakeEvent
    event_api.filter = SimpleNamespace(command=lambda name: lambda handler: handler)
    components = modules["astrbot.api.message_components"]
    components.At = At
    components.AtAll = AtAll
    components.Plain = Plain
    spec = importlib.util.spec_from_file_location("touch_head_under_test", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


plugin_module = load_plugin()
TouchHeadPlugin = plugin_module.TouchHeadPlugin


class PluginInitializationTests(unittest.TestCase):
    def test_missing_injected_config_logs_warning_and_uses_default_speed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                plugin_module.StarTools, "get_data_dir", create=True, return_value=Path(temp_dir)
            ), patch.object(plugin_module.logger, "warning") as warning:
                plugin = TouchHeadPlugin(context=object())
            self.assertEqual(plugin.config, {})
            warning.assert_called_once()
            self.assertIn("未注入", warning.call_args.args[0])
            self.assertEqual(plugin._get_frame_duration(), 100)

    def test_astrbot_injected_config_is_used_for_gif_generation(self):
        config = {"patpat": {"speed": 2.0}}
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                plugin_module.StarTools, "get_data_dir", create=True, return_value=Path(temp_dir)
            ):
                plugin = TouchHeadPlugin(context=object(), config=config)
            self.assertIs(plugin.config, config)
            self.assertTrue(plugin.output_dir.is_dir())
            with Image.new("RGBA", (100, 100), "blue") as avatar:
                gif_path = plugin._build_petpet_gif(avatar, "target")
            self.assertIsNotNone(gif_path)
            with Image.open(gif_path) as gif:
                self.assertEqual(gif.n_frames, 5)
                for index in range(gif.n_frames):
                    gif.seek(index)
                    self.assertEqual(gif.info["duration"], 50)


class MentionCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.gif_path = Path(self.temp_dir.name) / "petpet.gif"
        self.gif_path.write_bytes(b"GIF89a")
        self.avatar = Image.new("RGBA", (100, 100), "blue")
        self.addCleanup(self.avatar.close)
        self.plugin = TouchHeadPlugin.__new__(TouchHeadPlugin)
        self.plugin.config = {}
        self.plugin._download_image = AsyncMock(return_value=self.avatar)
        self.plugin._build_petpet_gif = Mock(return_value=self.gif_path)

    async def assert_target(self, messages, user_id, nickname):
        self.plugin._download_image.reset_mock()
        self.plugin._build_petpet_gif.reset_mock()
        result = await self.plugin.handle_command(FakeEvent(messages))
        self.assertEqual(result, ("image", str(self.gif_path)))
        _assert_avatar_downloaded(self, f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640")
        self.plugin._build_petpet_gif.assert_called_once_with(self.avatar, nickname)

    async def test_uses_typed_at_qq_and_name(self):
        for qq in (234567, "234567", " 00234567 "):
            with self.subTest(qq=qq):
                await self.assert_target(
                    [Plain("/摸摸 "), At(qq=qq, name="缘滢.")], "234567", "缘滢."
                )

    async def test_missing_target_name_uses_qq(self):
        for name in (None, ""):
            with self.subTest(name=name):
                await self.assert_target(
                    [Plain("/摸摸"), At(qq="234567", name=name)], "234567", "234567"
                )

    async def test_bot_wakeup_mention_does_not_override_target(self):
        await self.assert_target(
            [At(qq=100001, name="机器人"), Plain(" /摸摸 "), At(qq="234567", name="目标")],
            "234567",
            "目标",
        )

    async def test_explicit_bot_target_after_command_is_selected(self):
        for messages in (
            [Plain("/摸摸 "), At(qq="100001", name="机器人")],
            [At(qq="100001"), Plain(" /摸摸 "), At(qq="100001", name="机器人")],
        ):
            with self.subTest(messages=messages):
                await self.assert_target(messages, "100001", "机器人")

    async def test_no_user_mention_keeps_sender_target(self):
        for messages in (
            [Plain("/摸摸")],
            [At(qq="100001", name="机器人"), Plain(" /摸摸")],
        ):
            with self.subTest(messages=messages):
                await self.assert_target(messages, "123456", "发送者")

    async def test_self_id_getter_failure_uses_message_self_id(self):
        for error in (AttributeError("not available"), RuntimeError("adapter failure")):
            with self.subTest(error=error):
                self.plugin._download_image.reset_mock()
                event = FakeEvent(
                    [At(qq="100001"), Plain(" /摸摸 "), At(qq="234567", name="目标")]
                )
                event.get_self_id = Mock(side_effect=error)
                result = await self.plugin.handle_command(event)
                self.assertEqual(result, ("image", str(self.gif_path)))
                _assert_avatar_downloaded(self, "https://q1.qlogo.cn/g?b=qq&nk=234567&s=640")

    async def test_empty_or_invalid_self_id_uses_message_self_id(self):
        for self_id in (None, "", " ", "not-a-qq", "000"):
            with self.subTest(self_id=self_id):
                self.plugin._download_image.reset_mock()
                event = FakeEvent(
                    [At(qq="100001"), Plain(" /摸摸 "), At(qq="234567", name="目标")]
                )
                event.get_self_id = Mock(return_value=self_id)
                result = await self.plugin.handle_command(event)
                self.assertEqual(result, ("image", str(self.gif_path)))
                _assert_avatar_downloaded(self, "https://q1.qlogo.cn/g?b=qq&nk=234567&s=640")

    async def test_unknown_self_id_rejects_ambiguous_leading_mention(self):
        for self_id in (None, "", "not-a-qq", "0"):
            with self.subTest(self_id=self_id):
                event = FakeEvent([At(qq="100001"), Plain(" /摸摸")], self_id=self_id)
                event.get_self_id = Mock(side_effect=AttributeError("not available"))
                result = await self.plugin.handle_command(event)
                self.assertEqual(result[0], "text")
                self.assertIn("机器人", result[1])
                self.plugin._download_image.assert_not_awaited()
                self.plugin._build_petpet_gif.assert_not_called()

    async def test_unknown_self_id_allows_no_mention_or_explicit_target(self):
        for messages, target_id in (
            ([Plain("/摸摸")], "123456"),
            ([Plain("/摸摸 "), At(qq="234567")], "234567"),
        ):
            with self.subTest(messages=messages):
                self.plugin._download_image.reset_mock()
                event = FakeEvent(messages, self_id=None)
                event.get_self_id = Mock(side_effect=AttributeError("not available"))
                result = await self.plugin.handle_command(event)
                self.assertEqual(result, ("image", str(self.gif_path)))
                _assert_avatar_downloaded(self, f"https://q1.qlogo.cn/g?b=qq&nk={target_id}&s=640")

    async def test_first_user_mention_is_selected(self):
        await self.assert_target(
            [Plain("/摸摸 "), At(qq="234567", name="第一位"), At(qq="345678", name="第二位")],
            "234567",
            "第一位",
        )

    async def test_invalid_mentions_do_not_fall_back_to_sender(self):
        invalid_mentions = [
            At(qq=value)
            for value in ("", None, "all", "0", "000", "not-a-qq", "１２３", "123&x=1")
        ]
        invalid_mentions.append(AtAll())
        for mention in invalid_mentions:
            with self.subTest(mention=mention):
                result = await self.plugin.handle_command(FakeEvent([Plain("/摸摸 "), mention]))
                self.assertEqual(result[0], "text")
                self.assertIn("@", result[1])
                self.plugin._download_image.assert_not_awaited()
                self.plugin._build_petpet_gif.assert_not_called()

    async def test_configured_speed_controls_gif_returned_by_command(self):
        self.plugin.config = {"patpat": {"speed": 2.0}}
        self.plugin.output_dir = Path(self.temp_dir.name)
        self.plugin.assets_dir = ROOT / "data" / "petpet"
        self.plugin._is_terminating = False
        self.plugin._build_petpet_gif = TouchHeadPlugin._build_petpet_gif.__get__(
            self.plugin, TouchHeadPlugin
        )
        result = await self.plugin.handle_command(FakeEvent([Plain("/摸摸")]))
        self.assertEqual(result[0], "image")
        with Image.open(result[1]) as gif:
            self.assertEqual(gif.n_frames, 5)
            for index in range(gif.n_frames):
                gif.seek(index)
                self.assertEqual(gif.info["duration"], 50)

    async def test_target_avatar_failure_does_not_retry_sender(self):
        self.plugin._download_image.return_value = None
        result = await self.plugin.handle_command(FakeEvent([At(qq="234567", name="目标")]))
        self.assertEqual(
            result,
            ("text", "抱歉，无法获取头像，无法生成摸头杀图片（QQ 头像接口异常）。"),
        )
        _assert_avatar_downloaded(self, "https://q1.qlogo.cn/g?b=qq&nk=234567&s=640")
        self.plugin._build_petpet_gif.assert_not_called()


class PlatformEvent(FakeEvent):
    def __init__(self, messages, platform, **kwargs):
        super().__init__(messages, **kwargs)
        self._platform = platform

    def get_platform_name(self):
        return self._platform


class FakeContent:
    def __init__(self, data=b""):
        self._data = data

    async def iter_chunked(self, size):
        for start in range(0, len(self._data), size):
            yield self._data[start:start + size]


class FakeResponse:
    """接口替身：带 headers 与 close()，便于校验重定向与下载阶段行为。"""

    def __init__(self, status=200, payload=None, headers=None, body=b""):
        self.status = status
        self.headers = dict(headers or {})
        self._payload = payload if payload is not None else {}
        self.content = FakeContent(body)
        self.content_length = len(body)
        self.closed = False

    async def json(self, content_type=None):
        return self._payload

    def close(self):
        self.closed = True

    def __await__(self):
        # aiohttp 的 session.get() 既可 await 也可 async with，替身要两种都支持。
        async def _self():
            return self

        return _self().__await__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _assert_avatar_downloaded(testcase, url):
    """断言按给定地址下载头像。

    kind 参数由实现侧决定（用于逐跳重定向复核），这里只校验下载地址本身，
    避免断言把内部签名写死。
    """
    mocked = testcase.plugin._download_image
    mocked.assert_awaited_once()
    testcase.assertEqual(mocked.await_args.args[0], url)


class FakeSession:
    closed = False

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def _next(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        return self._responses.pop(0)

    def get(self, url, **kwargs):
        return self._next("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, kwargs)


class PlatformSupportTests(unittest.IsolatedAsyncioTestCase):
    """issue #16：多平台适配（平台识别 / @ 解析 / 头像解析与提示）。"""

    def setUp(self):
        self.plugin = TouchHeadPlugin.__new__(TouchHeadPlugin)
        self.plugin.config = {}

    def test_platform_kind_mapping(self):
        for platform, kind in (
            ("", "unknown"),  # 平台名缺失：不能默认当 QQ（见 _effective_kind）
            ("aiocqhttp", "qq"),
            ("qq_official", "qq"),
            ("telegram", "telegram"),
            ("tg", "telegram"),
            ("discord", "discord"),
            ("slack", "slack"),
            ("lark", "feishu"),
            ("feishu", "feishu"),
            ("wechat", "weixin"),
            ("weixin_official", "weixin"),
            ("some_new_platform", "unknown"),
        ):
            with self.subTest(platform=platform):
                event = PlatformEvent([Plain("/摸摸")], platform)
                self.assertEqual(self.plugin._platform_kind(event), kind)

    def test_unnamed_platform_falls_back_to_qq_only_for_numeric_at_id(self):
        # 适配器不实现 get_platform_name 时，只看消息里的 @ 标识形态：
        # 全是纯数字才按 QQ 处理，其余按平台标识处理（PR #18 审查）。
        class AnonymousEvent(PlatformEvent):
            def get_platform_name(self):
                raise AttributeError("adapter without get_platform_name")

        for messages, expected in (
            ([Plain("/摸摸 "), At(qq="234567")], "qq"),
            ([Plain("/摸摸 "), At(qq="@user")], "unknown"),
            ([Plain("/摸摸")], "unknown"),  # 没有 @ 目标时不能凭发送者 ID 猜 QQ
        ):
            with self.subTest(expected=expected):
                event = AnonymousEvent(list(messages), "")
                self.assertEqual(self.plugin._effective_kind(event), expected)

        # 非 QQ 适配器不再被套上 QQ 号校验并抛「无法识别 QQ 号」
        event = AnonymousEvent([Plain("/摸摸 "), At(qq="@someone", name="某人")], "")
        self.assertEqual(self.plugin._resolve_mentioned_user(event), ("@someone", "某人"))

    def test_named_unknown_platform_is_not_treated_as_qq(self):
        event = PlatformEvent([Plain("/摸摸 "), At(qq="234567")], "some_new_platform")
        self.assertEqual(self.plugin._effective_kind(event), "unknown")

    def test_non_qq_platform_accepts_non_numeric_at_id(self):
        for platform, target in (
            ("telegram", "@target_user"),
            ("feishu", "ou_8c9d1e2f"),
            ("slack", "U0G9QF9C6"),
            ("discord", "1029384756102938475"),
        ):
            with self.subTest(platform=platform, target=target):
                event = PlatformEvent(
                    [Plain("/摸摸 "), At(qq=target, name="目标")], platform
                )
                resolved = self.plugin._resolve_mentioned_user(event)
                self.assertEqual(resolved, (target, "目标"))

    async def test_avatar_url_dispatches_to_platform_resolver(self):
        event = PlatformEvent([Plain("/摸摸")], "telegram")
        self.plugin._telegram_avatar_url = AsyncMock(return_value="https://api.telegram.org/file/bot1/a.jpg")
        url = await self.plugin._resolve_avatar_url(event, "telegram", "@user")
        self.assertEqual(url, "https://api.telegram.org/file/bot1/a.jpg")
        self.plugin._telegram_avatar_url.assert_awaited_once_with(event, "@user")

    async def test_avatar_url_resolvers_require_credentials(self):
        for kind, user_id in (
            ("telegram", "@user"),
            ("discord", "1029384756102938475"),
            ("slack", "U0G9QF9C6"),
            ("feishu", "ou_8c9d1e2f"),
        ):
            with self.subTest(kind=kind):
                self.plugin.config = {"platform_tokens": {}}
                resolver = getattr(self.plugin, f"_{kind}_avatar_url")
                if kind == "telegram":
                    url = await resolver(PlatformEvent([Plain("/摸摸")], "telegram"), user_id)
                else:
                    url = await resolver(user_id)
                self.assertIsNone(url)

    async def test_unsupported_platform_has_no_avatar_url(self):
        event = PlatformEvent([Plain("/摸摸")], "some_new_platform")
        url = await self.plugin._resolve_avatar_url(event, "unknown", "123")
        self.assertIsNone(url)

    def test_avatar_unavailable_reason_gives_config_hint(self):
        for kind, keyword in (
            ("telegram", "telegram_bot_token"),
            ("discord", "discord_bot_token"),
            ("slack", "slack_bot_token"),
            ("feishu", "feishu_app"),
            ("weixin", "微信官方平台不提供用户头像"),
            ("unknown", "未知平台"),
        ):
            with self.subTest(kind=kind):
                self.assertIn(keyword, self.plugin._avatar_unavailable_reason(kind))

    def test_telegram_reason_distinguishes_username_from_bad_config(self):
        # 目标写成 @用户名 时属于用法问题，提示不能只说“去填 Token”。
        reason = self.plugin._avatar_unavailable_reason("telegram", "@target_user")
        self.assertIn("数字 user_id", reason)
        self.assertNotIn("telegram_bot_token", reason)
        self.assertIn(
            "telegram_bot_token", self.plugin._avatar_unavailable_reason("telegram", "987654")
        )

    async def test_avatar_failure_message_includes_platform_hint(self):
        self.plugin._download_image = AsyncMock(return_value=None)
        event = PlatformEvent([Plain("/摸摸")], "weixin")
        result = await self.plugin.handle_command(event)
        self.assertEqual(result[0], "text")
        self.assertIn("微信官方平台不提供用户头像", result[1])
        self.plugin._download_image.assert_not_awaited()

    def test_redact_secrets_masks_credentials(self):
        url = "https://api.telegram.org/file/bot123456:AAH-secret_token/photo/file_1.jpg"
        redacted = self.plugin._redact_secrets(url)
        self.assertNotIn("AAH-secret_token", redacted)
        self.assertIn("bot***", redacted)
        self.assertIn("file_1.jpg", redacted)
        self.assertNotIn("1234-abcd", self.plugin._redact_secrets("xoxb-1234-abcd"))
        self.assertNotIn(
            "abc", self.plugin._redact_secrets("https://x/y?access_token=abc&z=1")
        )
        self.assertEqual(self.plugin._redact_secrets(None), "")

    def test_user_key_normalization(self):
        self.assertTrue(self.plugin._is_same_user("@MyBot", "mybot"))
        self.assertTrue(self.plugin._is_same_user("My_Bot", "@mybot"))
        self.assertTrue(self.plugin._is_same_user(" ou_8C9D ", "OU_8c9d"))
        self.assertTrue(self.plugin._is_same_user(" 100001 ", "100001"))
        self.assertFalse(self.plugin._is_same_user("", ""))
        self.assertFalse(self.plugin._is_same_user("@alice", "@bob"))

    def test_non_qq_leading_mention_is_kept_as_candidate(self):
        # 命令前的 @ 在非 QQ 平台无法确认是否机器人自身：不直接采用，
        # 但也不丢——后面没有更明确的 @ 时用它。
        for platform in ("telegram", "feishu", "slack", "discord"):
            with self.subTest(platform=platform):
                event = PlatformEvent(
                    [At(qq="@some_user", name="某人"), Plain(" /摸摸")], platform
                )
                self.assertEqual(
                    self.plugin._resolve_mentioned_user(event), ("@some_user", "某人")
                )

    def test_non_qq_later_mention_overrides_leading_candidate(self):
        event = PlatformEvent(
            [At(qq="@some_user", name="某人"), Plain(" /摸摸 "), At(qq="@target", name="目标")],
            "telegram",
        )
        self.assertEqual(
            self.plugin._resolve_mentioned_user(event), ("@target", "目标")
        )

    def test_require_session_refuses_after_terminate(self):
        self.plugin._is_terminating = True
        with self.assertRaises(RuntimeError):
            self.plugin._require_session()

    async def test_telegram_avatar_main_path(self):
        self.plugin.config = {"platform_tokens": {"telegram_bot_token": "123:abc"}}
        self.plugin._session = FakeSession(
            [
                FakeResponse(200, {"ok": True, "result": {"photos": [[{"file_id": "f1"}]]}}),
                FakeResponse(200, {"ok": True, "result": {"file_path": "photos/x.jpg"}}),
            ]
        )
        event = PlatformEvent([Plain("/摸摸")], "telegram")
        url = await self.plugin._telegram_avatar_url(event, "987654")
        self.assertEqual(
            url, "https://api.telegram.org/file/bot123:abc/photos/x.jpg"
        )

    async def test_telegram_rejects_non_numeric_user_id_without_request(self):
        # Telegram Bot API 只接受数字 user_id，没有公开的「@用户名 → id」接口。
        # 这里必须直接放弃，不能发出必然 400 的请求（PR #18 审查）。
        self.plugin.config = {"platform_tokens": {"telegram_bot_token": "123:abc"}}
        self.plugin._session = FakeSession([])
        event = PlatformEvent([Plain("/摸摸")], "telegram")
        self.assertIsNone(await self.plugin._telegram_avatar_url(event, "@target_user"))
        self.assertIsNone(await self.plugin._telegram_avatar_url(event, ""))
        self.assertEqual(self.plugin._session.calls, [])

    async def test_non_json_response_is_handled_gracefully(self):
        # 网关错误页/HTML 会抛 ContentTypeError，应作为失败处理而不是冒泡。
        class HtmlResponse(FakeResponse):
            async def json(self, content_type=None):
                raise ValueError("Attempt to decode JSON with unexpected mimetype")

        self.plugin.config = {"platform_tokens": {"slack_bot_token": "xoxb-1"}}
        self.plugin._session = FakeSession([HtmlResponse(502)])
        self.assertIsNone(await self.plugin._slack_avatar_url("U0G9QF9C6"))

        self.plugin.config = {"platform_tokens": {"telegram_bot_token": "123:abc"}}
        self.plugin._session = FakeSession([HtmlResponse(502)])
        event = PlatformEvent([Plain("/摸摸")], "telegram")
        self.assertIsNone(await self.plugin._telegram_avatar_url(event, "987654"))

    def test_avatar_url_sanitizer_blocks_internal_targets(self):
        ok = self.plugin._sanitize_avatar_url
        self.assertEqual(
            ok("https://avatars.slack-edge.com/a.png", "slack"),
            "https://avatars.slack-edge.com/a.png",
        )
        self.assertEqual(
            ok("https://q1.qlogo.cn/g?b=qq&nk=1&s=640", "qq"),
            "https://q1.qlogo.cn/g?b=qq&nk=1&s=640",
        )
        for bad in (
            "http://127.0.0.1/a.png",
            "http://localhost/a.png",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/a.png",
            "http://[::1]/a.png",
            "file:///etc/passwd",
            "https://user:pw@cdn.example.com/a.png",
            # 域名不在该平台白名单内（含指向内网的第三方域名）一律拒绝
            "https://cdn.example.com/a.png",
            "https://evil-slack.com/a.png",
            "",
            None,
        ):
            with self.subTest(url=bad):
                self.assertIsNone(ok(bad, "slack"))

    def test_avatar_url_sanitizer_rejects_other_platforms_hosts(self):
        ok = self.plugin._sanitize_avatar_url
        # 跨平台域名不放行：Slack 的结果不能指到 Discord/飞书 CDN。
        self.assertIsNone(ok("https://cdn.discordapp.com/avatars/1/a.png", "slack"))
        self.assertIsNone(ok("https://s1-imfile.feishucdn.com/a.png", "telegram"))
        self.assertIsNotNone(ok("https://cdn.discordapp.com/embed/avatars/0.png", "discord"))

    def test_qq_self_id_prefers_message_self_id_when_getter_returns_group(self):
        # get_self_id 返回的不是机器人 QQ 号（如群号）时，message_obj.self_id
        # 仍需参与自身判定，否则命令前 @ 机器人会被当成被摸对象
        # （PR #18 审查：incomplete-self-id-resolution）。
        event = PlatformEvent([At(qq="100001"), Plain(" /摸摸")], "aiocqhttp")
        event.get_self_id = Mock(return_value="999999")
        self.assertIsNone(self.plugin._resolve_mentioned_user(event))

    async def test_avatar_hint_is_task_local(self):
        # 提示用 contextvars 承载：并发任务之间不共享，也不会互相清空
        # （PR #18 审查：shared-mutable-state-race）。
        self.plugin.config = {
            "platform_tokens": {"feishu_app_id": "id", "feishu_app_secret": "sec"}
        }
        self.plugin._session = FakeSession(
            [
                FakeResponse(200, {"tenant_access_token": "t-1"}),
                FakeResponse(403, {"code": 41050, "msg": "no permission"}),
            ]
        )
        event = PlatformEvent([Plain("/摸摸")], "feishu")

        async def read_hint():
            await asyncio.sleep(0)
            return self.plugin._avatar_unavailable_reason("feishu")

        other = asyncio.create_task(read_hint())  # 创建时拷贝当前（空）上下文
        await self.plugin._resolve_avatar_url(event, "feishu", "ou_abc123")
        self.assertIn("通讯录权限", self.plugin._avatar_unavailable_reason("feishu"))
        self.assertNotIn("通讯录权限", await other)

    async def test_qq_avatar_url_requires_numeric_id(self):
        event = PlatformEvent([Plain("/摸摸")], "aiocqhttp")
        for bad in ("abc", "12&s=1", "", "../../x", "１２３"):
            with self.subTest(target=bad):
                self.assertIsNone(
                    await self.plugin._resolve_avatar_url(event, "qq", bad)
                )
        self.assertEqual(
            await self.plugin._resolve_avatar_url(event, "qq", "234567"),
            "https://q1.qlogo.cn/g?b=qq&nk=234567&s=640",
        )

    async def test_slack_avatar_url_internal_target_is_rejected(self):
        self.plugin.config = {"platform_tokens": {"slack_bot_token": "xoxb-1"}}
        self.plugin._session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "ok": True,
                        "user": {"profile": {"image_192": "http://169.254.169.254/x.png"}},
                    },
                )
            ]
        )
        self.assertIsNone(await self.plugin._slack_avatar_url("U0G9QF9C6"))

    def test_non_qq_leading_mention_of_bot_itself_is_dropped(self):
        # 命令前 @ 机器人自身时，@字段 与 self_id 常是不同写法（大小写/下划线/@前缀）。
        for platform, at_id, self_id in (
            ("telegram", "@My_Bot", "my_bot"),
            ("telegram", "MyBot", "@mybot"),
            ("slack", "U0G9QF9C6", "  u0g9qf9c6 "),
            ("feishu", "ou_ABC123", "ou_abc123"),
        ):
            with self.subTest(platform=platform, at_id=at_id):
                event = PlatformEvent(
                    [At(qq=at_id, name="机器人"), Plain(" /摸摸")], platform, self_id=self_id
                )
                self.assertIsNone(self.plugin._resolve_mentioned_user(event))

    def test_non_qq_command_self_mention_does_not_shadow_later_target(self):
        event = PlatformEvent(
            [At(qq="@My_Bot", name="机器人"), Plain(" /摸摸 "), At(qq="@target", name="目标")],
            "telegram",
            self_id="my_bot",
        )
        self.assertEqual(
            self.plugin._resolve_mentioned_user(event), ("@target", "目标")
        )

    async def test_telegram_error_response_returns_none(self):
        self.plugin.config = {"platform_tokens": {"telegram_bot_token": "bad"}}
        self.plugin._session = FakeSession(
            [FakeResponse(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})]
        )
        event = PlatformEvent([Plain("/摸摸")], "telegram")
        self.assertIsNone(await self.plugin._telegram_avatar_url(event, "987654"))

    async def test_slack_error_response_returns_none(self):
        self.plugin.config = {"platform_tokens": {"slack_bot_token": "xoxb-1"}}
        self.plugin._session = FakeSession(
            [FakeResponse(200, {"ok": False, "error": "missing_scope"})]
        )
        self.assertIsNone(await self.plugin._slack_avatar_url("U0G9QF9C6"))

    async def test_slack_avatar_main_path(self):
        self.plugin.config = {"platform_tokens": {"slack_bot_token": "xoxb-1"}}
        self.plugin._session = FakeSession(
            [
                FakeResponse(
                    200,
                    {"ok": True, "user": {"profile": {"image_192": "https://avatars.slack-edge.com/a.png"}}},
                )
            ]
        )
        self.assertEqual(
            await self.plugin._slack_avatar_url("U0G9QF9C6"), "https://avatars.slack-edge.com/a.png"
        )

    async def test_feishu_accepts_plain_string_avatar(self):
        self.plugin.config = {
            "platform_tokens": {"feishu_app_id": "id", "feishu_app_secret": "sec"}
        }
        self.plugin._session = FakeSession(
            [
                FakeResponse(200, {"tenant_access_token": "t-1"}),
                FakeResponse(
                    200, {"code": 0, "data": {"user": {"avatar": "https://s1-imfile.feishucdn.com/plain.png"}}}
                ),
            ]
        )
        self.assertEqual(
            await self.plugin._feishu_avatar_url("ou_abc123"), "https://s1-imfile.feishucdn.com/plain.png"
        )

    async def test_feishu_accepts_avatar_url_field(self):
        self.plugin.config = {
            "platform_tokens": {"feishu_app_id": "id", "feishu_app_secret": "sec"}
        }
        self.plugin._session = FakeSession(
            [
                FakeResponse(200, {"tenant_access_token": "t-1"}),
                FakeResponse(
                    200, {"code": 0, "data": {"user": {"avatar_url": "https://s1-imfile.feishucdn.com/url.png"}}}
                ),
            ]
        )
        self.assertEqual(
            await self.plugin._feishu_avatar_url("ou_abc123"), "https://s1-imfile.feishucdn.com/url.png"
        )

    async def test_discord_rejects_non_snowflake_id(self):
        self.plugin.config = {"platform_tokens": {"discord_bot_token": "t"}}
        self.plugin._session = FakeSession([])
        for uid in ("", "abc", "../../users", "@name", "12 34"):
            with self.subTest(uid=uid):
                self.assertIsNone(await self.plugin._discord_avatar_url(uid))
        self.assertEqual(self.plugin._session.calls, [])

    async def test_discord_error_response_is_not_treated_as_user(self):
        self.plugin.config = {"platform_tokens": {"discord_bot_token": "t"}}
        self.plugin._session = FakeSession(
            [FakeResponse(404, {"code": 10013, "message": "Unknown User"})]
        )
        self.assertIsNone(await self.plugin._discord_avatar_url("123456789012345678"))

    async def test_discord_falls_back_to_default_avatar(self):
        self.plugin.config = {"platform_tokens": {"discord_bot_token": "t"}}
        self.plugin._session = FakeSession(
            [FakeResponse(200, {"id": "123456789012345678", "avatar": None})]
        )
        url = await self.plugin._discord_avatar_url("123456789012345678")
        self.assertTrue(url.startswith("https://cdn.discordapp.com/embed/avatars/"))

    async def test_discord_custom_avatar_uses_cdn_path(self):
        self.plugin.config = {"platform_tokens": {"discord_bot_token": "t"}}
        self.plugin._session = FakeSession(
            [FakeResponse(200, {"id": "123456789012345678", "avatar": "a_abcdef"})]
        )
        url = await self.plugin._discord_avatar_url("123456789012345678")
        self.assertEqual(
            url,
            "https://cdn.discordapp.com/avatars/123456789012345678/a_abcdef.gif?size=256",
        )

    async def test_feishu_accepts_variant_avatar_fields(self):
        self.plugin.config = {
            "platform_tokens": {"feishu_app_id": "id", "feishu_app_secret": "sec"}
        }
        self.plugin._session = FakeSession(
            [
                FakeResponse(200, {"tenant_access_token": "t-1"}),
                FakeResponse(
                    200,
                    {
                        "code": 0,
                        "data": {"user": {"avatar": {"avatar_big": "https://s1-imfile.feishucdn.com/big.png"}}},
                    },
                ),
            ]
        )
        self.assertEqual(
            await self.plugin._feishu_avatar_url("ou_abc123"), "https://s1-imfile.feishucdn.com/big.png"
        )

    async def test_feishu_error_code_returns_none(self):
        self.plugin.config = {
            "platform_tokens": {"feishu_app_id": "id", "feishu_app_secret": "sec"}
        }
        self.plugin._session = FakeSession(
            [
                FakeResponse(200, {"tenant_access_token": "t-1"}),
                FakeResponse(200, {"code": 41050, "msg": "no permission"}),
            ]
        )
        self.assertIsNone(await self.plugin._feishu_avatar_url("ou_abc123"))

    async def test_feishu_permission_error_adds_actionable_hint(self):
        # 自建应用常见 403/41050：提示必须指出缺通讯录权限，而不是只说去填凭据。
        self.plugin.config = {
            "platform_tokens": {"feishu_app_id": "id", "feishu_app_secret": "sec"}
        }
        self.plugin._session = FakeSession(
            [
                FakeResponse(200, {"tenant_access_token": "t-1"}),
                FakeResponse(403, {"code": 41050, "msg": "no permission"}),
            ]
        )
        event = PlatformEvent([Plain("/摸摸")], "feishu")
        self.assertIsNone(await self.plugin._resolve_avatar_url(event, "feishu", "ou_abc123"))
        reason = self.plugin._avatar_unavailable_reason("feishu", "ou_abc123")
        self.assertIn("通讯录权限", reason)

    async def test_telegram_weird_file_path_is_rejected(self):
        self.plugin.config = {"platform_tokens": {"telegram_bot_token": "123:abc"}}
        for file_path in ("../../etc/passwd", "photos/x.jpg?evil=1", "", "photos/x y.jpg"):
            with self.subTest(file_path=file_path):
                self.plugin._session = FakeSession(
                    [
                        FakeResponse(
                            200, {"ok": True, "result": {"photos": [[{"file_id": "f1"}]]}}
                        ),
                        FakeResponse(200, {"ok": True, "result": {"file_path": file_path}}),
                    ]
                )
                event = PlatformEvent([Plain("/摸摸")], "telegram")
                self.assertIsNone(await self.plugin._telegram_avatar_url(event, "987654"))


class GifGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.plugin = TouchHeadPlugin.__new__(TouchHeadPlugin)
        self.plugin.config = {}
        self.plugin.output_dir = Path(self.temp_dir.name)
        self.plugin.assets_dir = ROOT / "data" / "petpet"
        self.plugin._is_terminating = False
        self.avatar = Image.new("RGBA", (100, 100), "blue")
        self.addCleanup(self.avatar.close)

    def test_hand_moves_up_five_pixels_without_moving_avatar(self):
        self.plugin.assets_dir = self.plugin.output_dir / "templates"
        self.plugin.assets_dir.mkdir()
        markers = [(30, (255, 0, 0)), (34, (0, 255, 0))]
        originals = {}
        for index, (top, color) in enumerate(markers):
            template = self.plugin.assets_dir / f"frame{index}.png"
            with Image.new("RGBA", (112, 112), (0, 0, 0, 0)) as hand:
                ImageDraw.Draw(hand).rectangle((40, top, 60, top + 9), fill=(*color, 255))
                hand.save(template)
            originals[template] = template.read_bytes()

        gif_path = self.plugin._build_petpet_gif(self.avatar, "target")
        self.assertIsNotNone(gif_path)
        with Image.open(gif_path) as gif:
            self.assertEqual(gif.size, (112, 112))
            self.assertEqual(gif.n_frames, len(markers))
            for index, (top, color) in enumerate(markers):
                gif.seek(index)
                with gif.convert("RGB") as frame:
                    self.assertEqual(frame.getpixel((50, top - 5)), color)
                    self.assertEqual(frame.getpixel((50, top + 9)), (0, 0, 255))
                    self.assertEqual(frame.getpixel((50, 16)), (0, 0, 255))
                    self.assertEqual(frame.getpixel((50, 15)), (255, 255, 255))
        for template, original in originals.items():
            self.assertEqual(template.read_bytes(), original)

    def assert_duration(self, config, expected):
        self.plugin.config = config
        gif_path = self.plugin._build_petpet_gif(self.avatar, "target")
        self.assertIsNotNone(gif_path)
        with Image.open(gif_path) as gif:
            self.assertEqual(gif.n_frames, 5)
            for index in range(gif.n_frames):
                gif.seek(index)
                self.assertEqual(gif.info["duration"], expected)

    def test_configured_speeds_change_each_frame_duration(self):
        for speed, duration in ((0.5, 200), (1.0, 100), (1.5, 70), (2.0, 50), (3.0, 30)):
            with self.subTest(speed=speed):
                self.assert_duration({"patpat": {"speed": speed}}, duration)

    def test_bad_speed_falls_back_to_default(self):
        for speed in (None, True, False, 0, -1, float("nan"), float("inf"), "x", [], {}):
            with self.subTest(speed=speed):
                self.assert_duration({"patpat": {"speed": speed}}, 100)

    def test_speed_outside_ui_range_falls_back_to_default(self):
        for speed in (0.1, 0.49, 3.01, 4.0, 10**400):
            with self.subTest(speed=speed):
                self.assert_duration({"patpat": {"speed": speed}}, 100)

    def test_missing_or_malformed_config_falls_back_to_default(self):
        for config in (None, {}, {"patpat": None}, {"patpat": []}, {"patpat": {}}):
            with self.subTest(config=config):
                self.assert_duration(config, 100)

    def test_changed_config_is_used_by_subsequent_gifs(self):
        config = {"patpat": {"speed": 0.5}}
        self.assert_duration(config, 200)
        config["patpat"]["speed"] = 2.0
        self.assert_duration(config, 50)

    def test_bundled_templates_still_produce_a_complete_gif(self):
        gif_path = self.plugin._build_petpet_gif(self.avatar, "target")
        self.assertIsNotNone(gif_path)
        with Image.open(gif_path) as gif:
            self.assertEqual(gif.size, (112, 112))
            self.assertEqual(gif.n_frames, 5)
            self.assertEqual(gif.info["loop"], 0)
            for index in range(gif.n_frames):
                gif.seek(index)
                self.assertEqual(gif.info["duration"], 100)
                gif.load()


if __name__ == "__main__":
    unittest.main()


class PlatformRequestContractTests(unittest.IsolatedAsyncioTestCase):
    """接口契约：请求地址、方法、参数写错时必须能被测试直接发现。

    原来的 FakeSession 只回放响应、不校验请求，接口路径或参数写错也照样通过
    （PR #18 审查：fake-session-does-not-validate-requests）。
    """

    def setUp(self):
        self.plugin = TouchHeadPlugin.__new__(TouchHeadPlugin)
        self.plugin.config = {}
        self.plugin._is_terminating = False
        self.plugin._session = None

    async def test_qq_avatar_url_contract(self):
        event = PlatformEvent([Plain("摸摸")], "aiocqhttp")
        self.assertEqual(
            await self.plugin._resolve_avatar_url(event, "qq", "234567"),
            "https://q1.qlogo.cn/g?b=qq&nk=234567&s=640",
        )

    async def test_telegram_request_contract(self):
        self.plugin.config = {"platform_tokens": {"telegram_bot_token": "1:abc"}}
        session = FakeSession(
            [
                FakeResponse(200, {"ok": True, "result": {"photos": [[{"file_id": "f1"}]]}}),
                FakeResponse(200, {"ok": True, "result": {"file_path": "photos/a.jpg"}}),
            ]
        )
        self.plugin._session = session
        event = PlatformEvent([Plain("摸摸")], "telegram")
        url = await self.plugin._telegram_avatar_url(event, "987654")

        self.assertEqual(url, "https://api.telegram.org/file/bot1:abc/photos/a.jpg")
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET"])
        self.assertEqual(
            session.calls[0][1], "https://api.telegram.org/bot1:abc/getUserProfilePhotos"
        )
        self.assertEqual(session.calls[0][2]["params"], {"user_id": "987654", "limit": 1})
        self.assertEqual(
            session.calls[1][1], "https://api.telegram.org/bot1:abc/getFile"
        )
        self.assertEqual(session.calls[1][2]["params"], {"file_id": "f1"})

    async def test_discord_request_contract(self):
        self.plugin.config = {"platform_tokens": {"discord_bot_token": "tok"}}
        uid = "123456789012345678"
        session = FakeSession([FakeResponse(200, {"id": uid, "avatar": "ab12"})])
        self.plugin._session = session
        url = await self.plugin._discord_avatar_url(uid)

        self.assertEqual(
            url, "https://cdn.discordapp.com/avatars/123456789012345678/ab12.png?size=256"
        )
        self.assertEqual(session.calls[0][1], "https://discord.com/api/v10/users/" + uid)
        self.assertEqual(session.calls[0][2]["headers"]["Authorization"], "Bot tok")

    async def test_slack_request_contract(self):
        self.plugin.config = {"platform_tokens": {"slack_bot_token": "xoxb-1"}}
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "ok": True,
                        "user": {
                            "profile": {"image_192": "https://avatars.slack-edge.com/x.png"}
                        },
                    },
                )
            ]
        )
        self.plugin._session = session
        url = await self.plugin._slack_avatar_url("U0G9QF9C6")

        self.assertEqual(url, "https://avatars.slack-edge.com/x.png")
        self.assertEqual(session.calls[0][0], "POST")
        self.assertEqual(session.calls[0][1], "https://slack.com/api/users.info")
        self.assertEqual(session.calls[0][2]["data"], {"user": "U0G9QF9C6"})
        self.assertEqual(session.calls[0][2]["headers"]["Authorization"], "Bearer xoxb-1")

    async def test_feishu_request_contract(self):
        self.plugin.config = {
            "platform_tokens": {"feishu_app_id": "id1", "feishu_app_secret": "sec1"}
        }
        session = FakeSession(
            [
                FakeResponse(200, {"code": 0, "tenant_access_token": "t-1"}),
                FakeResponse(
                    200,
                    {
                        "code": 0,
                        "data": {
                            "user": {
                                "avatar": {
                                    "avatar_240": "https://s1-imfile.feishucdn.com/a.png"
                                }
                            }
                        },
                    },
                ),
            ]
        )
        self.plugin._session = session
        url = await self.plugin._feishu_avatar_url("ou_abc")

        self.assertEqual(url, "https://s1-imfile.feishucdn.com/a.png")
        self.assertEqual(
            session.calls[0][1],
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        )
        self.assertEqual(
            session.calls[0][2]["json"], {"app_id": "id1", "app_secret": "sec1"}
        )
        self.assertEqual(
            session.calls[1][1],
            "https://open.feishu.cn/open-apis/contact/v3/users/ou_abc",
        )
        self.assertEqual(session.calls[1][2]["params"], {"user_id_type": "open_id"})
        self.assertEqual(session.calls[1][2]["headers"]["Authorization"], "Bearer t-1")

    async def test_telegram_negative_user_id_is_rejected_without_request(self):
        # Telegram user_id 只可能是正整数，负数是 chat_id
        # （PR #18 审查：telegram-user-id-sign）。
        self.plugin.config = {"platform_tokens": {"telegram_bot_token": "1:abc"}}
        session = FakeSession([])
        self.plugin._session = session
        event = PlatformEvent([Plain("摸摸")], "telegram")
        self.assertIsNone(await self.plugin._telegram_avatar_url(event, "-100123"))
        self.assertEqual(session.calls, [])
        self.assertIn(
            "数字 user_id", self.plugin._avatar_unavailable_base_reason("telegram", "-100123")
        )

    async def test_discord_default_avatar_follows_username_system(self):
        # 官方规则：迁移到新用户名体系（discriminator 为 "0"）用 (id >> 22) % 6，
        # 旧体系仍用 discriminator % 5
        # （PR #18 审查：discord-default-avatar-index）。
        self.plugin.config = {"platform_tokens": {"discord_bot_token": "tok"}}
        uid = "123456789012345678"
        for discriminator, expected in (("0", (int(uid) >> 22) % 6), ("1234", 1234 % 5)):
            with self.subTest(discriminator=discriminator):
                self.plugin._session = FakeSession(
                    [
                        FakeResponse(
                            200,
                            {"id": uid, "avatar": None, "discriminator": discriminator},
                        )
                    ]
                )
                self.assertEqual(
                    await self.plugin._discord_avatar_url(uid),
                    "https://cdn.discordapp.com/embed/avatars/{}.png".format(expected),
                )

    async def test_self_touch_on_unnamed_adapter_falls_back_to_qq(self):
        # 未实现 get_platform_name 的适配器上，"摸摸"（无 @）也要能取到头像，
        # 否则与"摸摸 @某人"行为不一致
        # （PR #18 审查：self-avatar-unreachable-on-unnamed-adapter）。
        class AnonymousEvent(PlatformEvent):
            def get_platform_name(self):
                raise AttributeError("adapter without get_platform_name")

        self.plugin._download_image = AsyncMock(return_value="img")
        event = AnonymousEvent([Plain("摸摸")], "", sender_id="234567")
        self.assertEqual(self.plugin._effective_kind(event, "234567"), "qq")
        self.assertEqual(await self.plugin._get_user_avatar_by_id(event, "234567"), "img")
        _assert_avatar_downloaded(self, "https://q1.qlogo.cn/g?b=qq&nk=234567&s=640")


class AvatarDownloadRedirectTests(unittest.IsolatedAsyncioTestCase):
    """下载阶段必须逐跳复核白名单（PR #18 审查：redirect-ssrf-bypass）。"""

    AVATAR_URL = "https://cdn.discordapp.com/avatars/1/a.png"

    def setUp(self):
        self.plugin = TouchHeadPlugin.__new__(TouchHeadPlugin)
        self.plugin.config = {}
        self.plugin._is_terminating = False
        self.plugin._session = None

    async def test_redirect_to_internal_address_is_rejected(self):
        session = FakeSession(
            [
                FakeResponse(
                    302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
                )
            ]
        )
        self.plugin._session = session
        self.assertIsNone(await self.plugin._download_image(self.AVATAR_URL, "discord"))
        # 只在首跳发了请求，内网地址从未被访问
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.calls[0][2]["allow_redirects"])

    async def test_redirect_to_other_platform_host_is_rejected(self):
        session = FakeSession(
            [FakeResponse(307, headers={"Location": "https://evil.example.com/a.png"})]
        )
        self.plugin._session = session
        self.assertIsNone(await self.plugin._download_image(self.AVATAR_URL, "discord"))
        self.assertEqual(len(session.calls), 1)

    async def test_allowed_redirect_is_refollowed_and_revalidated(self):
        session = FakeSession(
            [
                FakeResponse(302, headers={"Location": "/avatars/2/b.png"}),
                FakeResponse(200, headers={"Content-Type": "text/html"}, body=b"<html>"),
            ]
        )
        self.plugin._session = session
        self.assertIsNone(await self.plugin._download_image(self.AVATAR_URL, "discord"))
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[1][1], "https://cdn.discordapp.com/avatars/2/b.png")
        self.assertFalse(session.calls[1][2]["allow_redirects"])

    async def test_too_many_redirects_gives_up(self):
        responses = [
            FakeResponse(302, headers={"Location": "/avatars/2/b.png"})
            for _ in range(TouchHeadPlugin._MAX_AVATAR_REDIRECTS + 1)
        ]
        session = FakeSession(responses)
        self.plugin._session = session
        self.assertIsNone(await self.plugin._download_image(self.AVATAR_URL, "discord"))
        self.assertEqual(len(session.calls), TouchHeadPlugin._MAX_AVATAR_REDIRECTS + 1)


class AvatarHintLifetimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = TouchHeadPlugin.__new__(TouchHeadPlugin)
        self.plugin.config = {}

    async def test_avatar_hint_is_consumed_once(self):
        # 构造用户回复时 consume=True 才清空，避免残留到后续回复
        # （PR #18 审查：avatar-hint-leaks-across-tasks）。
        plugin_module._AVATAR_HINT.set("飞书应用缺少通讯录权限")
        self.assertIn(
            "通讯录权限", self.plugin._avatar_unavailable_reason("feishu", consume=True)
        )
        self.assertNotIn(
            "通讯录权限", self.plugin._avatar_unavailable_reason("feishu", consume=True)
        )

    async def test_logging_does_not_consume_hint(self):
        # 只读不消费：日志读完提示仍要留给用户回复
        # （PR #18 审查：consumed-error-hint）。
        plugin_module._AVATAR_HINT.set("飞书应用缺少通讯录权限")
        self.assertIn("通讯录权限", self.plugin._avatar_unavailable_reason("feishu"))
        self.assertIn(
            "通讯录权限", self.plugin._avatar_unavailable_reason("feishu", consume=True)
        )

    async def test_feishu_permission_denied_reply_contains_hint(self):
        # 完整走一遍命令：飞书 403 时最终回复必须带上权限提示
        # （PR #18 审查：consumed-error-hint）。
        self.plugin.config = {
            "platform_tokens": {"feishu_app_id": "id", "feishu_app_secret": "sec"}
        }
        self.plugin._is_terminating = False
        self.plugin._session = FakeSession(
            [
                FakeResponse(200, {"code": 0, "tenant_access_token": "t-1"}),
                FakeResponse(403, {"code": 41050, "msg": "no permission"}),
            ]
        )
        event = PlatformEvent([Plain("/摸摸")], "feishu")
        result = await self.plugin.handle_command(event)
        self.assertEqual(result[0], "text")
        self.assertIn("通讯录权限", result[1])

    async def test_unknown_kind_path_clears_previous_hint(self):
        # kind 为 unknown 时不会进入 _resolve_avatar_url，提示必须在这里也被清掉。
        plugin_module._AVATAR_HINT.set("飞书应用缺少通讯录权限")

        async def fake_download(url, kind=""):
            return None

        self.plugin._download_image = fake_download
        event = PlatformEvent([Plain("摸摸")], "some_new_platform")
        self.assertIsNone(await self.plugin._get_user_avatar_by_id(event, "u1"))
        self.assertEqual(plugin_module._AVATAR_HINT.get(), "")

class PlatformKindConsistencyTests(unittest.IsolatedAsyncioTestCase):
    """同一事件只能用一份平台判定（PR #18 审查：platform-kind-inconsistency）。"""

    def setUp(self):
        self.plugin = TouchHeadPlugin.__new__(TouchHeadPlugin)
        self.plugin.config = {}
        self.plugin._is_terminating = False
        self.plugin._session = None

    def test_target_id_takes_precedence_over_mention_shape(self):
        # 有明确目标时以目标标识为准：@ 里是不是数字不再决定平台
        # （PR #18 审查：target-id-ignored-in-platform-fallback）。
        class AnonymousEvent(PlatformEvent):
            def get_platform_name(self):
                raise AttributeError("adapter without get_platform_name")

        event = AnonymousEvent([Plain("/摸摸 "), At(qq="@user")], "")
        self.assertEqual(self.plugin._effective_kind(event, "234567"), "qq")
        self.assertEqual(self.plugin._effective_kind(event, "@user"), "unknown")
        # 没有目标时退回看 @ 形态（@解析阶段的判定入口）
        self.assertEqual(self.plugin._effective_kind(event), "unknown")

    async def test_handle_command_passes_single_kind_down(self):
        avatar = Image.new("RGBA", (10, 10), "blue")
        self.addCleanup(avatar.close)
        self.plugin._get_user_avatar_by_id = AsyncMock(return_value=avatar)
        self.plugin._build_petpet_gif = Mock(return_value=None)
        self.plugin.assets_dir = Path(tempfile.gettempdir())
        self.plugin._download_image = AsyncMock()

        event = PlatformEvent([Plain("/摸摸 "), At(qq="234567")], "aiocqhttp")
        await self.plugin.handle_command(event)

        # 目标确定后算出的 kind 原样传下去，不再在下游重算
        self.plugin._get_user_avatar_by_id.assert_awaited_once_with(
            event, "234567", "qq"
        )


class DownloadBoundaryValidationTests(unittest.IsolatedAsyncioTestCase):
    """下载边界自身也要校验地址（PR #18 审查：missing-url-validation-at-download-boundary）。"""

    def setUp(self):
        self.plugin = TouchHeadPlugin.__new__(TouchHeadPlugin)
        self.plugin.config = {}
        self.plugin._is_terminating = False
        self.plugin._session = None

    async def test_unsanitized_url_is_rejected_before_request(self):
        session = FakeSession([FakeResponse(200, headers={"Content-Type": "image/png"})])
        self.plugin._session = session
        for bad in (
            "http://127.0.0.1/a.png",
            "https://evil.example.com/a.png",
            "file:///etc/passwd",
        ):
            with self.subTest(url=bad):
                self.assertIsNone(await self.plugin._download_image(bad, "qq"))
        self.assertEqual(session.calls, [])

    async def test_unknown_kind_is_rejected_if_caller_forgets_kind(self):
        session = FakeSession([FakeResponse(200, headers={"Content-Type": "image/png"})])
        self.plugin._session = session
        self.assertIsNone(
            await self.plugin._download_image("https://q1.qlogo.cn/g?b=qq&nk=1&s=640")
        )
        self.assertEqual(session.calls, [])
