/**
 * Content Analysis (内容分析) — REAL end-to-end flow.
 *
 * Per CLAUDE.md "E2E Tests — NEVER fake the data or flow under test": no
 * `route.fulfill` faking of analysis/brief data. Two strategies keep this
 * spec real while avoiding the billed ASR + Gemini pipeline:
 *
 * 1. Seeding: a `completed` ContentAnalysis (+ ReferenceSample rows) is
 *    inserted directly into the REAL DB via `podman exec` into the running
 *    backend container (mirrors `trim-dual-track.spec.ts` / `CLAUDE.md`'s
 *    "podman exec 进容器插行" seeding convention) — using the app's own ORM
 *    models via `uv run --project /app python -` (stdin, no new backend
 *    file — this worktree's task explicitly disallows backend changes).
 *    The UI then renders this real row via the real GET /api/analyses and
 *    GET /api/analyses/{id} endpoints. Teardown deletes the seeded rows the
 *    same way (`reference_samples` first — SQLite FK cascade is not
 *    enforced on this connection, verified: a bare `DELETE FROM
 *    content_analyses` left orphan `reference_samples` rows behind).
 *
 * 2. The one true-UI-driven test submits a REAL `POST /api/analyses` with
 *    an invalid `region_hint` (`en-US`). The backend validates region_hint
 *    at the API boundary and returns a REAL HTTP 400 BEFORE enqueuing any
 *    worker job (verified via direct curl: 400, no ASR/LLM job created) —
 *    so this exercises the real create → validate → error path with zero
 *    billing risk.
 */
import { test, expect } from '@playwright/test'
import { execSync } from 'node:child_process'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

const CONTAINER = 'video-maker-backend-dev'

// ─── Seed helpers (real DB via podman exec — no backend file added) ────────

const NICHE_SUMMARY = 'E2E-赛道摘要：都市悬疑短剧钩子密度高，前 3 秒抛出反常细节。'
const HOOK_TYPE = 'E2E-悬念反转式开场'
const DIRECTIVES = 'E2E-编剧指令：开场用一句强反差台词切入，避免自我介绍式铺垫。'

function seedCompletedAnalysis(analysisId: string, title: string): void {
  const script = `
import asyncio, json, sys
sys.path.insert(0, "/app")
from app.db import AsyncSession, init_db
from app.models.project import ContentAnalysis, ReferenceSample

ANALYSIS_ID = ${JSON.stringify(analysisId)}
TITLE = ${JSON.stringify(title)}

BRIEF = {
    "niche_summary": ${JSON.stringify(NICHE_SUMMARY)},
    "sample_stats": {"sample_n": 2, "no_speech_pct": 0.5, "sample_warning": None},
    "hook_strategy": {
        "common_hook_types": [${JSON.stringify(HOOK_TYPE)}],
        "example_hooks": ["你绝对想不到接下来发生了什么"],
    },
    "script_structure": {
        "pacing": "前紧后松，3 秒一个信息点",
        "emotion": "好奇 -> 惊讶 -> 释然",
        "info_gap": "先抛结果、不解释原因",
        "cta": "关注看下集揭晓",
    },
    "do": ["前 3 秒抛钩子", "台词口语化"],
    "dont": ["开场自我介绍", "长镜头铺垫"],
    "screenwriter_directives": ${JSON.stringify(DIRECTIVES)},
}

async def main():
    await init_db()
    async with AsyncSession() as session:
        analysis = ContentAnalysis(
            id=ANALYSIS_ID, title=TITLE, region_hint="zh",
            status="completed", brief_json=json.dumps(BRIEF, ensure_ascii=False),
        )
        session.add(analysis)
        await session.flush()
        session.add(ReferenceSample(
            analysis_id=ANALYSIS_ID, order_index=0,
            video_path="/app/storage/e2e-seed/" + ANALYSIS_ID + "/s1.mp4",
            has_speech=True, hook_text="你绝对想不到接下来发生了什么",
            full_transcript="完整转写文本示例", language="zh", status="transcribed",
        ))
        session.add(ReferenceSample(
            analysis_id=ANALYSIS_ID, order_index=1,
            video_path="/app/storage/e2e-seed/" + ANALYSIS_ID + "/s2.mp4",
            has_speech=False, status="transcribed",
        ))
        await session.commit()
    print("ok")

asyncio.run(main())
`.trim()
  const out = execSync(`podman exec -i ${CONTAINER} uv run --project /app python -`, {
    input: script,
    encoding: 'utf8',
  }).trim()
  expect(out).toBe('ok')
}

function deleteAnalysis(analysisId: string): void {
  const script = `
import asyncio, sys
sys.path.insert(0, "/app")
from sqlalchemy import delete
from app.db import AsyncSession, init_db
from app.models.project import ContentAnalysis, ReferenceSample

ANALYSIS_ID = ${JSON.stringify(analysisId)}

async def main():
    await init_db()
    async with AsyncSession() as session:
        # SQLite FK cascade is NOT enforced on this connection — delete the
        # child rows explicitly first, or they orphan (verified during dev).
        await session.execute(delete(ReferenceSample).where(ReferenceSample.analysis_id == ANALYSIS_ID))
        await session.execute(delete(ContentAnalysis).where(ContentAnalysis.id == ANALYSIS_ID))
        await session.commit()
    print("ok")

asyncio.run(main())
`.trim()
  execSync(`podman exec -i ${CONTAINER} uv run --project /app python -`, {
    input: script,
    encoding: 'utf8',
  })
}

// ─── Seeded-data tests (list card + brief view) ─────────────────────────────

test.describe('Analyses List + Detail (seeded real DB row)', () => {
  let analysisId = ''
  const title = `E2E-内容分析-${Date.now()}`

  test.beforeAll(() => {
    analysisId = `e2e-ca-${Date.now()}`
    seedCompletedAnalysis(analysisId, title)
  })

  test.afterAll(() => {
    if (analysisId) deleteAnalysis(analysisId)
  })

  test('list shows the seeded analysis card with title + 已完成 badge', async ({ page }) => {
    await page.goto('/analyses')
    await expect(page.getByTestId('analysis-list')).toBeVisible({ timeout: 10_000 })
    const card = page.getByTestId(`analysis-card-${analysisId}`)
    await expect(card).toBeVisible()
    await expect(card.getByText(title)).toBeVisible()
    await expect(card.getByText('已完成')).toBeVisible()
  })

  test('detail page renders brief-view with niche summary, hook type, and directives', async ({ page }) => {
    await page.goto(`/analyses/${analysisId}`)
    await expect(page.getByTestId('analysis-detail-page')).toBeVisible({ timeout: 10_000 })
    const brief = page.getByTestId('brief-view')
    await expect(brief).toBeVisible({ timeout: 10_000 })
    await expect(brief.getByText(NICHE_SUMMARY)).toBeVisible()
    await expect(brief.getByText(HOOK_TYPE)).toBeVisible()
    await expect(brief.getByText(DIRECTIVES)).toBeVisible()
  })
})

// ─── Real-UI, no-seed test (real 400 before pipeline enqueue) ──────────────

test.describe('New Analysis form (real backend 400, no pipeline)', () => {
  let fixtureDir: string
  let fixturePath: string

  test.beforeAll(() => {
    fixtureDir = mkdtempSync(path.join(tmpdir(), 'ca-e2e-'))
    fixturePath = path.join(fixtureDir, 'fixture.mp4')
    execSync(
      `ffmpeg -y -f lavfi -i testsrc=duration=1:size=128x128:rate=10 ` +
        `-f lavfi -i sine=frequency=440:duration=1 -shortest -pix_fmt yuv420p ${fixturePath}`,
      { stdio: 'pipe' }
    )
  })

  test.afterAll(() => {
    if (fixtureDir) rmSync(fixtureDir, { recursive: true, force: true })
  })

  test('invalid region_hint surfaces the real backend 400 as a visible error toast', async ({ page }) => {
    await page.goto('/analyses/new')
    await page.getByTestId('analysis-title-input').fill('E2E-语言校验测试')
    await page.getByTestId('analysis-lang-input').fill('en-US')
    await page
      .locator('[data-testid="analysis-upload"] input[type="file"]')
      .setInputFiles(fixturePath)
    await page.getByTestId('create-analysis-submit').click()

    // Real POST /api/analyses -> real 400 (region_hint validated before enqueue)
    // -> frontend surfaces backend `detail` as a toast. Message names the
    // rejected language code. (The static form hint text below the language
    // input also mentions "en-US" as an example, so match the quoted form
    // ('en-US') that only the backend's error detail produces, to keep this
    // scoped to the toast rather than colliding with that static hint.)
    await expect(page.getByText(/不是 ASR 模型支持的语言代码/)).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText(/'en-US'/)).toBeVisible()

    // No pipeline was enqueued: submit button returns to its idle label
    // instead of staying in the (would-be) processing state, and we never
    // navigated away to /analyses/{id}.
    await expect(page).toHaveURL(/\/analyses\/new$/)
  })
})
