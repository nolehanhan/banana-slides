# patch/ — banana-slides 本地补丁

本目录是**挂载覆盖式补丁**：由 `docker-compose.prod.yml` 的 `volumes` 以 `:ro` 方式覆盖容器内
对应文件，**不需要重建镜像**。改完宿主机文件后 `docker restart banana-slides-backend` 即可生效。

---

## ★ 先读这条：容器运行的代码 ≠ 宿主机仓库那份

`Dockerfile.backend-libreoffice` 是 `FROM anoinex/banana-slides-backend:latest` 再 apt 装
LibreOffice，**从不 COPY 宿主机源码**。所以：

- 宿主机 `backend/` 只是参考，**与镜像里那份不是同一提交**（都标 0.9.0，但内容有差异）；
- **改宿主机源码 + 重建镜像 = 改动不会生效**（重建拿到的还是镜像自带那份）；
- **写补丁的正确姿势**：先从容器里取出**真实运行版本**再改

  ```bash
  docker cp banana-slides-backend:/app/backend/<路径> ./patch/<名>.py
  ```

- **换镜像版本后必须重新取出**并重新比对，否则会挂载出旧代码。

比对文件前记得去 CRLF（宿主机是 CRLF、容器是 LF），否则会整文件假差异。

---

## 补丁清单

> **当前启用状态（2026-09-20 12:10）**：**四个补丁全部挂载中**。
> （后两个兜底补丁在 11:59 曾被停用，12:10 因启用「生图多源轮询」而重新挂载。）

| 补丁文件 | 覆盖容器内路径 | 作用 |
|---|---|---|
| `file_parser_service.py` ✅生效中 | `services/file_parser_service.py` | ① **PDF 解析方式可切换**（环境变量 `PDF_LOCAL_PARSE`，默认 `false` = 走 MinerU）。**为什么默认走 MinerU**：MinerU 会把 PDF 内嵌图片抠出来上传、并调视觉模型生成图片说明，markdown 里才有 `![](地址)`；PPT 翻新时原 PPT 里的图片就是靠这一步变成页面描述里的素材，后续「生成图片」再用 `extract_image_urls_from_markdown` 从描述里抓出来当参考图。设 `true` 走本地 PyMuPDF：秒出、离线，但**只提文字、不抠图**，图片素材会全部丢失。**MinerU 失败时自动降级为本地提取并打 warning**，不会让整个翻新任务失败。② 修正 `__init__` 参数名不匹配（补 `ai_provider_format` / `upload_folder` / `**ignored_kwargs`），上游调用方与类定义用了两套参数名，会抛 `TypeError` 致「重新解析此页」失败。 |
| `openai_provider.py` ✅生效中 | `services/ai_providers/image/openai_provider.py` | ① 适配 AIHubMix **`/ai/v1` 异步图像通道**（豆包 Seedream 等）：该通道只认 `aspect_ratio`、响应是任务对象而非 `data[]`、图片需带鉴权下载。**仅在 `IMAGE_API_BASE` 含 `/ai/v1` 时生效**（当前配的是 `aihubmix.com/v1`，此分支休眠），切回免费模型不受影响。② 支持**多张参考图**：上游 `_generate_with_images_api` 只取 `ref_images[0]`、其余静默丢弃；本补丁按 `_MAX_REF_IMAGES = 8` 全量下发（多图时单张等比缩到长边 ≤ `_MULTI_REF_MAX_EDGE = 1024`，只缩不放）。 |
| `ai_providers_init.py` ✅生效中 | `services/ai_providers/__init__.py` | 原 `get_image_provider` 改名 `_build_single_image_provider`，新的 `get_image_provider` 在其上包一层兜底；一次覆盖两个入口（`ai_service_manager` 缓存路径与 `AIService.__init__` 直接构造路径）。 |
| `fallback.py` ✅生效中 | `services/ai_providers/fallback.py` | 生图**多源轮询与失败兜底**：失败分类（额度 / 鉴权 / 模型不存在 / 参数 / 瞬时 / 内容拦截）、冷却登记（落盘 `instance/fallback_state.json`，容器重启后仍记得哪个源已耗尽）、多源调度（`priority` / `round_robin`）、全冷却时的降级试探。**2026-09-20 15:20 修「冷却被反复续期」缺陷**（见下节 ②）。 |

> **状态变更史**：2026-09-20 11:59 曾按「没配置好的临时改动」停用过这两条，原因是当时
> `.env` 里 `IMAGE_FALLBACK_ENABLED=auto` 却**一个备用源都没填**，候选表永远只有第 0 个主源，
> 兜底一直空转（证据：`instance/fallback_state.json` 从未生成过）。12:10 用户要求启用
> **生图 API 额度轮询**（一个源额度耗尽自动换下一个），遂重新挂载。
> **13:52 备用源首次真正填上并实测通过**（详见下节）。
>
> **生效前提（务必确认）**：`.env` 里至少配好一个 `IMAGE_FALLBACK_n_*`。只打开
> `IMAGE_FALLBACK_ENABLED` 而没有备用源时，会自动降级为单源运行，**不会报错也不会告警**，
> 容易误以为「已经生效」。
>
> **失败分类速查**：额度/频率耗尽（HTTP 402、429，或 `quota` / `insufficient balance` /
> `exceeded` 等关键词）→ 该类源冷却 8 小时；鉴权失效（401/403）与模型不存在（404）同样长冷却；
> 超时/网络/5xx/参数错属瞬时类，连续失败 3 次才冷却 10 分钟。
>
> 兜底只解决「源不可用」，**不解决「画得不好」**——画质问题换源解决不了。

---

## 兜底配置（写在 `.env`）✅ 已启用并实测通过

> **2026-09-20 13:52 起槽位 1 已填真 key**（AIHubMix 第二个账号，只授权 `gpt-image-2-free`），
> 容器内已确认拿到 `FallbackImageProvider` + 2 个候选，并**真实切换测试通过**（见下节）。
> ⚠️ 备用源**没有 UI 入口**，只能改 `.env`，改完必须
> `docker-compose -f docker-compose.prod.yml up -d backend` **重建**（`restart` 不重载 `env_file`）。

### ★ 先读这两条，都在 2026-09-20 踩过

**① AIHubMix 的额度极小，且多开同平台小号没用**

免费模型的额度**整个免费目录共享**：未充值账号**总共只有约 10 次**；充值 ≥ $1 后才解锁
**每天约 100 次 / 每分钟 10 次**，按 UTC 日切重置（≈ 北京时间早 8 点）。
实测证据：两个不同账号的 key（2 个候选）在 **4 秒内先后 429**（13:55:32 主源、随后备用号），
说明限制不止看账号，还带 IP / 设备维度 —— **同平台多开号 ≈ 没开**。要真正叠加额度，
得接**另一个平台**（商汤日日新每 5 小时 1500 次、硅基流动等）。

**② 冷却周期必须短于额度重置周期**

`fallback.py` 原写法是 `cooldown_until = max(cooldown_until, now + cooldown)`，
**每次「全冷却降级试探」失败都会重新续期**。配额恢复是外部事件（日切），不会因为你
多试几次而提前，于是重试越勤、恢复时刻越靠后 —— 实测 13:55 首次耗尽本该 21:55 解冻，
被 330 次重试一路推到 23:08，**永远等不到恢复**。

已于 15:20 修成「只在已解冻时才重新计时」：

```python
if kind in LONG_COOLDOWN_KINDS:
    if st['cooldown_until'] <= now:      # 已解冻才重新计时，冷却中不续期
        st['cooldown_until'] = now + long_cooldown
        st['reason'] = kind
    st['consecutive_failures'] = 0
```

同时把 `.env` 的 `IMAGE_FALLBACK_QUOTA_COOLDOWN_SECONDS` 从 28800(8h) 改成 **3600(1h)**：
8 小时比日重置周期还长，额度恢复了也要干等到冷却到期。**被 429 拒绝的请求不计入配额**，
所以缩短冷却是零成本的 —— 1 小时 = 额度恢复后最长 1 小时内自动复通。

主源就是原有的 `IMAGE_*` 配置（第 0 个候选），无需改动。备用源按序号追加，最多 9 个：

```dotenv
IMAGE_FALLBACK_1_LABEL=AIHubMix备用号
IMAGE_FALLBACK_1_SOURCE=openai
IMAGE_FALLBACK_1_API_KEY=sk-xxx
IMAGE_FALLBACK_1_API_BASE=https://aihubmix.com/v1
IMAGE_FALLBACK_1_MODEL=gpt-image-2-free
IMAGE_FALLBACK_1_PROTOCOL=images
```

> ⚠️ `API_BASE` / `MODEL` / `PROTOCOL` **必须与主源完全一致**，否则免费模型名会被内部白名单
> 误判为「聊天模型」→ 直接 400。

单个备用源可用 `ENABLED`、`API_KEY`、`API_BASE`、`MODEL`、`SOURCE`、`VENDOR`、`PROTOCOL`、`LABEL` 配置；
整块没配则跳过且不告警。全局开关：

| 变量 | 默认 | 说明 |
|---|---|---|
| `IMAGE_FALLBACK_ENABLED` | `auto` | `auto` = 配了备用源就启用；也接受 `true` / `false` |
| `IMAGE_FALLBACK_STRATEGY` | `priority` | `priority` 永远优先第 0 个（同一份 PPT 画风更统一）；`round_robin` 轮流起手，把用量摊平到所有源 |
| `IMAGE_FALLBACK_MAX_ATTEMPTS` | `0`（不限） | 单次生成最多尝试几个源 |
| `IMAGE_FALLBACK_QUOTA_COOLDOWN_SECONDS` | `28800`（8h） | 额度/鉴权/模型不存在类的冷却时长 |
| `IMAGE_FALLBACK_ERROR_COOLDOWN_SECONDS` | `600`（10min） | 瞬时类故障的冷却时长 |
| `IMAGE_FALLBACK_FAILURE_THRESHOLD` | `3` | 瞬时类故障连续失败几次后才冷却 |

> AIHubMix 免费额度按 **UTC 日切**（≈北京时间早 8 点重置）。

---

## 怎么验证轮询真的生效

### 1. 确认备用源被登记（★ 零成本探针，推荐）

**不耗额度、不改任何配置、不用重建**，直接在容器里构造一次 provider 并打印候选清单：

```bash
docker exec -i banana-slides-backend /app/.venv/bin/python - <<'PY' 2>&1 | grep -E "REG>>>|已登记|兜底已启用"
import logging, sys, os
os.chdir('/app/backend'); sys.path.insert(0, '/app/backend')
from app import create_app
app = create_app()
logging.getLogger('services.ai_providers.fallback').setLevel(logging.INFO)
with app.app_context():
    from flask import current_app
    model = current_app.config.get("IMAGE_MODEL") or "gpt-image-2-free"
    from services.ai_providers import get_image_provider
    p = get_image_provider(model=model)
    print("REG>>> provider =", type(p).__name__)
PY
```

配置正确时应看到（实测输出）：

```
INFO  [services.ai_providers.fallback] 已登记备用图片源 #1：AIHubMix备用号[openai] gpt-image-2-free @ aihubmix.com/v1
INFO  [services.ai_providers.fallback] 图片多源兜底已启用（策略=priority，共 2 个源）：primary(...)  → AIHubMix备用号(...)
REG>>> provider = FallbackImageProvider
```

判据：
- `provider = FallbackImageProvider` → 兜底层**真的包上了**（返回单源 provider 就是没生效）；
- 候选行里必须**主源 + ≥1 备用源**；只开 `IMAGE_FALLBACK_ENABLED` 而没配备用源时，
  `auto` 模式会**静默降级单源、不报错**，极易误判。

> 真实服务里这条 INFO 只会在**第一次生图**时才打印（provider 懒加载），
> 启动日志里没有兜底输出是正常现象。注意 `.env` 须 `LOG_LEVEL=INFO` 才看得到。

### 2. 看当前冷却状态

```bash
docker exec banana-slides-backend cat /app/backend/instance/fallback_state.json
```

文件不存在 = 从未发生过失败/冷却（正常）。有内容则能看到哪个源、因何冷却、还剩多久。

### 3. 真实切换实测（会消耗 1 张备用源额度）

用「内存里伪造一个坏掉的主源」来触发切换，**不改配置、不用重建、不污染正式状态文件**：

```bash
docker exec -i banana-slides-backend /app/.venv/bin/python - <<'PY' 2>&1 | grep -E ">>>|兜底成功|失败"
import sys, os
os.chdir('/app/backend'); sys.path.insert(0, '/app/backend')
from app import create_app
app = create_app()
with app.app_context():
    from services.ai_providers import _build_single_image_provider, _resolve_setting
    from services.ai_providers.fallback import (CoolDownRegistry, FallbackImageProvider,
                                                _primary_candidate, _extra_candidates)
    BOGUS = "no-such-model-probe-abc123"
    cands  = [_primary_candidate(_build_single_image_provider(BOGUS), BOGUS, _resolve_setting)]
    cands += _extra_candidates(_resolve_setting)
    print(">>> 候选:", [c.describe() for c in cands])
    p = FallbackImageProvider(cands, strategy="priority",
                              registry=CoolDownRegistry(path="/tmp/probe_state.json"))
    img = p.generate_image(prompt="a solid red circle on plain white background",
                           aspect_ratio="16:9", resolution="1K")
    print(">>> ★ 拿到图片:", img.size, img.mode)
PY
```

实测结果：坏主源报 `403 auth` 并被冷却 28800 秒 → 自动落到 `AIHubMix备用号` →
**14.2 秒拿到 1280×720 真图（206,837 字节）**。用完记得
`docker exec banana-slides-backend rm -f /tmp/probe_state.json`。

> 注意：`CoolDownRegistry(path=...)` 用临时文件，**不会**写正式
> `instance/fallback_state.json`，所以不会影响线上源的冷却状态。

### 4. 重置全部冷却（想立刻重试所有源）

```bash
docker exec banana-slides-backend rm -f /app/backend/instance/fallback_state.json
```

## 回滚

在 `docker-compose.prod.yml` 里注释掉对应的 `volumes` 条目、重启后端即可（每个补丁条目上方
都写了具体回滚方法）。`Dockerfile.backend-libreoffice` 的回滚是删掉 backend 的 `build` 段并把
`image` 改回 `${DOCKER_IMAGE_BACKEND:-anoinex/banana-slides-backend:latest}`。

### 状态变更记录

| 日期 | 事项 | 原因 | 备份 |
|---|---|---|---|
| 2026-09-20 11:59 | 停用 `ai_providers_init.py` + `fallback.py` 挂载 | 未配任何备用源，兜底长期空转；且官方原版行为已够用 | `docker-compose.prod.yml.bak-before-fallback-rollback-20260920-115853`、`.env.bak-before-fallback-rollback-20260920-115853` |
| 2026-09-20 12:10 | ↑ 同两项**重新启用** | 启用生图 API 额度轮询（一个源额度耗尽自动换下一个） | 无（本次为恢复挂载） |
| 2026-09-20 15:20 | 修 `fallback.py` **冷却续期缺陷** + `.env` 降温时长 8h → 1h | 原 `max(cooldown_until, now + cd)` 让每次降级试探都续期，恢复时刻被无限推后（实测 13:55 首次耗尽本该 21:55 解冻，被 330 次重试推到 23:08）；且 8h 冷却是配额日重置周期的 1/3，额度回来了也要干等 | `patch/fallback.py`（改前版本见 git `050ee56`） |

### 评估过但**未**回滚的项（2026-09-20 复核结论）

| 项 | 结论 |
|---|---|
| `file_parser_service.py`（本地 PDF 解析） | **2026-09-20 12:40 改为可切换，默认切回 MinerU**。当日查清因果链：本地解析只抠文字不抠图 → markdown 里 0 个图片链接 → 页面描述里 0 张图 → 生成图片时抓不到任何参考图，**原 PPT 里的图片等于被丢弃**。文件仍需挂载，因为其中还含「参数名不匹配」的上游 bug 修复（回滚挂载会重新引爆 `TypeError`）。⚠️ 走 MinerU 必须先解决 `cdn-mineru.openxlab.org.cn` 的 TLS 握手阻断（见下）。 |
| `openai_provider.py`（AIHubMix `/ai/v1` 通道） | **保留**。该分支要求 `IMAGE_API_BASE` 含 `/ai/v1`，当前配的是 `https://aihubmix.com/v1`，**分支本就休眠**＝等于已回滚；且同一文件还承载着多参考图（8 张）支持，删掉会连带丢失。 |
| compose 里的出网代理段（`HTTP_PROXY` / `extra_hosts`） | **必须保留**。图片生成走 `aihubmix.com`，该域名在本机被 DNS 污染 + SNI 阻断，容器不继承宿主机代理，去掉后生图直接不可用。 |

---

## 许可

上游 banana-slides 为 **AGPL-3.0**。自己用随意改；**一旦对外提供网络服务，必须开源改动后的源码**。
