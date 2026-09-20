"""图片生成的多源轮询与失败兜底（自定义扩展，2026-09-20 加入）。

背景
----
``get_image_provider()`` 原本只按 ``IMAGE_*`` 配置构造**单一** provider：某个源的
额度耗尽（429）、密钥失效（401/403）或模型下架/不存在（404）时，整条生成链路
直接失败，页面上就是「生成失败」。要同时用上多家平台的免费额度，需要「按顺序
多接几个源，前面不行自动用后面的」。

设计
----
1. **候选列表**：第 0 个候选就是原有 ``IMAGE_*`` 配置（向后兼容——不配任何
   ``IMAGE_FALLBACK_n_*`` 时，本模块不介入，行为与改动前完全一致）；其后按序号
   读取 ``IMAGE_FALLBACK_1_*``、``IMAGE_FALLBACK_2_*`` …（最多 9 个）。
2. **调度策略**：
   - ``priority``（默认）：永远优先用第 0 个，它不行才往下走。同一份 PPT 的
     画风更统一。
   - ``round_robin``：每次调用轮换起点，把用量摊到所有源上，额度叠加最大化。
3. **失败分类**：从异常链里取 HTTP 状态码 + 关键字，区分
   「额度耗尽 / 鉴权失败 / 模型不存在」（长冷却，不再白打）、
   「瞬时故障 / 参数错 / 未知」（累计到阈值才短冷却）、
   「内容被安全策略拦截」（不冷却，换下一个源碰运气）。
4. **冷却登记**：按候选 label 记账，线程安全（生图有多个并发 worker）；默认落盘到
   ``instance/fallback_state.json``，容器重启后仍记得哪个源已耗尽。
5. **全冷却时的降级**：所有源都在冷却期时，只挑**最快解冻**的那一个试一次，
   而不是把每个冷却中的源都白打一遍。

⚠️ 边界：本模块只解决「源不可用」，**不解决「画得不好」**。画质问题换源解决不了。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

# 本模块位于 services/ai_providers/fallback.py（包根），故用 .image.* 引用子包
from .image import ImageProvider

logger = logging.getLogger(__name__)

__all__ = [
    'QUOTA', 'AUTH', 'MODEL_MISSING', 'BAD_REQUEST', 'TRANSIENT', 'CONTENT', 'UNKNOWN',
    'classify', 'Candidate', 'FallbackImageProvider',
    'maybe_wrap_image_provider', 'get_registry', 'reset_runtime_state',
]

# ==========================================================================
# 一、失败分类
# ==========================================================================

QUOTA = 'quota'                # 额度/频率耗尽（429 / 402 / 余额不足）
AUTH = 'auth'                  # 密钥无效、无权限（401 / 403）
MODEL_MISSING = 'model_missing'  # 端点没有这个模型（404）
BAD_REQUEST = 'bad_request'    # 参数被上游拒（400 / 422）
TRANSIENT = 'transient'        # 瞬时故障：超时、网络、5xx
CONTENT = 'content'            # 内容被安全策略拦截
UNKNOWN = 'unknown'

# 长冷却：这类失败不会因为等几秒就好，继续试等于白打（也白花钱）
LONG_COOLDOWN_KINDS = (QUOTA, AUTH, MODEL_MISSING)
# 短冷却：累计失败到阈值才冷却，避免偶发抖动就把源摘掉
SHORT_COOLDOWN_KINDS = (TRANSIENT, UNKNOWN, BAD_REQUEST)

_KW_QUOTA = (
    'rate limit', 'ratelimit', 'too many requests', 'quota', 'insufficient balance',
    'insufficient credit', 'insufficient fund', 'exceeded', 'exceed your',
    'out of credits', 'no credits', 'billing', 'payment required', 'free tier',
    '额度', '余额', '欠费', '限流', '频率限制',
)
_KW_AUTH = (
    'unauthorized', 'invalid api key', 'incorrect api key', 'invalid_api_key',
    'api key', 'permission', 'forbidden', 'not allowed', 'authentication',
    '鉴权', '密钥', '无权限', '未授权',
)
_KW_MODEL_MISSING = (
    'model_not_found', 'model not found', 'no such model', 'unknown model',
    'invalid model', 'does not exist', 'decommission', 'retired',
    '模型不存在', '不支持的模型', '模型名错误', '已退役', '已下线',
)
_KW_TRANSIENT = (
    'timeout', 'timed out', 'deadline exceeded', 'connection', 'connect error',
    'temporarily', 'unavailable', 'overloaded', 'server error', 'bad gateway',
    'reset by peer', 'remote end closed', '超时', '网络', '连接',
)
_KW_CONTENT = (
    'content policy', 'content_policy', 'content filter', 'safety', 'sensitive',
    'moderation', 'blocked', 'violat', 'nsfw', '审核', '敏感', '违规', '安全策略',
)

_CODE_RE = re.compile(r'\b([45]\d{2})\b')


def _walk_chain(exc: BaseException):
    """遍历异常链（含 __cause__ / __context__），带环保护。"""
    seen, cur = set(), exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def _collect_status_codes(exc: BaseException) -> set:
    """从异常链的属性与文本里收集 HTTP 状态码。

    OpenAI SDK 会把状态码挂在异常的 ``status_code`` / ``response.status_code``；
    本项目的 AIHubMix 专用通道用的是 ``RuntimeError("HTTP 429: ...")``，只能从文本里抠。
    """
    codes = set()
    for e in _walk_chain(exc):
        for attr in ('status_code', 'status', 'http_status', 'code'):
            val = getattr(e, attr, None)
            if isinstance(val, int) and 400 <= val <= 599:
                codes.add(val)
        resp = getattr(e, 'response', None)
        val = getattr(resp, 'status_code', None)
        if isinstance(val, int) and 400 <= val <= 599:
            codes.add(val)
    for m in _CODE_RE.findall(_collect_text(exc)):
        codes.add(int(m))
    return codes


def _collect_text(exc: BaseException) -> str:
    """把异常链的类名与消息拼成一段小写文本，供关键字匹配。"""
    parts: List[str] = []
    for e in _walk_chain(exc):
        parts.append(type(e).__name__)
        parts.append(str(e))
        for attr in ('body', 'message', 'detail'):
            val = getattr(e, attr, None)
            if isinstance(val, (str, bytes)):
                parts.append(val.decode('utf-8', 'ignore') if isinstance(val, bytes) else val)
            elif val is not None and not isinstance(val, (int, float, bool)):
                try:
                    parts.append(json.dumps(val, ensure_ascii=False))
                except Exception:
                    pass
    return ' '.join(parts).lower()


def classify(exc: BaseException) -> str:
    """判断一次失败属于哪一类，决定「冷却换源」还是「直接换源再试」。

    判定顺序：模型不存在 → 额度 → 鉴权 → 内容拦截 → 瞬时 → 参数错 → 未知。
    先看 HTTP 状态码（可靠），再看关键字（兜底）。
    """
    codes = _collect_status_codes(exc)
    text = _collect_text(exc)

    def has(keywords: Tuple[str, ...]) -> bool:
        return any(k in text for k in keywords)

    if 404 in codes or has(_KW_MODEL_MISSING):
        return MODEL_MISSING
    if {402, 429} & codes or has(_KW_QUOTA):
        return QUOTA
    if {401, 403} & codes or has(_KW_AUTH):
        return AUTH
    if has(_KW_CONTENT):
        return CONTENT
    if {408, 409, 425, 500, 502, 503, 504} & codes or has(_KW_TRANSIENT):
        return TRANSIENT
    if {400, 422} & codes:
        return BAD_REQUEST
    return UNKNOWN


# ==========================================================================
# 二、冷却登记表
# ==========================================================================

def _default_state_path() -> str:
    """默认状态文件：backend/instance/fallback_state.json（该目录已做持久化挂载）。

    本文件位于 backend/services/ai_providers/fallback.py，往上三层即 backend/。
    """
    backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(backend_dir, 'instance', 'fallback_state.json')


class CoolDownRegistry:
    """按候选 label 记录冷却状态与成败计数（线程安全，可选落盘）。

    只持久化「冷却截止时间」这一件事——它跨重启有意义；成败计数属于本次会话的
    观测数据，重启后清零即可。
    """

    def __init__(self, path: Optional[str] = None):
        self._lock = threading.Lock()
        self._states: Dict[str, Dict[str, Any]] = {}
        self._path = path if path is not None else _default_state_path()
        self._load()

    # -- 内部 ---------------------------------------------------------------
    def _state(self, label: str) -> Dict[str, Any]:
        st = self._states.get(label)
        if st is None:
            st = {'cooldown_until': 0.0, 'reason': '', 'successes': 0,
                  'failures': 0, 'consecutive_failures': 0, 'last_error': ''}
            self._states[label] = st
        return st

    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path, 'r', encoding='utf-8') as fp:
                saved = json.load(fp)
            now = time.time()
            for label, item in (saved.get('cooldowns') or {}).items():
                until = float(item.get('until') or 0)
                if until > now:  # 已过期的直接丢弃
                    st = self._state(label)
                    st['cooldown_until'] = until
                    st['reason'] = str(item.get('reason') or '')
            alive = {k: v for k, v in self._states.items()
                     if v['cooldown_until'] > now}
            self._states = alive
            if alive:
                logger.info("已恢复 %d 个源的冷却状态：%s",
                            len(alive), {k: round(v['cooldown_until'] - now) for k, v in alive.items()})
        except Exception as exc:  # 状态文件坏了不影响生产
            logger.warning("读取兜底状态文件失败（忽略）：%s", exc)

    def _save(self) -> None:
        if not self._path:
            return
        try:
            now = time.time()
            data = {
                'updated_at': now,
                'cooldowns': {
                    label: {'until': st['cooldown_until'], 'reason': st['reason']}
                    for label, st in self._states.items()
                    if st['cooldown_until'] > now
                },
            }
            tmp = self._path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception as exc:
            logger.warning("写入兜底状态文件失败（忽略）：%s", exc)

    # -- 对外 ---------------------------------------------------------------
    def is_cooling(self, label: str) -> bool:
        with self._lock:
            return self._state(label)['cooldown_until'] > time.time()

    def remaining(self, label: str) -> float:
        with self._lock:
            return max(0.0, self._state(label)['cooldown_until'] - time.time())

    def cooldown_reason(self, label: str) -> str:
        with self._lock:
            st = self._state(label)
            return st['reason'] if st['cooldown_until'] > time.time() else ''

    def record_success(self, label: str) -> None:
        with self._lock:
            st = self._state(label)
            st['successes'] += 1
            st['consecutive_failures'] = 0
            if st['cooldown_until']:
                st['cooldown_until'] = 0.0
                st['reason'] = ''
                self._save()

    def record_failure(self, label: str, kind: str, *,
                       long_cooldown: float, short_cooldown: float,
                       threshold: int) -> None:
        now = time.time()
        with self._lock:
            st = self._state(label)
            st['failures'] += 1
            st['consecutive_failures'] += 1
            st['last_error'] = kind
            changed = False
            if kind in LONG_COOLDOWN_KINDS:
                st['cooldown_until'] = max(st['cooldown_until'], now + long_cooldown)
                st['reason'] = kind
                st['consecutive_failures'] = 0
                changed = True
            elif kind in SHORT_COOLDOWN_KINDS and st['consecutive_failures'] >= threshold:
                st['cooldown_until'] = max(st['cooldown_until'], now + short_cooldown)
                st['reason'] = kind
                st['consecutive_failures'] = 0
                changed = True
            if changed:
                self._save()

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """给调试/状态接口看的快照。"""
        now = time.time()
        with self._lock:
            return {
                label: {
                    'cooling': st['cooldown_until'] > now,
                    'remaining_seconds': max(0, round(st['cooldown_until'] - now)),
                    'reason': st['reason'],
                    'successes': st['successes'],
                    'failures': st['failures'],
                }
                for label, st in self._states.items()
            }

    def clear(self) -> None:
        with self._lock:
            self._states.clear()
            self._save()


_registry: Optional[CoolDownRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> CoolDownRegistry:
    """取全局冷却登记表（惰性初始化，供状态查询与测试使用）。"""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = CoolDownRegistry()
        return _registry


def reset_runtime_state() -> None:
    """清空冷却状态（测试与「手动重试全部源」用）。"""
    get_registry().clear()


# ==========================================================================
# 三、候选与包装 provider
# ==========================================================================

@dataclass
class Candidate:
    """一个可用的图片源。"""
    label: str
    provider: ImageProvider
    source: str = ''      # 展示用：格式/厂商
    model: str = ''
    api_base: str = ''

    def describe(self) -> str:
        base = self.api_base.replace('https://', '').replace('http://', '').rstrip('/')
        return f"{self.label}[{self.source}] {self.model or '-'} @ {base or '-'}"


class FallbackImageProvider(ImageProvider):
    """把多个单源 provider 串成「按顺序尝试 + 失败冷却换源」的一层。

    对上层完全透明：只实现 ``generate_image``，签名与单源 provider 一致。
    """

    def __init__(self,
                 candidates: List[Candidate],
                 *,
                 strategy: str = 'priority',
                 max_attempts: int = 0,
                 quota_cooldown: float = 8 * 3600,
                 error_cooldown: float = 600.0,
                 failure_threshold: int = 3,
                 registry: Optional[CoolDownRegistry] = None):
        if not candidates:
            raise ValueError("FallbackImageProvider 至少需要一个候选源")
        self.candidates = candidates
        self.strategy = (strategy or 'priority').strip().lower()
        self.max_attempts = max(0, int(max_attempts or 0))
        self.quota_cooldown = float(quota_cooldown)
        self.error_cooldown = float(error_cooldown)
        self.failure_threshold = max(1, int(failure_threshold or 1))
        self._registry = registry or get_registry()
        self._rr = 0
        self._rr_lock = threading.Lock()
        logger.info("图片多源兜底已启用（策略=%s，共 %d 个源）：%s",
                    self.strategy, len(candidates),
                    ' → '.join(c.describe() for c in candidates))

    # -- 调度 ---------------------------------------------------------------
    def _ordered(self) -> List[Candidate]:
        n = len(self.candidates)
        if self.strategy != 'round_robin':
            return list(self.candidates)
        with self._rr_lock:
            start = self._rr % n
            self._rr += 1
        return self.candidates[start:] + self.candidates[:start]

    def _plan(self) -> Tuple[List[Candidate], bool]:
        """产出本次要尝试的候选顺序。

        返回 ``(候选列表, 是否处于「全部冷却」降级态)``。
        """
        ordered = self._ordered()
        ready = [c for c in ordered if not self._registry.is_cooling(c.label)]
        if self.max_attempts > 0:
            ready = ready[:self.max_attempts]
        if ready:
            return ready, False
        # 全部冷却：只挑最快解冻的那个试一次，避免每页把冷却中的源全白打一遍
        thaw = sorted(ordered, key=lambda c: self._registry.remaining(c.label))
        return thaw[:1], True

    # -- 主入口 -------------------------------------------------------------
    def generate_image(self, prompt: str,
                       ref_images: Optional[List[Image.Image]] = None,
                       aspect_ratio: str = "16:9",
                       resolution: str = "2K",
                       enable_thinking: bool = False,
                       thinking_budget: int = 0) -> Optional[Image.Image]:
        plan, degraded = self._plan()
        if degraded and plan:
            logger.warning("图片源全部处于冷却期，降级尝试最快解冻的 %s（约 %.0f 秒后解冻，原因=%s）",
                           plan[0].label, self._registry.remaining(plan[0].label),
                           self._registry.cooldown_reason(plan[0].label) or 'unknown')

        errors: List[str] = []
        for idx, cand in enumerate(plan):
            try:
                img = cand.provider.generate_image(
                    prompt=prompt,
                    ref_images=ref_images,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    enable_thinking=enable_thinking,
                    thinking_budget=thinking_budget,
                )
            except Exception as exc:
                kind = classify(exc)
                self._registry.record_failure(
                    cand.label, kind,
                    long_cooldown=self.quota_cooldown,
                    short_cooldown=self.error_cooldown,
                    threshold=self.failure_threshold,
                )
                cooling = self._registry.remaining(cand.label)
                logger.warning("图片源 %s 失败（%s%s）：%s",
                               cand.label, kind,
                               f"，已冷却 {cooling:.0f} 秒" if cooling else '',
                               str(exc)[:220])
                errors.append(f"{cand.label}[{kind}]: {str(exc)[:160]}")
                continue

            if img is None:
                self._registry.record_failure(
                    cand.label, TRANSIENT,
                    long_cooldown=self.quota_cooldown,
                    short_cooldown=self.error_cooldown,
                    threshold=self.failure_threshold,
                )
                logger.warning("图片源 %s 返回空图，换下一个源", cand.label)
                errors.append(f"{cand.label}[empty]: 返回空图")
                continue

            self._registry.record_success(cand.label)
            if idx > 0:
                logger.info("兜底成功：前 %d 个源不可用，已由 %s 完成本页生成", idx, cand.label)
            return img

        detail = '；'.join(errors) if errors else '没有可尝试的源'
        raise RuntimeError(f"所有图片源均失败（共试 {len(plan)} 个）：{detail}")


# ==========================================================================
# 四、从配置构造候选链
# ==========================================================================

_MAX_SLOTS = 9
_FALSY = ('false', '0', 'no', 'off', 'none')
_TRUTHY = ('true', '1', 'yes', 'on')


def _as_bool(raw: Optional[str], default: bool) -> bool:
    if raw is None or str(raw).strip() == '':
        return default
    val = str(raw).strip().lower()
    if val in _TRUTHY:
        return True
    if val in _FALSY:
        return False
    return default


def _as_float(raw: Optional[str], default: float) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _as_int(raw: Optional[str], default: int) -> int:
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def _create_single_provider(spec: Dict[str, Any]) -> ImageProvider:
    """按显式配置构造一个单源 provider。

    只支持真正能文生图的格式：openai 兼容端点（含火山等第三方）、gemini、LazyLLM 厂商。
    """
    from . import LAZYLLM_VENDORS  # 惰性导入，避免包初始化期的循环依赖
    from .image import OpenAIImageProvider, GenAIImageProvider, LazyLLMImageProvider

    fmt = (spec.get('source') or 'openai').strip().lower()
    model = spec.get('model') or ''
    api_key = spec.get('api_key') or ''
    api_base = spec.get('api_base') or ''

    if fmt in ('openai', 'volcengine'):
        if not api_key:
            raise ValueError("缺少 API_KEY")
        return OpenAIImageProvider(
            api_key=api_key,
            api_base=api_base or None,
            model=model,
            image_api_protocol=spec.get('protocol') or 'auto',
        )
    if fmt == 'gemini':
        if not api_key:
            raise ValueError("缺少 API_KEY")
        return GenAIImageProvider(api_key=api_key, api_base=api_base or None, model=model)
    if fmt == 'lazyllm' or fmt in LAZYLLM_VENDORS:
        vendor = fmt if fmt in LAZYLLM_VENDORS else (spec.get('vendor') or 'doubao')
        return LazyLLMImageProvider(source=vendor, model=model)

    raise ValueError(f"不支持的生图格式 '{fmt}'（可用：openai / gemini / "
                     f"{'/'.join(sorted(LAZYLLM_VENDORS))}）")


def _primary_candidate(provider: ImageProvider, model: str, resolve) -> Candidate:
    label = resolve('IMAGE_FALLBACK_PRIMARY_LABEL') or f"primary({model or 'default'})"
    return Candidate(
        label=label,
        provider=provider,
        source=(resolve('IMAGE_MODEL_SOURCE') or resolve('AI_PROVIDER_FORMAT') or 'openai').lower(),
        model=model or '',
        api_base=resolve('IMAGE_API_BASE') or resolve('OPENAI_API_BASE') or '',
    )


def _extra_candidates(resolve) -> List[Candidate]:
    """读取 IMAGE_FALLBACK_1_* … IMAGE_FALLBACK_9_*，构造备用候选。

    单个备用源配置有误只跳过它自己并告警，不影响其他源与主源。
    """
    out: List[Candidate] = []
    for i in range(1, _MAX_SLOTS + 1):
        prefix = f'IMAGE_FALLBACK_{i}_'
        model = resolve(prefix + 'MODEL')
        api_base = resolve(prefix + 'API_BASE')
        api_key = resolve(prefix + 'API_KEY')
        vendor = resolve(prefix + 'VENDOR')
        source = resolve(prefix + 'SOURCE')

        if not any((model, api_base, api_key, vendor)):
            continue  # 整块没配，跳过（不告警，属正常情况）

        label = resolve(prefix + 'LABEL') or f'fallback{i}'
        if not _as_bool(resolve(prefix + 'ENABLED'), True):
            logger.info("备用图片源 %s 已被 ENABLED=false 禁用，跳过", label)
            continue
        if not model:
            logger.warning("备用图片源 %s 未配置 %sMODEL，已跳过", label, prefix)
            continue

        spec = {
            'source': source or ('lazyllm' if vendor else 'openai'),
            'vendor': vendor,
            'api_key': api_key,
            'api_base': api_base,
            # 未单独指定时继承全局协议设置（gpt-image-2-free 依赖它为 images）
            'protocol': resolve(prefix + 'PROTOCOL') or resolve('OPENAI_IMAGE_API_PROTOCOL') or 'auto',
            'model': model,
        }
        try:
            provider = _create_single_provider(spec)
        except Exception as exc:
            logger.error("备用图片源 %s 构造失败，已跳过：%s", label, exc)
            continue

        out.append(Candidate(label=label, provider=provider, source=spec['source'],
                             model=model, api_base=api_base or ''))
        logger.info("已登记备用图片源 #%d：%s", i, out[-1].describe())
    return out


def maybe_wrap_image_provider(provider: ImageProvider, model: str) -> ImageProvider:
    """如果配了备用源，就把单源 provider 包成多源兜底；否则原样返回。

    本函数**绝不抛异常影响主流程**：任何配置错误都降级为「不加兜底，用原来的单源」。
    """
    try:
        from . import _resolve_setting  # 惰性导入，避免循环依赖

        enabled_raw = _resolve_setting('IMAGE_FALLBACK_ENABLED') or 'auto'
        enabled = _as_bool(enabled_raw, True)
        explicitly_on = str(enabled_raw).strip().lower() in _TRUTHY

        if not enabled:
            logger.info("图片多源兜底已关闭（IMAGE_FALLBACK_ENABLED=%s）", enabled_raw)
            return provider

        candidates = [_primary_candidate(provider, model, _resolve_setting)]
        candidates += _extra_candidates(_resolve_setting)

        if len(candidates) < 2:
            if explicitly_on:
                logger.warning("IMAGE_FALLBACK_ENABLED=true，但没有任何 IMAGE_FALLBACK_n_* "
                               "备用源配置，仍按单源运行")
            return provider

        return FallbackImageProvider(
            candidates,
            strategy=_resolve_setting('IMAGE_FALLBACK_STRATEGY') or 'priority',
            max_attempts=_as_int(_resolve_setting('IMAGE_FALLBACK_MAX_ATTEMPTS'), 0),
            quota_cooldown=_as_float(_resolve_setting('IMAGE_FALLBACK_QUOTA_COOLDOWN_SECONDS'), 8 * 3600),
            error_cooldown=_as_float(_resolve_setting('IMAGE_FALLBACK_ERROR_COOLDOWN_SECONDS'), 600.0),
            failure_threshold=_as_int(_resolve_setting('IMAGE_FALLBACK_FAILURE_THRESHOLD'), 3),
        )
    except Exception as exc:
        logger.error("初始化图片多源兜底失败，回退为单源运行：%s", exc, exc_info=True)
        return provider
