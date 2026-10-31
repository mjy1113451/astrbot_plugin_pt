import base64
import re
import time
import asyncio
import math
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image
import aiohttp

from astrbot.api import star, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.star import StarTools

# 设置Pillow图像像素上限，防止解压炸弹
Image.MAX_IMAGE_PIXELS = 10_000_000


class TouchHeadPlugin(star.Star):
    """ 摸头杀插件主类。严格遵循AstrBot生命周期，使用线程池处理CPU密集型任务，确保异步事件循环不被阻塞。 """

    def __init__(self, context: star.Context):
        super().__init__(context)
        logger.info("摸头杀插件正在初始化...")

        self.data_dir = StarTools.get_data_dir()
        self.output_dir = self.data_dir / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.assets_dir = Path(__file__).parent / "assets"
        if not self.assets_dir.exists():
            self.assets_dir.mkdir(parents=True, exist_ok=True)
            logger.warning(f"资源目录不存在，已创建空目录: {self.assets_dir}")

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
        从事件中解析发送者的 (user_id, nickname)。
        优先使用 AstrBot 通用接口 get_sender_id() / get_sender_name()，
        失败时回退到 event.message_obj.sender (MessageMember)。

        返回:
            (user_id, nickname) 元组；若无法获取有效 user_id，返回 None。
        """
        user_id: Optional[str] = None
        nickname: str = ""

        # 1) 首选：AstrBot 通用接口
        try:
            uid = event.get_sender_id()
            if uid:
                user_id = str(uid)
            nickname = event.get_sender_name() or ""
        except Exception as e:
            logger.debug(f"get_sender_id/get_sender_name 不可用或出错: {e}")

        # 2) 回退：message_obj.sender (MessageMember)
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

        # 3) 最终校验：user_id 必须存在且非空
        if not user_id:
            logger.error(
                "无法解析发送者 user_id，可能该平台适配器未提供发送者信息。"
                f"event type={type(event).__name__}, platform={getattr(event, 'get_platform_name', lambda: 'unknown')()}"
            )
            return None

        return user_id, nickname

    # --- 核心功能实现 ---

    @filter.command("摸摸")
    async def handle_command(self, event: AstrMessageEvent):
        """ 处理"摸摸"命令。支持摸自己和@用户。 """
        # 1. 解析发送者（已抽离为独立辅助方法）
        sender = self._resolve_sender(event)
        if sender is None:
            return event.set_result(
                MessageEventResult().message("无法识别您的用户信息，暂时无法处理摸头杀命令。")
            )

        sender_id, sender_name = sender
        # 兜底：昵称为空时用 user_id 代替，避免界面显示空白
        if not sender_name:
            sender_name = str(sender_id)

        # 2. 解析 @ 目标
        target_user = None
        for component in event.message_obj.message:
            if hasattr(component, "type") and component.type == "at":
                target_id = (
                    component.data.get("qq")
                    or component.data.get("user_id")
                )
                if target_id:
                    target_user = {
                        "user_id": str(target_id),
                        "nickname": component.data.get("name", str(target_id)),
                    }
                    logger.info(
                        f"检测到@用户: {target_user['nickname']} ({target_user['user_id']})"
                    )
                    break

        # 3. 确定最终目标
        if target_user:
            target_name = target_user["nickname"]
            target_id = target_user["user_id"]
            action_desc = f"{sender_name} 摸了摸 {target_name}"
        else:
            target_name = sender_name
            target_id = sender_id
            action_desc = f"{sender_name} 摸了摸自己"

        logger.info(f"收到摸头杀命令: {action_desc}")

        try:
            user_image = await self._get_user_avatar_by_id(event, target_id)
            if user_image is None:
                return event.set_result(
                    MessageEventResult().message("抱歉，无法获取头像，无法生成摸头杀图片。")
                )

            gif_path = await asyncio.to_thread(
                self._build_petpet_gif, user_image, target_name
            )

            if gif_path and gif_path.exists():
                await event.send_message(AstrImage.fromFilePath(str(gif_path)))
            else:
                await event.send_message("生成摸头杀图片失败，请稍后再试。")

        except Exception as e:
            logger.error(f"处理摸头杀命令时发生错误: {e}", exc_info=True)
            await event.send_message("发生内部错误，无法处理您的请求。")

    # --- 头像获取与GIF生成（逻辑与之前一致，仅修复缩进） ---

    async def _get_user_avatar_by_id(
        self, event: AstrMessageEvent, user_id: str
    ) -> Optional[Image.Image]:
        try:
            qq_avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
            logger.info(f"下载用户头像: {qq_avatar_url}")
            return await self._download_image(qq_avatar_url)
        except Exception as e:
            logger.error(f"下载用户 {user_id} 的头像失败: {e}")
            return None

    async def _download_image(self, url: str) -> Optional[Image.Image]:
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()

            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status != 200:
                    logger.error(f"下载图片失败，HTTP状态码: {response.status}")
                    return None

                content_type = response.headers.get("Content-Type", "")
                if "image" not in content_type:
                    logger.warning(f"非图片Content-Type: {content_type}")
                    return None

                max_size = 5 * 1024 * 1024
                if response.content_length and response.content_length > max_size:
                    logger.warning(
                        f"图片过大，超过限制: {response.content_length} bytes"
                    )
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
            logger.error(f"下载头像网络错误: {e}")
        except Exception as e:
            logger.error(f"处理下载头像时发生意外错误: {e}")

        return None

    def _build_petpet_gif(
        self, user_image: Image.Image, username: str
    ) -> Optional[Path]:
        if self._is_terminating:
            return None

        logger.info(f"开始为用户 {username} 生成GIF...")

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
                    canvas = Image.alpha_composite(canvas, hand_frame)
                    frames.append(canvas.convert("RGB"))

            if frames:
                frames[0].save(
                    output_path,
                    save_all=True,
                    append_images=frames[1:],
                    duration=100,
                    loop=0,
                    optimize=True,
                    disposal=2,
                )
                logger.info(f"GIF已生成并保存到: {output_path}")
                return output_path
            else:
                logger.error("未能生成任何帧。")
                return None

        except Exception as e:
            logger.error(f"生成GIF过程中发生错误: {e}", exc_info=True)
            if output_path.exists():
                try:
                    output_path.unlink()
                except Exception:
                    pass
            return None

    def _calculate_deformation(
        self, frame_index: int, total_frames: int
    ) -> float:
        if total_frames <= 1:
            return 1.0
        progress = frame_index / (total_frames - 1)
        deformation_factor = math.sin(progress * math.pi)
        return 0.85 + 0.15 * (1 - deformation_factor)

    async def _cleanup_old_gifs(self):
        while not self._is_terminating:
            try:
                await asyncio.sleep(3600)
                logger.info("执行GIF清理任务...")

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
                    logger.info(f"本次清理了 {count} 个旧GIF文件。")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"清理任务发生错误: {e}", exc_info=True)