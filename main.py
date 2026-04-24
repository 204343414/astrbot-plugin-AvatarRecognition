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

    # 指定的识图 Provider ID（例如 "gemini_default_source/gemini-3.1-flash-lite-preview"）
    self.image_desc_provider_id = config.get("image_desc_provider", "").strip()

    # 临时文件目录
    shared_data_path = Path(__file__).resolve().parent.parent.parent
    self.temp_dir = os.path.join(shared_data_path, "avatar_describer_temp")
    os.makedirs(self.temp_dir, exist_ok=True)

    # 图片缓存容器
    from collections import deque
    self.image_history_cache = {}
    self.desc_cache = {}

    # 提示日志
    if not self.robot_id:
        logger.warning("avatar_describer: 未配置 robot_self_id，头像无法被画图插件引用。")
    if not self.image_desc_provider_id:
        logger.info("avatar_describer: 未配置专门的识图 Provider，将回退到当前对话模型。")
    else:
        logger.info(f"avatar_describer: 使用识图 Provider: {self.image_desc_provider_id}")

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

        # 1. 尝试下载头像
        avatar_path = await self.download_avatar(user_id)
        if not avatar_path:
            return json.dumps({"description": "无法获取用户头像，请稍后重试。", "cached": False}, ensure_ascii=False)

        # 2. 调用当前 LLM provider 进行识图
        try:
            # 原来的 provider 获取行（删除掉）
# provider = self.context.get_using_provider(umo=event.unified_msg_origin)

# 替换为：
provider = None
if self.image_desc_provider_id:
        # 优先使用配置的识图专用 provider
        provider = self.context.get_provider_by_id(self.image_desc_provider_id)
        if not provider:
            logger.warning(f"未找到配置的识图 Provider: {self.image_desc_provider_id}，尝试回退到当前对话模型。")
    
    if not provider:
        # 如果没有配置或是没找到，则回退到当前对话模型（可能不支持多模态）
        provider = self.context.get_using_provider(umo=event.unified_msg_origin)
    
    if not provider:
        # 仍然没有，则只能用文字返回错误
        description = "（无法调用视觉模型，请检查 avatar_describer 插件的 image_desc_provider 配置）"
    else:
        prompt = "请用简洁的中文描述这张头像图片的内容，包括主要元素、风格、角色特征等。不要超过100字。"
        try:
            llm_resp = await provider.text_chat(
                prompt=prompt,
                image_urls=[avatar_path]
            )
            description = llm_resp.completion_text.strip() if llm_resp and hasattr(llm_resp, "completion_text") else "识别失败"
        except Exception as e:
            logger.error(f"识图调用失败: {e}", exc_info=True)
            description = "图像识别出错，请稍后重试。"
            # 即便识别失败，仍可缓存头像
            # 但不缓存可能合适的路径，这里可以选择缓存原图
        finally:
            # 3. 将头像加入机器人图片历史（供画图工具引用）
            if self.robot_id and avatar_path and os.path.exists(avatar_path):
                self.store_avatar_to_bot_history(str(group_id), avatar_path, f"avatar_{user_id}.png")
                cached = True
            else:
                cached = False

        return json.dumps({
            "description": description,
            "cached": cached
        }, ensure_ascii=False, default=str)
