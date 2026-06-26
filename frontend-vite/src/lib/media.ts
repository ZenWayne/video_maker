import type { Shot } from './types'

/**
 * Cache-bust a shot's media URLs by keying them on the shot's DB `updated_at`,
 * which changes on every (re)generation / trim / VC / CC. This makes the served
 * URL unique per generation so the browser can never replay a stale cached copy
 * of an overwritten-in-place file (e.g. output.mp4).
 *
 * Idempotent: a URL that already carries a query string (e.g. a live SSE update
 * that appended `?t=`/`?v=`) is left untouched.
 */
export function versionShotMedia(shot: Shot): Shot {
  const stamp = shot.updated_at ? Date.parse(shot.updated_at) : Date.now()
  const bust = (url: string | null | undefined): string | null | undefined =>
    !url || url.includes('?') ? url : `${url}?v=${stamp}`
  return {
    ...shot,
    video_path: bust(shot.video_path) ?? null,
    last_frame_path: bust(shot.last_frame_path) ?? null,
  }
}
