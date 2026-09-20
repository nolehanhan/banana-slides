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
| `fallback.py` ✅生效中 | `services/ai_providers/fallback.py` | 生图**多源轮询与失败兜底**：失败分类（额度 / 鉴权 / 模型不存在 / 参数 / 瞬时 / 内容拦截）、冷却登记（落盘 `instance/fallback_state.json`，容器重启后仍记得哪个源已耗尽）、多源调度（`priority` / `round_robin`）、全冷却时的降级试探。 |

> **状态变更史**：2026-09-20 11:59 曾按「没配置好的临时改动」停用过这两条，原因是当时
> `.env` 里 `IMAGE_FALLBACK_ENABLED=auto` 却**一个备用源都没填**，候选表永远只有第 0 个主源，
> 兜底一直空转（证据：`instance/fallback_state.json` 从未生成过）。12:10 用户要求启用
> **生图 API 额度轮询**（一个源额度耗尽自动换下一个），遂重新挂载 —— 这次会真正配上备用源。
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

## 兜底配置（写在 `.env`）✅ 当前已启用

> 补丁已挂载，但**必须真正填上备用源才开始轮询**。当前待填的是槽位 1
> （AIHubMix 第二个账号），即 `.env` 里标了「★ 你的备用源就填这里」的那一段。
> 填好之后 `docker-compose -f docker-compose.prod.yml up -d backend` 重建即生效。

主源就是原有的 `IMAGE_*` 配置（第 0 个候选），无需改动。备用源按序号追加，最多 9 个：

```dotenv
IMAGE_FALLBACK_1_LABEL=doubao
IMAGE_FALLBACK_1_SOURCE=openai
IMAGE_FALLBACK_1_API_KEY=sk-xxx
IMAGE_FALLBACK_1_API_BASE=https://aihubmix.com/ai/v1
IMAGE_FALLBACK_1_MODEL=doubao-seedream-5.0-lite
IMAGE_FALLBACK_1_PROTOCOL=images
```

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

1. **确认备用源被登记**：重建容器后看日志，应出现
   `已登记备用图片源 #1：...`。若看到
   `IMAGE_FALLBACK_ENABLED=true，但没有任何 IMAGE_FALLBACK_n_* 备用源配置` 就说明没配上。
   启动日志里完全没有兜底输出属正常 —— provider 是生图时才构造的。

2. **看当前冷却状态**：
   ```bash
   docker exec banana-slides-backend cat /app/backend/instance/fallback_state.json
   ```
   文件不存在 = 从未发生过失败/冷却（正常）。有内容则能看到哪个源、因何冷却、还剩多久。

3. **手动模拟额度耗尽**（最直接）：临时把主源的 `IMAGE_MODEL` 改成一个不存在的模型名，
   主源必然返回 404（属长冷却类），从而触发切换。生成一次图片，看日志与
   `fallback_state.json`；**验证完务必改回来**。

4. **重置全部冷却**（想立刻重试所有源）：
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

### 评估过但**未**回滚的项（2026-09-20 复核结论）

| 项 | 结论 |
|---|---|
| `file_parser_service.py`（本地 PDF 解析） | **2026-09-20 12:40 改为可切换，默认切回 MinerU**。当日查清因果链：本地解析只抠文字不抠图 → markdown 里 0 个图片链接 → 页面描述里 0 张图 → 生成图片时抓不到任何参考图，**原 PPT 里的图片等于被丢弃**。文件仍需挂载，因为其中还含「参数名不匹配」的上游 bug 修复（回滚挂载会重新引爆 `TypeError`）。⚠️ 走 MinerU 必须先解决 `cdn-mineru.openxlab.org.cn` 的 TLS 握手阻断（见下）。 |
| `openai_provider.py`（AIHubMix `/ai/v1` 通道） | **保留**。该分支要求 `IMAGE_API_BASE` 含 `/ai/v1`，当前配的是 `https://aihubmix.com/v1`，**分支本就休眠**＝等于已回滚；且同一文件还承载着多参考图（8 张）支持，删掉会连带丢失。 |
| compose 里的出网代理段（`HTTP_PROXY` / `extra_hosts`） | **必须保留**。图片生成走 `aihubmix.com`，该域名在本机被 DNS 污染 + SNI 阻断，容器不继承宿主机代理，去掉后生图直接不可用。 |

---

## 许可

上游 banana-slides 为 **AGPL-3.0**。自己用随意改；**一旦对外提供网络服务，必须开源改动后的源码**。
