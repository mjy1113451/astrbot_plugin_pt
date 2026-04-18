import asyncio
import base64
import re
import time
from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image
import aiohttp

from astrbot.api import star, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.star import StarTools

# 设置Pillow图像像素上限，防止解压炸弹
Image.MAX_IMAGE_PIXELS = 10_000_000


class TouchHeadPlugin(star.Star):
    """ 摸头杀插件主类。 严格遵循AstrBot生命周期，使用线程池处理CPU密集型任务， 确保异步事件循环不被阻塞。 """

    def __init__(self, context: star.Context):
        super().__init__(context)
        logger.info("摸头杀插件正在初始化...")

        # 1. 使用规范的数据持久化目录
        # StarTools.get_data_dir() 返回的已经是Path对象，无需再次包裹
        self.data_dir = StarTools.get_data_dir()
        self.output_dir = self.data_dir / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 2. 资源路径（模板图片）使用插件目录
        self.assets_dir = Path(__file__).parent / "assets"
        if not self.assets_dir.exists():
            self.assets_dir.mkdir(parents=True, exist_ok=True)
            logger.warning(f"资源目录不存在，已创建空目录: {self.assets_dir}")

        # 3. 初始化异步任务管理
        self._cleanup_task: Optional[asyncio.Task] = None
        self._is_terminating = False  # 用于优雅关闭

        # 4. 初始化aiohttp session（复用）
        self._session: Optional[aiohttp.ClientSession] = None

        logger.info("摸头杀插件初始化完成。")

    async def on_astrbot_loaded(self):
        """ 插件加载完成后的生命周期钩子。 启动后台清理任务，并正确管理其生命周期。 """
        logger.info("摸头杀插件已加载，启动后台清理任务...")
        self._cleanup_task = asyncio.create_task(self._cleanup_old_gifs())

    async def terminate(self):
        """ 插件卸载/停止时的生命周期钩子。 取消后台任务，确保资源释放，避免任务泄漏。 """
        logger.info("摸头杀插件正在终止...")
        self._is_terminating = True

        # 安全取消后台任务
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                logger.info("后台清理任务已取消。")
            except Exception as e:
                logger.error(f"取消清理任务时出错: {e}")

        # 关闭aiohttp session
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("aiohttp session已关闭。")

        logger.info("摸头杀插件已终止。")

    # --- 核心功能实现 ---

@filter.command("摸头杀")
    async def handle_command(self, event: AstrMessageEvent):
        """ 处理“摸头杀”命令。 1. 异步获取头像（支持多种来源）。 2. 将CPU密集型GIF生成任务卸载到线程池，避免阻塞事件循环。 """
        sender_name = event.message_event_obj.sender.nickname
        logger.info(f"收到来自 {sender_name} 的摸头杀命令。")

        try:
            # 第一步：异步获取用户头像图片
            user_image = await self._get_user_avatar(event)
            if user_image is None:
                return event.set_result(
                    MessageEventResult().message("抱歉，无法获取您的头像，无法生成摸头杀图片。")
                )

            # 第二步：将CPU密集型任务卸载到线程池
            # 这是解决事件循环阻塞的关键
            gif_path = await asyncio.to_thread(
                self._build_petpet_gif, user_image, sender_name
            )

            if gif_path and gif_path.exists():
                # 使用异步方式发送图片（模拟，实际框架可能提供异步发送）
                await event.send_message(AstrImage.fromFilePath(str(gif_path)))
            else:
                await event.send_message("生成摸头杀图片失败，请稍后再试。")

        except Exception as e:
            logger.error(f"处理摸头杀命令时发生错误: {e}", exc_info=True)
            await event.send_message("发生内部错误，无法处理您的请求。")

    async def _get_user_avatar(self, event: AstrMessageEvent) -> Optional[Image.Image]:
        """ 获取用户头像，兼容多种来源： 1. 事件上下文中的头像URL（常见形式）。 2. 事件上下文中的头像Base64数据。 3. 通过框架API获取。 4. 作为fallback尝试下载QQ头像。 """
        # 尝试从事件上下文获取（框架不同字段名可能不同，需适配）
        avatar_url = getattr(event.message_event_obj.sender, "avatar", None)
        avatar_base64 = getattr(event.message_event_obj.sender, "avatar_base64", None)

        if avatar_url and isinstance(avatar_url, str) and avatar_url.startswith(("http://", "https://")):
            logger.info(f"从事件URL获取头像: {avatar_url}")
            return await self._download_image(avatar_url)
        elif avatar_base64 and isinstance(avatar_base64, str):
            logger.info("从事件Base64获取头像。")
            try:
                image_data = base64.b64decode(avatar_base64)
                # 安全验证：大小限制
                if len(image_data) > 5 * 1024 * 1024:
                    logger.warning("Base64头像过大，超过5MB限制")
                    return None
                # 安全验证：使用Pillow验证
                try:
                    with Image.open(BytesIO(image_data)) as img:
                        img.verify()  # 验证图片完整性
                except Exception:
                    logger.error("Base64头像验证失败")
                    return None

                # verify()后重新打开进行像素检查和处理
                with Image.open(BytesIO(image_data)) as img:
                    if img.width * img.height > Image.MAX_IMAGE_PIXELS:
                        logger.warning(f"Base64头像像素过大: {img.width}x{img.height}")
                        return None
                    # 返回图像的副本，避免上下文关闭后图像失效
                    return img.copy()
            except Exception as e:
                logger.error(f"解析Base64头像失败: {e}")

        # 尝试通过框架API获取（这是更标准的方式）
        try:
            avatar_bytes = await self._get_avatar_via_framework(event)
            if avatar_bytes:
                # 同样进行安全验证
                if len(avatar_bytes) > 5 * 1024 * 1024:
                    logger.warning("框架头像过大，超过5MB限制")
                    return None
                # 使用with上下文管理器验证
                try:
                    with Image.open(BytesIO(avatar_bytes)) as img:
                        img.verify()
                except Exception:
                    logger.error("框架头像验证失败")
                    return None

                with Image.open(BytesIO(avatar_bytes)) as img:
                    if img.width * img.height > Image.MAX_IMAGE_PIXELS:
                        logger.warning(f"框架头像像素过大: {img.width}x{img.height}")
                        return None
                    return img.copy()
        except AttributeError:
            logger.debug("框架未提供标准头像获取方法。")
        except Exception as e:
            logger.error(f"通过框架获取头像失败: {e}")

        # Fallback: 尝试下载QQ头像（原始逻辑，但增加安全限制）
        try:
            qq_id = getattr(event.message_event_obj.sender, "user_id", "")
            if qq_id:
                qq_avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={qq_id}&s=640"
                logger.info(f"Fallback: 尝试下载QQ头像: {qq_avatar_url}")
                return await self._download_image(qq_avatar_url)
        except Exception as e:
            logger.error(f"下载QQ头像失败: {e}")

        return None

    async def _download_image(self, url: str) -> Optional[Image.Image]:
        """安全下载网络图片，使用流式读取防止内存放大。"""
        try:
            # 复用session
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()

            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    logger.error(f"下载图片失败，HTTP状态码: {response.status}")
                    return None

                # 1. 检查Content-Type
                content_type = response.headers.get("Content-Type", "")
                if "image" not in content_type:
                    logger.warning(f"非图片Content-Type: {content_type}")
                    return None

                # 2. 限制下载数据大小 (5MB)
                max_size = 5 * 1024 * 1024
                if response.content_length and response.content_length > max_size:
                    logger.warning(f"图片过大，超过限制: {response.content_length} bytes")
                    return None

                # 3. 流式读取，边读边检查大小
                image_data = b""
                async for chunk in response.content.iter_chunked(8192):
                    image_data += chunk
                    if len(image_data) > max_size:
                        logger.warning("下载图片超过大小限制，已中止")
                        return None

                # 4. 验证并打开图片
                try:
                    with Image.open(BytesIO(image_data)) as img:
                        img.verify()
                except Exception:
                    logger.error("下载图片验证失败")
                    return None

                # 5. 检查像素尺寸并返回副本
                with Image.open(BytesIO(image_data)) as img:
                    if img.width * img.height > Image.MAX_IMAGE_PIXELS:
                        logger.warning(f"图片像素过大: {img.width}x{img.height}")
                        return None
                    return img.copy()

        except asyncio.TimeoutError:
            logger.error("下载图片超时。")
        except aiohttp.ClientError as e:
            logger.error(f"下载图片网络错误: {e}")
        except Exception as e:
            logger.error(f"处理下载图片时发生意外错误: {e}")

        return None

    def _build_petpet_gif(self, user_image: Image.Image, username: str) -> Optional[Path]:
        """ CPU密集型：生成摸头杀GIF。 正确处理图层顺序：头像在下，手在上，使用alpha通道混合。 此函数在单独的线程中运行，不会阻塞事件循环。 """
        if self._is_terminating:
            return None

        logger.info(f"开始为用户 {username} 生成GIF...")

        # 1. 准备输出文件路径 - 清洗用户名防止路径注入
        timestamp = int(time.time() * 1000)
        safe_username = re.sub(r'[/\\<>:"|?*\x00-\x1f]', '_', username)
        safe_username = safe_username[:50]
        output_filename = f"petpet_{safe_username}_{timestamp}.gif"
        output_path = self.output_dir / output_filename

        try:
            # 2. 调整用户头像尺寸并转换为RGBA模式
            avatar_size = (100, 100)
            user_image = user_image.convert("RGBA").resize(avatar_size, Image.Resampling.LANCZOS)

            # 3. 加载所有模板帧并处理
            frames = []
            frame_files = sorted(self.assets_dir.glob("frame*.png"))

            if not frame_files:
                logger.error(f"在 {self.assets_dir} 中未找到任何模板帧(frame*.png)。")
                return None

            for i, frame_path in enumerate(frame_files):
                with Image.open(frame_path) as hand_frame:
                    # 转换为RGBA模式以支持透明通道
                    hand_frame = hand_frame.convert("RGBA")

                    # 创建新画布（与手图层相同尺寸）
                    canvas = Image.new("RGBA", hand_frame.size, (255, 255, 255, 0))

                    # 计算头像位置（居中偏下）
                    avatar_x = (canvas.width - avatar_size[0]) // 2
                    avatar_y = (canvas.height - avatar_size[1]) // 2 + 10

                    # 根据帧索引添加变形效果（模拟被摸时的挤压）
                    deformation = self._calculate_deformation(i, len(frame_files))
                    if deformation != 1.0:
                        # 应用变形：水平挤压
                        new_width = int(avatar_size[0] * deformation)
                        avatar_frame = user_image.resize((new_width, avatar_size[1]), Image.Resampling.LANCZOS)
                        avatar_x = (canvas.width - new_width) // 2
                    else:
                        avatar_frame = user_image

                    # 1. 先粘贴头像（底层）- 使用alpha通道
                    canvas.paste(avatar_frame, (avatar_x, avatar_y), avatar_frame)

                    # 2. 再粘贴手图层（上层）- 使用alpha混合
                    canvas = Image.alpha_composite(canvas, hand_frame)

                    # 转换为RGB模式（GIF不支持RGBA）
                    frames.append(canvas.convert("RGB"))

            # 4. 保存为GIF
            if frames:
                frames[0].save(
                    output_path,
                    save_all=True,
                    append_images=frames[1:],
                    duration=100,
                    loop=0,
                    optimize=True,
                    disposal=2
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

    def _calculate_deformation(self, frame_index: int, total_frames: int) -> float:
        """ 计算头像变形系数，模拟被摸时的挤压效果。 返回1.0表示无变形，<1.0表示水平挤压。 """
        import math
        if total_frames <= 1:
            return 1.0
        progress = frame_index / (total_frames - 1)
        # 正弦波变形，中间帧挤压最明显
        deformation_factor = math.sin(progress * math.pi)
        # 映射到 0.85 - 1.0 范围（最大挤压到85%宽度）
        return 0.85 + 0.15 * (1 - deformation_factor)

    async def _cleanup_old_gifs(self):
        """ 后台任务：定期清理旧的GIF文件，防止磁盘空间无限增长。 设置为每小时运行一次。 """
        while not self._is_terminating:
            try:
                await asyncio.sleep(3600)  # 每小时运行一次
                logger.info("执行GIF清理任务...")

                # 清理超过24小时的文件
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
                # 任务被取消，正常退出
                raise
            except Exception as e:
                logger.error(f"清理任务发生错误: {e}", exc_info=True)

    # 以下为辅助方法示例，需根据实际框架API补充
    async def _get_avatar_via_framework(self, event: AstrMessageEvent) -> Optional[bytes]:
        """ 通过AstrBot框架提供的API获取用户头像。 这是一个占位方法，实际实现需要根据您使用的AstrBot版本和API文档进行调整。 """
        try:
            # 示例：假设框架在上下文中提供了用户头像获取方法
            # if hasattr(self.context, 'get_user_avatar'):
            # return await self.context.get_user_avatar(event.sender.user_id)
            # 请替换为实际的框架API调用
            logger.debug("_get_avatar_via_framework: 需根据实际框架API实现。")
            return None
        except Exception as e:
            logger.error(f"通过框架获取头像失败: {e}")
            return None