import { describe, it, expect } from 'vitest'
import { versionShotMedia } from '../media'

const baseShot: any = {
  id: 1, project_id: 'p', shot_id: 1, status: 'completed',
  video_path: '/api/media/p/shots/shot_1/output.mp4',
  last_frame_path: '/api/media/p/shots/shot_1/last_frame.png',
  updated_at: '2026-06-25T13:48:10',
}

describe('versionShotMedia', () => {
  it('appends ?v=<updated_at epoch> to plain media URLs', () => {
    const v = Date.parse(baseShot.updated_at)
    const out = versionShotMedia(baseShot)
    expect(out.video_path).toBe(`/api/media/p/shots/shot_1/output.mp4?v=${v}`)
    expect(out.last_frame_path).toBe(`/api/media/p/shots/shot_1/last_frame.png?v=${v}`)
  })

  it('changes the URL when updated_at changes (new generation busts cache)', () => {
    const a = versionShotMedia(baseShot).video_path
    const b = versionShotMedia({ ...baseShot, updated_at: '2026-06-25T14:00:00' }).video_path
    expect(a).not.toBe(b)
  })

  it('leaves already-querystringed URLs untouched (idempotent vs live SSE busts)', () => {
    const busted = { ...baseShot, video_path: '/api/media/p/shots/shot_1/output.mp4?t=123' }
    expect(versionShotMedia(busted).video_path).toBe('/api/media/p/shots/shot_1/output.mp4?t=123')
  })

  it('passes through null media paths', () => {
    const out = versionShotMedia({ ...baseShot, video_path: null, last_frame_path: null })
    expect(out.video_path).toBeNull()
    expect(out.last_frame_path).toBeNull()
  })
})
