"""Application configuration using pydantic-settings."""

from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Gemini API key (Google AI Studio — set via secrets/gemini_api_key)
    gemini_api_key: str = ""

    # DeepSeek API key (OpenAI-compatible — set via secrets/deepseek_api_key)
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # Redis
    redis_url: str = "redis://redis:6379"

    # Database (3 slashes for relative path)
    database_url: str = "sqlite+aiosqlite:///./metadata.db"

    # LLM Models (via Vertex AI)
    gemini_project: str = "tarot-493203"
    gemini_location: str = "global"
    gemini_script_model: str = "gemini-2.5-pro"
    gemini_director_model: str = "gemini-2.5-pro"

    # Worker settings (from config.yml / config.env)
    worker_pool_size: int = 4

    # 内容分析（爆款归因）
    asr_model: str = "large-v3"
    asr_device: str = "cpu"          # cpu | cuda
    asr_compute_type: str = "int8"   # int8 | float16
    content_analysis_model: str = "gemini-2.5-pro"

    # Langfuse observability (LLM tracing)
    # Keys come from secrets (langfuse_public_key / langfuse_secret_key);
    # host + enabled flag come from config.yml. Tracing degrades to a no-op
    # when disabled or when keys are missing.
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://us.cloud.langfuse.com"

    # Video provider selection: "vertex" (Veo via Vertex AI) or "kie" (kie.ai REST)
    video_provider: str = "vertex"

    # Veo (video generation via Vertex AI)
    veo_api_key: str = ""
    veo_project: str = "tarot-493203"
    veo_location: str = "us-central1"
    veo_model: str = "veo-3.1-fast-generate-001"
    veo_poll_interval_seconds: int = 10
    veo_max_wait_seconds: int = 300

    # kie.ai (video generation via REST — used when video_provider == "kie")
    # API key from https://kie.ai/api-key — set via secrets/kie_api_key
    kie_api_key: str = ""
    kie_base_url: str = "https://api.kie.ai"
    kie_upload_url: str = "https://kieai.redpandaai.co"
    # veo3 | veo3_fast | veo3_lite (REFERENCE_2_VIDEO requires veo3_fast)
    kie_veo_model: str = "veo3_fast"
    kie_resolution: str = "1080p"  # 720p | 1080p | 4k
    kie_poll_interval_seconds: int = 10
    kie_max_wait_seconds: int = 600
    # Retry once on transient upstream failure (successFlag=3, "upstream gen failed").
    kie_max_retries: int = 1
    kie_retry_backoff_seconds: int = 15

    # ── 腾讯云 COS ──────────────────────────────────────────────────────────
    # 存储权威副本所在 bucket。dev / prod 使用不同 bucket。
    # 应与 CVM 同地域，后端↔COS 才走内网免流量费。
    cos_region: str = ""
    # 必须含 AppId，形如 video-maker-dev-1250000000
    cos_bucket: str = ""
    cos_scheme: str = "https"
    # 自定义源站域名；留空则用默认域名 {bucket}.cos.{region}.myqcloud.com
    cos_domain: Optional[str] = None
    # "static"（开发，永久密钥）| "cvm_role"（生产，实例角色取 STS 临时密钥）
    cos_auth_mode: str = "static"
    # cvm_role 模式下绑定在 CVM 上的 CAM 角色名
    cos_cvm_role: Optional[str] = None
    # 预签名 URL 有效期，默认 2 小时
    cos_signed_url_ttl_sec: int = 7200
    # 仅 static 模式使用，由 /run/secrets/ 注入
    cos_secret_id: Optional[str] = None
    cos_secret_key: Optional[str] = None

    # Voice conversion (in-process vc2.VoiceConverter)
    model_dir: str = "/workspace/exported_vc2"
    vc_num_threads: int = 4

    # Character calibration (face correction on last frame, via Vertex AI)
    cc_project: str = "tarot-493203"
    cc_location: str = "global"
    cc_model: str = "gemini-3.1-flash-image-preview"
    # [Image 1] = character reference image (identity donor)
    # [Image 2] = shot last frame (target to repair)
    # [Image 1] = shot last frame (BASE — preserve everything)
    # [Image 2] = character reference image (only copy facial features from this)
    cc_prompt: str = (
        "Precise FACIAL-FEATURE-ONLY identity edit. Edit the LAST image only.\n\n"
        "The LAST image is the BASE. The output MUST stay identical to the LAST "
        "image in EVERYTHING except the facial features: same camera framing, same "
        "pose, same head angle and position, same body, arms, hands, fingers and "
        "ring placement, same clothing, same hair, same expression, same eye gaze "
        "and mouth state, same lighting and background — pixel-identical.\n\n"
        "The OTHER image(s) are the IDENTITY reference, used ONLY as the source of "
        "facial features: eye shape, nose shape, lip/mouth shape, eyebrows, and "
        "face/jaw bone structure and skin texture. Do NOT copy ANYTHING else from "
        "the reference image(s) — ignore their pose, head angle, hands, body, "
        "expression, gaze, hair, clothing and background. They only tell you WHO "
        "the person is, never how they are posed.\n\n"
        "Task: keep the LAST image unchanged and adjust ONLY the facial features so "
        "the person's identity matches the reference. Do NOT re-pose or move the "
        "head, hands, arms or body. Do NOT change the expression or where the eyes "
        "look. Keep the skin texture, wrinkles and pores from the LAST image — do "
        "not smooth.\n\n"
        "If changing the face would require altering the pose, keep the pose."
    )

    # Tail frame generation (target end-frame via Vertex AI)
    tf_project: str = "tarot-493203"
    tf_location: str = "global"
    tf_model: str = "gemini-3.1-flash-image-preview"
    tf_cot_model: str = "gemini-2.5-flash"  # text model for CoT pose analysis
    # Step 1: CoT analysis (TEXT only) — reason about the end pose
    tf_cot_prompt: str = (
        "You are a cinematography expert. Analyze the motion prompt below "
        "and the starting frame image to determine the character's FINAL "
        "pose after the described action completes.\n\n"
        "Motion prompt:\n{motion_prompt}\n\n"
        "Input images (in order):\n"
        "- [Character Reference]: facial identity only\n"
        "- [Object Reference] (if any): props/objects that MUST be visible "
        "in the final frame — describe how and where the character interacts "
        "with or displays these objects in the end pose\n"
        "- [Starting Frame]: current scene state\n\n"
        "Think step by step:\n"
        "1. Describe the starting pose/position visible in the starting frame.\n"
        "2. What motion/action does the prompt describe?\n"
        "3. If object reference images are provided, how should these objects "
        "appear in the final frame? (e.g. held in hand, placed on table, "
        "displayed to camera)\n"
        "4. After that motion completes, what is the character's FINAL body "
        "pose — head angle, torso orientation, arm/hand positions, facial "
        "expression, eye gaze? Include interaction with reference objects.\n"
        "5. List the specific differences between starting pose and final pose.\n\n"
        "Hard rules:\n"
        "- Describe the FINAL pose that naturally results from the motion "
        "prompt — do NOT invent large movements that are not in the motion "
        "prompt.\n"
        "- If object reference images are provided, the end pose MUST show "
        "the character interacting with or clearly displaying those objects.\n\n"
        "Output a concise description of the FINAL POSE only (no preamble). "
        "Example: \"Head tilted 15° right, eyes looking down-left, lips "
        "slightly parted, right hand raised to chin level holding [object], "
        "torso leaning forward 10°.\""
    )

    # Step 2: Image generation (IMAGE only) — generate with CoT result
    tf_prompt: str = (
        "Task: Generate the FINAL FRAME of a video shot — the last moment "
        "after all described motion has completed.\n\n"
        "Motion prompt (what happens during the shot):\n"
        "{motion_prompt}\n\n"
        "Analyzed end pose (the character MUST be in this exact pose):\n"
        "{end_pose}\n\n"
        "Image roles — read carefully. The PRIMARY anchor for the person is "
        "the Character Reference; the Starting Frame is an AUXILIARY "
        "continuity reference only:\n"
        "- [Character Reference] (last image): PRIMARY anchor for rendering "
        "the character — face (bone structure, jawline, cheekbones, eye "
        "shape, nose, skin texture, hair) AND overall likeness come from "
        "this image, rendered SHARP and high-detail. This reference sets "
        "the output's fidelity: the person must look exactly like this and "
        "be at least as crisp as this image. Do NOT copy the pose, hand "
        "position, expression, or eye gaze from it — those come from the "
        "analyzed end pose.\n"
        "- [Starting Frame] (first image): AUXILIARY reference for "
        "CONTINUITY ONLY — background, scene layout, lighting, camera angle, "
        "camera framing, wardrobe colors, and the character's starting "
        "position so the end pose reads as a natural continuation. Keep the "
        "SAME camera distance, field of view, and subject size as this image "
        "— do NOT zoom in, crop tighter, or enlarge the subject. If this "
        "frame is soft, low-detail, or compressed (e.g. a video still), do "
        "NOT reproduce that softness — render the output crisp regardless. "
        "Do NOT copy the character's body/arm/hand pose from it — pose comes "
        "from the analyzed end pose.\n"
        "- [Object Reference] (if any): Props/objects that MUST be clearly "
        "visible, recognizable, and faithfully reproduced; the character "
        "interacts with, holds, or displays them. Do not use for pose.\n\n"
        "Requirements:\n"
        "- Fidelity + sharpness anchor: the person and all fine detail come "
        "from the [Character Reference]; the output must never be blurrier "
        "than that reference, even when the Starting Frame is a soft video "
        "still\n"
        "- Camera distance and framing: identical to [Starting Frame] — "
        "never tighter, never zoomed in\n"
        "- Body pose, hand position, facial expression, and eye gaze all "
        "come from the analyzed end pose above — NOT from any input image\n"
        "- Render the hands and arms in the position described by the end "
        "pose rather than copying the Starting Frame; when the end pose "
        "places them differently, reflect that change fully\n"
        "- Background / lighting / camera angle / wardrobe consistent with "
        "[Starting Frame] (for continuity)\n"
        "- Photorealistic, 8K detail"
    )

    # First frame generation (opening keyframe via Vertex AI) — mirrors the tail
    # frame two-step CoT and reuses tf_project/tf_location/tf_model/tf_cot_model.
    # Step 1: CoT analysis (TEXT only) — reason about the opening composition
    ff_cot_prompt: str = (
        "You are a cinematography expert. Analyze the shot description below "
        "to determine the OPENING FRAME composition — the very first moment "
        "of the shot, before any motion happens.\n\n"
        "Shot type: {shot_type}\n"
        "Visual description:\n{visual_description}\n\n"
        "Motion that will happen during the shot (the opening frame must be "
        "a natural STARTING point for it, not its result):\n{motion_prompt}\n\n"
        "Input images (in order):\n"
        "- [Character Reference]: facial identity only\n"
        "- [Object Reference] (if any): props/objects that MUST be visible "
        "in the opening frame — describe how and where they are placed or "
        "held\n"
        "- [Scene Context] (if any): current scene state for background/"
        "lighting/wardrobe continuity\n\n"
        "Think step by step:\n"
        "1. What framing does the shot type imply (camera distance, angle)?\n"
        "2. What scene, background, and lighting does the visual description "
        "call for?\n"
        "3. If object reference images are provided, how should these objects "
        "appear in the opening frame? (e.g. held in hand, placed on table, "
        "displayed to camera)\n"
        "4. What is the character's STARTING body pose — head angle, torso "
        "orientation, arm/hand positions, facial expression, eye gaze — such "
        "that the described motion can naturally begin from it?\n\n"
        "Hard rules:\n"
        "- Describe the OPENING state only — do NOT depict the motion already "
        "underway or completed.\n"
        "- If object reference images are provided, the opening frame MUST "
        "show them clearly.\n\n"
        "Output a concise description of the OPENING COMPOSITION only (no "
        "preamble): framing, scene, character pose, and object placement.\n\n"
        "Note: the provided context frame (if any) may be either the CURRENT "
        "opening frame or the shot's ENDING frame. If it is the ending frame, "
        "reason BACKWARDS: derive an opening composition that would naturally "
        "evolve into that ending state."
    )

    # Step 2: Image generation (IMAGE only) — generate with CoT result
    ff_prompt: str = (
        "Task: Generate the OPENING FRAME of a video shot — the first moment "
        "before any motion begins.\n\n"
        "Visual description of the shot:\n{visual_description}\n\n"
        "Analyzed opening composition (the image MUST match this):\n"
        "{opening_composition}\n\n"
        "Image roles — read carefully, each image has a NARROW purpose:\n"
        "- [Scene Context] (first image, if any): Use for background, "
        "scene layout, lighting, wardrobe colors, AND camera framing — "
        "the output MUST keep the SAME camera distance, field of view, "
        "and subject size as this image. Do NOT zoom in, crop tighter, "
        "or enlarge the subject relative to this image. Do NOT copy the "
        "character's body pose from this image — pose comes exclusively "
        "from the analyzed opening composition.\n"
        "- [Object Reference] (if any): Props/objects that MUST be clearly "
        "visible in the output image, faithfully reproduced and "
        "recognizable. Do not use for pose.\n"
        "- [Character Reference] (last image): Use ONLY for facial "
        "identity — facial bone structure (face shape, jawline, "
        "cheekbones), eye shape, nose shape, skin texture, hair style. "
        "The face must clearly look like this person — sharp, detailed, "
        "not blurry. DO NOT copy the pose or eye gaze from this image.\n\n"
        "Requirements:\n"
        "- Camera distance and framing: identical to [Scene Context] when "
        "provided — never tighter, never zoomed in; otherwise follow the "
        "shot type in the opening composition exactly\n"
        "- Scene, body pose, and object placement all come from "
        "the analyzed opening composition above\n"
        "- Face identity copied from [Character Reference] (features only, "
        "not expression)\n"
        "- Background / lighting / wardrobe consistent with [Scene Context] "
        "when provided\n"
        "- Photorealistic, 8K detail"
    )

    # CORS (from config.yml / config.env)
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # ── 应用级鉴权（FRD 2026-08-11） ────────────────────────────────────────
    # 唯一的「未认证是否放行」开关。false（默认）= 线上行为与鉴权上线前一致：
    # 无凭据照常放行、不按用户过滤、不扣点数。true = 默认拒绝，且按 owner_id
    # 严格隔离。分期上线见 FRD §4.1（P1/P2 保持 false，P3 才打开）。
    auth_enforced: bool = False
    # 会话有效期（滑动过期）。每次通过校验的请求都会把 TTL 重置回该值。
    session_ttl_days: int = 7
    session_cookie_name: str = "session"
    # 前后端是**不同站点**（Vercel vs 集群），跨站 cookie 必须 SameSite=None
    # + Secure，否则登录后一刷新就掉线。本地 http 调试可置 false。
    session_cookie_secure: bool = True
    # 机器凭据（MCP 等非浏览器调用方）。来自 secret，绝不写进 config.yml。
    machine_token: str = ""
    # 机器令牌绑定到哪个账号：置空 = 服务主体（不受 owner 过滤、不扣点数）。
    # 设成某个用户名后，机器调用与该用户完全同权同计费。
    machine_token_user: str = ""
    # 注册限流：每个来源 IP 每 window 秒最多注册 N 次（FR-10，已定 1 次/小时）。
    register_rate_limit: int = 1
    register_rate_limit_window_sec: int = 3600
    # 注册赠送点数 = 0（FR-0）：消灭批量注册的经济动机。
    register_grant_credits: int = 0

    # ── 点数单价（FRD §5.1，锚点 1 点 = $0.01 底层成本） ────────────────────
    # ⚠️ 换 VEO_MODEL 必须同步改 credit_cost_video_per_second：
    # Fast $0.15/秒 vs 标准 $0.75/秒，差 5 倍。
    credit_cost_video_per_second: int = 15
    credit_cost_script: int = 5
    credit_cost_shotlist: int = 5
    credit_cost_image: int = 8
    credit_cost_analysis: int = 3
    credit_cost_ai_edit: int = 1

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
