import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import { ShotCard } from '../ShotCard'
import type { Shot, AspectRatio } from '@/lib/types'

// Mock api module
vi.mock('@/lib/api', () => ({
  api: {},
}))

const baseShot: Shot = {
  id: 1,
  project_id: 'proj-1',
  shot_id: 1,
  text: 'Test shot',
  shot_type: 'Medium Shot',
  visual_description: 'desc',
  shot_duration: 4,
  status: 'completed',
  align_with_previous: false,
  use_prev_last_frame: false,
  motion_prompt: null,
  first_frame_path: '/fake/first.jpg',
  video_path: '/fake/video.mp4',
  last_frame_path: '/fake/last.jpg',
  word_count_warning: false,
  error_message: null,
  custom_first_frame_path: null,
  custom_reference_paths: null,
  reference_image_hint: null,
  vc_status: null,
  vc_error_message: null,
  cc_status: null,
  cc_error_message: null,
  target_last_frame_path: null,
  tf_status: null,
  tf_error_message: null,
  tf_confirmed: false,
  auto_trim: true,
}

function renderShotCard(aspectRatio?: AspectRatio) {
  return render(
    <ShotCard
      shot={baseShot}
      variant="review"
      projectId="proj-1"
      aspectRatio={aspectRatio}
    />,
  )
}

function getPreviewVideo(): HTMLElement {
  return document.querySelector('video')!
}

function getPreviewContainer(): HTMLElement {
  return getPreviewVideo().closest('div[class*="rounded-lg"]')!
}

describe('ShotCard — preview adaptive sizing', () => {
  it('preview container has no fixed pixel width or height', () => {
    renderShotCard('16:9')
    const container = getPreviewContainer()
    const style = container.getAttribute('style') || ''
    expect(style).not.toMatch(/width:\s*\d+px/)
    expect(style).not.toMatch(/height:\s*\d+px/)
  })

  it('preview container has no black background', () => {
    renderShotCard('16:9')
    const container = getPreviewContainer()
    expect(container.className).not.toContain('bg-zinc-900')
    expect(container.className).not.toContain('bg-black')
  })

  it('preview video uses w-full to fill container width', () => {
    renderShotCard('16:9')
    const img = getPreviewVideo()
    expect(img.className).toContain('w-full')
  })

  it('preview video has no fixed aspect-ratio or max-h constraints', () => {
    renderShotCard('16:9')
    const img = getPreviewVideo()
    const container = getPreviewContainer()
    // No forced aspect ratio — container adapts to image's natural ratio
    expect(container.className).not.toContain('aspect-video')
    expect(container.className).not.toContain('aspect-[9/16]')
    // No max-h — image fills naturally
    expect(img.className).not.toMatch(/max-h-/)
  })

  it('9:16 preview video also uses w-full (same adaptive behavior)', () => {
    renderShotCard('9:16')
    const img = getPreviewVideo()
    expect(img.className).toContain('w-full')
  })

  it('preview container clips overflow with rounded corners', () => {
    renderShotCard('16:9')
    const container = getPreviewContainer()
    expect(container.className).toContain('rounded-lg')
    expect(container.className).toContain('overflow-hidden')
  })
})
