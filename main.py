from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.all import *
from astrbot.core.utils.io import download_file
from io import BytesIO
import os
import time
import random
from PIL import Image as PILImage
from pathlib import Path
from collections import deque
from typing import Optional, Tuple, Dict
import json


@register("avatar_describer", "nichinichisou", "识别用户头像，并可被画图等插件联动引用", "1.0.0")
class AvatarDescriber(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)

        # 机器人 QQ 号
        self.robot_id = config.get("robot_self_id", "")

        # 最大缓存图片数
        self.max_cached_images = config.get("max_cached_images", 5)

        # 直接读取 AstrBot 全局配置中的“默认图片转述模型”
        astrbot_config = context.astrbot_config
        self.default_image_caption_provider_id = (
            astrbot_config.get("provider_settings", {})
            .get("default_image_caption_provider_id", "")
        )
        logger.info(
            f"avatar_describer 初始化，系统默认图片转述模型ID为: {self.default_image_caption_provider_id or '未设置'}"
        )

        # 临时文件目录
        shared_data_path = Path(__file__).resolve().parent.parent.parent
        self.temp_dir = os.path.join(shared_data_path, "avatar_describer_temp")
        os.makedirs(self.temp_dir, exist_ok=True)

        # 图片缓存容器（结构与画图插件一致）
        self.image_history_cache = {}
        self.desc_cache = {}

        if not self.robot_id:
            logger.warning("avatar_describer: 未配置 robot_self_id，头像无法被画图插件引用。")

    def store_avatar_to_bot_history(self, group_id: str, image_path: str, original_filename: Optional[str] = None):
        """将头像图片以机器人身份存入缓存，供其他插件通过 reference_bot 引用。"""
        if not self.robot_id:
            return
        key = (str(self.robot_id), str(group_id))
        if key not in self.image_history_cache:
            self.image_history_cache[key] = deque(maxlen=self.max_cached_images)
        self.image_history_cache[key].append((image_path, original_filename))
        logger.info(f"头像已缓存为机器人图片: {image_path} -> 群/会话 {group_id} (总数 {len(self.image_history_cache[key])})")

    async def download_avatar(self, user_id: str) -> Optional[str]:
        """根据 QQ 号下载头像，返回本地文件路径。"""
        avatar_url = f"http://q.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640"
        ext = ".png"
        filename = f"avatar_{user_id}_{int(time.time())}_{random.randint(1000,9999)}{ext}"
        filepath = os.path.join(self.temp_dir, filename)
        try:
            await download_file(url=avatar_url, path=filepath, show_progress=False)
            return filepath
        except Exception as e:
            logger.error(f"下载头像失败: {avatar_url} -> {e}", exc_info=True)
            return None

    @filter.llm_tool(name="describe_user_avatar")
    async def describe_user_avatar(self, event: AstrMessageEvent) -> str:
        '''
        获取当前聊天用户的头像并进行内容识别，返回头像的文字描述。
        该工具同时会将头像图片缓存，以便后续调用绘图工具（如 gemini_draw）时通过
        reference_bot=True 及 image_index 引用该头像作为生图参考。
        返回格式：JSON，包含 "description" 字段（中文描述）与 "cached" 字段（是否缓存成功）。
        若识别失败也会返回错误提示。
        '''
        user_id = event.get_sender_id()
        group_id = event.get_group_id() if hasattr(event, "message_obj") and event.message_obj.group_id else user_id

        # 1. 下载头像
        avatar_path = await self.download_avatar(user_id)
        if not avatar_path:
            return json.dumps({"description": "无法获取用户头像，请稍后重试。", "cached": False}, ensure_ascii=False)

        # 2. 优先使用系统默认图片转述模型
        provider = None
        if self.default_image_caption_provider_id:
            provider = self.context.get_provider_by_id(self.default_image_caption_provider_id)
            if not provider:
                logger.warning(f"系统默认图片转述模型配置有误或未找到: {self.default_image_caption_provider_id}")

        # 3. 识图
        if not provider:
            description = "无法调用视觉模型，请检查系统配置中的“默认图片转述模型”。"
        else:
            prompt = "请用简洁的中文描述这张头像图片的内容，包括主要元素、风格、角色特征等。不要超过100字。"
            try:
                llm_resp = await provider.text_chat(
                    prompt=prompt,
                    image_urls=[avatar_path]
                )
                description = llm_resp.completion_text.strip() if llm_resp and hasattr(llm_resp, "completion_text") else "识别失败"
            except Exception as e:
                error_msg = str(e)
                logger.error(f"识图调用失败: {error_msg}", exc_info=True)
                # 针对 Gemini 安全策略拦截给出友好提示
                if "违反 Gemini 平台政策" in error_msg or "SAFETY" in error_msg.upper():
                    description = "头像识别暂时被安全策略拦截，请稍后再试或换个头像~"
                else:
                    description = "图像识别出错，请稍后重试。"

        # 4. 缓存头像（供画图工具引用）
        cached = False
        if self.robot_id and avatar_path and os.path.exists(avatar_path):
            self.store_avatar_to_bot_history(str(group_id), avatar_path, f"avatar_{user_id}.png")
            cached = True

        return json.dumps({
            "description": description,
            "cached": cached
        }, ensure_ascii=False, default=str)
