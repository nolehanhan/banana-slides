"""
OpenAI SDK implementation for image generation

Two code paths:
1. Native images API (gpt-image-2, dall-e-3, dall-e-2): uses client.images.generate /
   client.images.edit, returns b64_json directly.
2. Chat completions path (Gemini-via-proxy, etc.): uses client.chat.completions.create
   with modalities=["text","image"] and extra_body resolution hints.

Resolution validation is handled at the task_manager level for all providers.
"""
import logging
import base64
import re
import time
import requests
from io import BytesIO
from typing import Optional, List
from openai import OpenAI
from PIL import Image
from .base import ImageProvider
from config import get_config

logger = logging.getLogger(__name__)


# Models that use the native OpenAI images API (images.generate / images.edit)
# rather than the chat completions multimodal path.
_GPT_IMAGE_MODELS = {'gpt-image-1', 'gpt-image-1.5', 'gpt-image-2'}
_DALLE_MODELS = {'dall-e-2', 'dall-e-3'}
_NATIVE_IMAGES_API_MODELS = _GPT_IMAGE_MODELS | _DALLE_MODELS

# Aspect-ratio → size per model family.
# DALL-E models only support fixed sizes; gpt-image-* uses dynamic calculation.
_DALLE3_SIZE_MAP = {
    '16:9': '1792x1024',
    '9:16': '1024x1792',
    '1:1':  '1024x1024',
    '3:2':  '1792x1024',
    '2:3':  '1024x1792',
}
_DALLE2_SIZE_MAP = {
    '1:1':  '1024x1024',
}

_RESOLUTION_LONG_EDGE = {
    '1K': 1280,
    '2K': 2048,
    '4K': 3840,
}

# [自定义 2026-09-20] 单次生图最多携带的参考图张数。
# 原实现只取 ref_images[0]，其余静默丢弃（上游 bug）；现改为全量下发并按此上限截断。
# 取 8 的理由：主流 images.edit 通道单请求上限为 16 张，留一半余量给不同网关的差异。
_MAX_REF_IMAGES = 8

# [自定义 2026-09-20] 多图参考时单张图的最长边（只缩不放）。
# 单图仍沿用原有的「拉到与输出同尺寸」逻辑以保持行为不变；
# 多图若也拉成 2K RGBA PNG，8 张的请求体可达数十 MB，容易被网关按体积拒收。
_MULTI_REF_MAX_EDGE = 1024


def _compute_gpt_image_size(aspect_ratio: str, resolution: str = '2K') -> str:
    """Dynamically compute WxH for gpt-image-* from aspect ratio and resolution.

    Rules: both edges multiples of 16, max edge ≤ 3840, ratio ≤ 3:1.
    """
    parts = aspect_ratio.split(':')
    if len(parts) != 2:
        return 'auto'
    try:
        aw, ah = int(parts[0]), int(parts[1])
    except ValueError:
        return 'auto'
    if aw <= 0 or ah <= 0:
        return 'auto'

    long_edge = _RESOLUTION_LONG_EDGE.get(resolution.upper(), 2048)

    if aw >= ah:
        w = long_edge
        h = round(w * ah / aw)
    else:
        h = long_edge
        w = round(h * aw / ah)

    w = max(16, (w // 16) * 16)
    h = max(16, (h // 16) * 16)

    # Clamp total pixels to API limit (max 8,294,400)
    max_pixels = 8_294_400
    if w * h > max_pixels:
        scale = (max_pixels / (w * h)) ** 0.5
        w = max(16, (int(w * scale) // 16) * 16)
        h = max(16, (int(h * scale) // 16) * 16)

    return f'{w}x{h}'


class OpenAIImageProvider(ImageProvider):
    """
    Image generation using OpenAI SDK.

    Two code paths selected by model name:
    • Native images API (gpt-image-2 / dall-e-*): images.generate / images.edit
    • Chat completions path (Gemini via proxy, etc.): chat.completions with modalities

    Supports multiple resolution parameter formats for different providers.
    Resolution support varies by provider:
    - Some providers support 2K/4K via extra_body parameters
    - Some providers only support 1K regardless of settings
    
    The provider will try multiple parameter formats to maximize compatibility.
    """
    
    def __init__(self, api_key: str, api_base: str = None, model: str = "gemini-3-pro-image-preview", image_api_protocol: str = 'auto'):
        """
        Initialize OpenAI image provider

        Args:
            api_key: API key
            api_base: API base URL (e.g., https://aihubmix.com/v1)
            model: Model name to use
            image_api_protocol: 'auto' (detect by model name), 'images' (force images.generate), 'chat' (force chat.completions)
        """
        self.client = OpenAI(
            api_key=api_key,
            base_url=api_base,
            timeout=get_config().OPENAI_TIMEOUT,  # set timeout from config
            max_retries=get_config().OPENAI_MAX_RETRIES  # set max retries from config
        )
        self.api_base = api_base or ""
        self.model = model
        self.image_api_protocol = image_api_protocol or 'auto'
        # AIHubMix 的 /ai/v1 通道需自行带鉴权头发请求，故保留原始 key
        self.api_key = api_key
    
    def _encode_image_to_base64(self, image: Image.Image) -> str:
        """
        Encode PIL Image to base64 string
        
        Args:
            image: PIL Image object
            
        Returns:
            Base64 encoded string
        """
        buffered = BytesIO()
        # Convert to RGB if necessary (e.g., RGBA images)
        if image.mode in ('RGBA', 'LA', 'P'):
            image = image.convert('RGB')
        image.save(buffered, format="JPEG", quality=95)
        return base64.b64encode(buffered.getvalue()).decode('utf-8')
    
    def _build_extra_body(self, aspect_ratio: str, resolution: str) -> dict:
        """
        Build extra_body parameters for resolution control.
        
        Uses multiple format strategies to support different providers:
        1. Flat style: aspect_ratio + resolution at top level
        2. Nested style: generationConfig.imageConfig structure
        
        Args:
            aspect_ratio: Image aspect ratio (e.g., "16:9", "9:16")
            resolution: Image resolution ("1K", "2K", "4K")
            
        Returns:
            Dict with extra_body parameters
        """
        # Ensure resolution is uppercase (some providers require "4K" not "4k")
        resolution_upper = resolution.upper()
        
        # Build comprehensive extra_body that works with multiple providers
        extra_body = {
            # Flat style parameters
            "aspect_ratio": aspect_ratio,
            "resolution": resolution_upper,
            
            # Nested style structure (compatible with some providers)
            "generationConfig": {
                "imageConfig": {
                    "aspectRatio": aspect_ratio,
                    "imageSize": resolution_upper,
                }
            }
        }
        
        return extra_body

    def _is_native_images_api_model(self) -> bool:
        """Return True when the model should use images.generate / images.edit."""
        return self.model.lower() in _NATIVE_IMAGES_API_MODELS

    def _pil_to_png_bytes(self, image: Image.Image) -> bytes:
        buf = BytesIO()
        # Preserve alpha channel: the images.edit endpoint uses it as a mask
        if image.mode != 'RGBA':
            image = image.convert('RGBA')
        image.save(buf, format='PNG')
        buf.seek(0)
        return buf.read()

    @staticmethod
    def _shrink_for_multi_ref(image: Image.Image) -> Image.Image:
        """[自定义 2026-09-20] 多图参考时把单张等比缩到长边上限（只缩不放）。

        小图（logo / 图标）保持原样，避免被放大成模糊大图后反倒污染生成结果。
        """
        long_edge = max(image.size)
        if long_edge <= _MULTI_REF_MAX_EDGE:
            return image
        scale = _MULTI_REF_MAX_EDGE / long_edge
        new_size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        return image.resize(new_size, Image.LANCZOS)

    def _resolve_size(self, aspect_ratio: str, resolution: str = '2K') -> str:
        """Map aspect_ratio to a size string appropriate for the current model."""
        model = self.model.lower()
        if model == 'dall-e-3':
            return _DALLE3_SIZE_MAP.get(aspect_ratio, '1024x1024')
        if model == 'dall-e-2':
            return _DALLE2_SIZE_MAP.get(aspect_ratio, '1024x1024')
        return _compute_gpt_image_size(aspect_ratio, resolution)

    def _resolve_quality(self):
        """Return quality param appropriate for the current model, or None to omit."""
        model = self.model.lower()
        if model == 'dall-e-3':
            return 'standard'   # dall-e-3 only accepts standard / hd
        if model == 'dall-e-2':
            return None          # dall-e-2 has no quality param
        return 'auto'            # gpt-image-* accepts auto / low / medium / high

    def _decode_image_response(self, item) -> Image.Image:
        """Extract PIL Image from an images API response item (b64_json, url, or raw string)."""
        if isinstance(item, str):
            return self._decode_raw_string(item)
        b64 = getattr(item, 'b64_json', None)
        if b64:
            return Image.open(BytesIO(base64.b64decode(b64)))
        url = getattr(item, 'url', None)
        if url:
            with requests.get(url, timeout=60, stream=True) as resp:
                resp.raise_for_status()
                return Image.open(BytesIO(resp.content))
        if isinstance(item, dict):
            if item.get('b64_json'):
                return Image.open(BytesIO(base64.b64decode(item['b64_json'])))
            if item.get('url'):
                with requests.get(item['url'], timeout=60, stream=True) as resp:
                    resp.raise_for_status()
                    return Image.open(BytesIO(resp.content))
        raise ValueError("images API returned neither b64_json nor url")

    def _decode_raw_string(self, raw: str) -> Image.Image:
        """Try to decode a raw string as base64 image data, data-URL, or HTTP URL."""
        raw = raw.strip()
        # data:image/...;base64,...
        if raw.startswith('data:image') and ',' in raw:
            b64 = raw.split(',', 1)[1]
            return Image.open(BytesIO(base64.b64decode(b64)))
        # plain HTTP(S) URL
        if raw.startswith(('http://', 'https://')):
            with requests.get(raw, timeout=60, stream=True) as resp:
                resp.raise_for_status()
                return Image.open(BytesIO(resp.content))
        # assume raw base64
        try:
            return Image.open(BytesIO(base64.b64decode(raw)))
        except Exception:
            raise ValueError(f"Cannot decode raw string as image (len={len(raw)}, prefix={raw[:80]!r})")

    def _extract_from_images_result(self, result) -> Image.Image:
        """Defensively extract an image from images.generate / images.edit result.

        Standard OpenAI returns an ImagesResponse with .data[0].
        Proxies (newapi, one-api, etc.) may return strings, dicts, or other shapes.
        """
        # Standard path: result.data exists and is iterable
        data = getattr(result, 'data', None)
        if data is not None:
            try:
                item = data[0]
                return self._decode_image_response(item)
            except (TypeError, IndexError, AttributeError) as exc:
                logger.warning("result.data exists but extraction failed: %s", exc)

        # Proxy returned a plain string (URL or base64)
        if isinstance(result, str):
            logger.info("images API returned raw string, attempting decode")
            return self._decode_raw_string(result)

        # Proxy returned a dict (e.g. {"url": "..."} or {"b64_json": "..."})
        if isinstance(result, dict):
            logger.info("images API returned dict, attempting decode")
            if 'data' in result and isinstance(result['data'], list) and result['data']:
                return self._decode_image_response(result['data'][0])
            return self._decode_image_response(result)

        raise ValueError(f"Unexpected images API response type: {type(result)}")

    def _uses_aihubmix_images_endpoint(self) -> bool:
        """是否走 AIHubMix 的 /ai/v1 图像通道。

        该通道与标准 OpenAI images API 有三处差异，命中时走专用实现：
        ① 只认 aspect_ratio，传 size 会被上游拒；
        ② 响应是任务对象 {status, output:[...]}，不是 {data:[...]}；
        ③ 图片地址 content_url 需带 Authorization 头下载。
        """
        return '/ai/v1' in (self.api_base or '')

    @staticmethod
    def _crop_to_aspect(image: Image.Image, aspect_ratio: str) -> Image.Image:
        """按目标比例居中裁剪。

        该通道只输出方图（aspect_ratio 与 size 参数均不生效），若不先裁对比例，
        下游 task_manager 会直接 resize 到源图尺寸，把画面拉变形。
        """
        try:
            aw, ah = (int(v) for v in aspect_ratio.split(':'))
        except (AttributeError, ValueError):
            return image
        if aw <= 0 or ah <= 0:
            return image
        w, h = image.size
        target = aw / ah
        if abs(w / h - target) < 0.01:
            return image
        if w / h > target:                     # 比目标更宽 → 裁左右
            new_w = int(round(h * target))
            left = (w - new_w) // 2
            return image.crop((left, 0, left + new_w, h))
        new_h = int(round(w / target))         # 比目标更高 → 裁上下
        top = (h - new_h) // 2
        return image.crop((0, top, w, top + new_h))

    def _generate_with_aihubmix_images_api(
        self,
        prompt: str,
        ref_images: Optional[List[Image.Image]],
        aspect_ratio: str,
    ) -> Optional[Image.Image]:
        """AIHubMix /ai/v1 图像通道（豆包 Seedream / Gemini 生图仅在此通道提供）。"""
        base = self.api_base.rstrip('/')
        headers = {'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'}
        # 豆包忽略比例参数，只能靠提示词把构图往目标比例上引，生成后再裁切校正；
        # Gemini 原生认 aspect_ratio，不需要这层引导。
        is_doubao = 'doubao' in self.model.lower()
        if is_doubao and aspect_ratio and aspect_ratio != '1:1':
            prompt = f"{prompt}\n\n构图要求：按 {aspect_ratio} 比例取景，主要内容集中在画面中央。"
        payload = {
            'model': self.model,
            'prompt': prompt,
            'n': 1,
            'aspect_ratio': aspect_ratio,
        }
        if is_doubao:
            # 这两个参数只有豆包认；Gemini 会以 schema_violation 直接拒收
            payload['output_format'] = 'png'
            payload['response_format'] = 'b64_json'
        refs = list(ref_images or [])[:_MAX_REF_IMAGES]
        if len(ref_images or []) > _MAX_REF_IMAGES:
            logger.warning("%s: 收到 %d 张参考图，超过上限 %d，仅下发前 %d 张",
                           self.model, len(ref_images), _MAX_REF_IMAGES, _MAX_REF_IMAGES)
        if refs:
            # [自定义 2026-09-20] 原实现只取 ref_images[0]；单张仍传字符串（与上游文档一致），
            # 多张传数组。该通道是否接受数组需实测，失败会原样抛出 HTTP 错误。
            encoded = ['data:image/jpeg;base64,' + self._encode_image_to_base64(img) for img in refs]
            payload['image'] = encoded[0] if len(encoded) == 1 else encoded

        logger.debug("%s: AIHubMix /ai/v1, aspect_ratio=%s, refs=%d", self.model, aspect_ratio, len(refs))
        resp = requests.post(base + '/images/generations', json=payload, headers=headers,
                             timeout=get_config().OPENAI_TIMEOUT)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()

        # 实测通常同步返回 completed；若进入排队则轮询同一任务
        for _ in range(60):
            status = body.get('status')
            if status in (None, 'completed'):
                break
            if status in ('failed', 'cancelled'):
                raise RuntimeError(f"task {status}: {body.get('error')}")
            time.sleep(3)
            poll = requests.get(f"{base}/images/{body.get('id')}", headers=headers, timeout=60)
            poll.raise_for_status()
            body = poll.json()

        item = (body.get('output') or [{}])[0]
        image = None
        if item.get('b64_json'):
            image = Image.open(BytesIO(base64.b64decode(item['b64_json'])))
        elif item.get('content_url'):
            dl = requests.get(item['content_url'], headers=headers, timeout=120)
            dl.raise_for_status()
            image = Image.open(BytesIO(dl.content))
        if image is None:
            raise ValueError(f"AIHubMix 图像端点未返回图片: {str(body)[:300]}")
        return self._crop_to_aspect(image, aspect_ratio)

    def _generate_with_images_api(
        self,
        prompt: str,
        ref_images: Optional[List[Image.Image]],
        aspect_ratio: str,
        resolution: str = '2K',
    ) -> Optional[Image.Image]:
        """Use the native OpenAI images API (gpt-image-* / dall-e-*)."""
        if self._uses_aihubmix_images_endpoint():
            return self._generate_with_aihubmix_images_api(prompt, ref_images, aspect_ratio)
        size = self._resolve_size(aspect_ratio, resolution)
        quality = self._resolve_quality()
        # GPT image models always return b64_json; DALL-E models default to url
        is_dalle = self.model.lower() in _DALLE_MODELS
        response_format = 'b64_json' if is_dalle else None

        if ref_images and self.model.lower() != 'dall-e-3':
            # dall-e-3 does not support images.edit; all other native models do
            # [自定义 2026-09-20] 原实现只取 ref_images[0]，其余静默丢弃；
            # 现改为按 _MAX_REF_IMAGES 截断后**全量**下发（images.edit 的 image 参数支持列表）。
            refs = list(ref_images)
            if len(refs) > _MAX_REF_IMAGES:
                logger.warning("%s: 收到 %d 张参考图，超过上限 %d，仅下发前 %d 张",
                               self.model, len(refs), _MAX_REF_IMAGES, _MAX_REF_IMAGES)
                refs = refs[:_MAX_REF_IMAGES]
            w, h = map(int, size.split('x'))
            single = len(refs) == 1
            image_files = []
            for idx, ref_img in enumerate(refs):
                if single:
                    # 单图保持原行为：拉到与输出同尺寸，避免上游以尺寸不匹配拒收
                    if ref_img.size != (w, h):
                        ref_img = ref_img.resize((w, h), Image.LANCZOS)
                else:
                    ref_img = self._shrink_for_multi_ref(ref_img)
                image_file = BytesIO(self._pil_to_png_bytes(ref_img))
                image_file.name = 'image.png' if single else f'image_{idx}.png'
                image_files.append(image_file)
            logger.debug("%s: images.edit, size=%s, refs=%d", self.model, size, len(image_files))
            kwargs = dict(model=self.model, prompt=prompt, n=1, size=size,
                          image=image_files[0] if single else image_files)
            if quality:
                kwargs['quality'] = quality
            if response_format:
                kwargs['response_format'] = response_format
            result = self.client.images.edit(**kwargs)
        else:
            if ref_images:
                logger.warning("dall-e-3 does not support images.edit; ignoring ref_images")
            logger.debug("%s: images.generate, size=%s, quality=%s", self.model, size, quality)
            kwargs = dict(model=self.model, prompt=prompt, n=1, size=size)
            if quality:
                kwargs['quality'] = quality
            if response_format:
                kwargs['response_format'] = response_format
            result = self.client.images.generate(**kwargs)

        return self._extract_from_images_result(result)

    def generate_image(
        self,
        prompt: str,
        ref_images: Optional[List[Image.Image]] = None,
        aspect_ratio: str = "16:9",
        resolution: str = "2K",
        enable_thinking: bool = False,
        thinking_budget: int = 0
    ) -> Optional[Image.Image]:
        """
        Generate image using OpenAI SDK
        
        Supports resolution control via extra_body parameters for compatible providers.
        Note: Not all providers support 2K/4K resolution - some may return 1K regardless.
        Note: enable_thinking and thinking_budget are ignored (OpenAI format doesn't support thinking mode)
        
        The provider will:
        1. Try to use extra_body parameters (API易/AvalAI style) for resolution control
        2. Use system message for aspect_ratio as fallback
        
        Args:
            prompt: The image generation prompt
            ref_images: Optional list of reference images
            aspect_ratio: Image aspect ratio
            resolution: Image resolution ("1K", "2K", "4K") - support depends on provider
            enable_thinking: Ignored, kept for interface compatibility
            thinking_budget: Ignored, kept for interface compatibility
            
        Returns:
            Generated PIL Image object, or None if failed
        """
        try:
            # Route based on image_api_protocol setting
            use_images_api = (
                self.image_api_protocol == 'images'
                or (self.image_api_protocol == 'auto' and self._is_native_images_api_model())
            )
            if use_images_api:
                return self._generate_with_images_api(prompt, ref_images, aspect_ratio, resolution)

            # Build message content
            content = []
            
            # Add reference images first (if any)
            if ref_images:
                for ref_img in ref_images:
                    base64_image = self._encode_image_to_base64(ref_img)
                    content.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}"
                        }
                    })
            
            # Add text prompt
            content.append({"type": "text", "text": prompt})
            
            logger.debug(f"Calling OpenAI API for image generation with {len(ref_images) if ref_images else 0} reference images...")
            logger.debug(f"Config - aspect_ratio: {aspect_ratio}, resolution: {resolution}")
            
            # Build extra_body with resolution parameters for compatible providers
            extra_body = self._build_extra_body(aspect_ratio, resolution)
            extra_body["modalities"] = ["text", "image"]
            logger.debug(f"Using extra_body: {extra_body}")

            # Use both system message (for basic providers) and extra_body (for advanced providers)
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": f"aspect_ratio={aspect_ratio}, resolution={resolution}"},
                    {"role": "user", "content": content},
                ],
                modalities=["text", "image"],
                extra_body=extra_body
            )
            
            logger.debug("OpenAI API call completed")
            
            # Extract image from response - handle different response formats
            message = response.choices[0].message

            # Debug: log available attributes
            logger.debug(f"Response message attributes: {dir(message)}")

            # Try message.images first (OpenRouter format)
            images_attr = getattr(message, 'images', None)
            if images_attr:
                for img_item in images_attr:
                    url = None
                    if isinstance(img_item, dict):
                        url = img_item.get('image_url', {}).get('url', '')
                    elif hasattr(img_item, 'image_url'):
                        iu = img_item.image_url
                        url = iu.get('url', '') if isinstance(iu, dict) else getattr(iu, 'url', '')
                    if url and url.startswith('data:image'):
                        base64_data = url.split(',', 1)[1]
                        image = Image.open(BytesIO(base64.b64decode(base64_data)))
                        logger.debug(f"Extracted image from message.images: {image.size}")
                        return image

            # Try multi_mod_content (custom format from some proxies)
            if hasattr(message, 'multi_mod_content') and message.multi_mod_content:
                parts = message.multi_mod_content
                for part in parts:
                    if "text" in part:
                        logger.debug(f"Response text: {part['text'][:100] if len(part['text']) > 100 else part['text']}")
                    if "inline_data" in part:
                        image_data = base64.b64decode(part["inline_data"]["data"])
                        image = Image.open(BytesIO(image_data))
                        logger.debug(f"Successfully extracted image: {image.size}, {image.mode}")
                        return image
            
            # Try standard OpenAI content format (list of content parts)
            if hasattr(message, 'content') and message.content:
                # If content is a list (multimodal response)
                if isinstance(message.content, list):
                    for part in message.content:
                        if isinstance(part, dict):
                            # Handle image_url type
                            if part.get('type') == 'image_url':
                                image_url = part.get('image_url', {}).get('url', '')
                                if image_url.startswith('data:image'):
                                    # Extract base64 data from data URL
                                    base64_data = image_url.split(',', 1)[1]
                                    image_data = base64.b64decode(base64_data)
                                    image = Image.open(BytesIO(image_data))
                                    logger.debug(f"Successfully extracted image from content: {image.size}, {image.mode}")
                                    return image
                            # Handle text type
                            elif part.get('type') == 'text':
                                text = part.get('text', '')
                                if text:
                                    logger.debug(f"Response text: {text[:100] if len(text) > 100 else text}")
                        elif hasattr(part, 'type'):
                            # Handle as object with attributes
                            if part.type == 'image_url':
                                image_url = getattr(part, 'image_url', {})
                                if isinstance(image_url, dict):
                                    url = image_url.get('url', '')
                                else:
                                    url = getattr(image_url, 'url', '')
                                if url.startswith('data:image'):
                                    base64_data = url.split(',', 1)[1]
                                    image_data = base64.b64decode(base64_data)
                                    image = Image.open(BytesIO(image_data))
                                    logger.debug(f"Successfully extracted image from content object: {image.size}, {image.mode}")
                                    return image
                # If content is a string, try to extract image from it
                elif isinstance(message.content, str):
                    content_str = message.content
                    logger.debug(f"Response content (string): {content_str[:200] if len(content_str) > 200 else content_str}")
                    
                    # Try to extract Markdown image URL: ![...](url)
                    markdown_pattern = r'!\[.*?\]\((https?://[^\s\)]+)\)'
                    markdown_matches = re.findall(markdown_pattern, content_str)
                    if markdown_matches:
                        image_url = markdown_matches[0]  # Use the first image URL found
                        logger.debug(f"Found Markdown image URL: {image_url}")
                        try:
                            response = requests.get(image_url, timeout=30, stream=True)
                            response.raise_for_status()
                            image = Image.open(BytesIO(response.content))
                            image.load()  # Ensure image is fully loaded
                            logger.debug(f"Successfully downloaded image from Markdown URL: {image.size}, {image.mode}")
                            return image
                        except Exception as download_error:
                            logger.warning(f"Failed to download image from Markdown URL: {download_error}")
                    
                    # Try to extract plain URL (not in Markdown format)
                    url_pattern = r'(https?://[^\s\)\]]+\.(?:png|jpg|jpeg|gif|webp|bmp)(?:\?[^\s\)\]]*)?)'
                    url_matches = re.findall(url_pattern, content_str, re.IGNORECASE)
                    if url_matches:
                        image_url = url_matches[0]
                        logger.debug(f"Found plain image URL: {image_url}")
                        try:
                            response = requests.get(image_url, timeout=30, stream=True)
                            response.raise_for_status()
                            image = Image.open(BytesIO(response.content))
                            image.load()
                            logger.debug(f"Successfully downloaded image from plain URL: {image.size}, {image.mode}")
                            return image
                        except Exception as download_error:
                            logger.warning(f"Failed to download image from plain URL: {download_error}")
                    
                    # Try to extract base64 data URL from string
                    base64_pattern = r'data:image/[^;]+;base64,([A-Za-z0-9+/=]+)'
                    base64_matches = re.findall(base64_pattern, content_str)
                    if base64_matches:
                        base64_data = base64_matches[0]
                        logger.debug(f"Found base64 image data in string")
                        try:
                            image_data = base64.b64decode(base64_data)
                            image = Image.open(BytesIO(image_data))
                            logger.debug(f"Successfully extracted base64 image from string: {image.size}, {image.mode}")
                            return image
                        except Exception as decode_error:
                            logger.warning(f"Failed to decode base64 image from string: {decode_error}")
            
            # Log raw response for debugging
            logger.warning(f"Unable to extract image. Raw message type: {type(message)}")
            logger.warning(f"Message content type: {type(getattr(message, 'content', None))}")
            raw = str(getattr(message, 'content', 'N/A'))
            logger.warning(f"Message content: {raw[:300]}{'...(truncated)' if len(raw) > 300 else ''}")
            logger.warning(f"Message all attrs: {vars(message) if hasattr(message, '__dict__') else dir(message)}"[:500])
            
            raise ValueError("No valid multimodal response received from OpenAI API")
            
        except Exception as e:
            error_detail = f"Error generating image with OpenAI (model={self.model}): {type(e).__name__}: {str(e)}"
            logger.error(error_detail, exc_info=True)
            raise Exception(error_detail) from e
