// frontend-vite/src/hooks/useVideoErrorRetry.ts
import { useCallback, useRef } from 'react'

/**
 * COS-signed media URLs carry a TTL (backend-side; the frontend never sees or
 * hardcodes it). A page left open past the TTL gets a stale signed URL, which
 * shows up as a `<video onError>` (typically a 403 from COS).
 *
 * This hook wires that recovery: on the first error, call `refetch` once to
 * obtain a freshly-signed URL from the real data source (e.g. re-GET the
 * project, or re-run whatever endpoint produced the URL).
 *
 * IMPORTANT: the attempt flag is **not** keyed on `url` identity. Every
 * signing (`object_store.signed_url`) regenerates `q-sign-time`, so a
 * refetch NEVER returns the same url string — not even for media that is
 * genuinely, permanently broken (an orphaned DB pointer, a pre-backfill row
 * with no COS object). Resetting on url-change therefore made "url changed"
 * indistinguishable from "the retry actually worked": a permanently-broken
 * file would re-error → refetch → get a new (still-broken) signed url →
 * re-error → refetch forever. For callers like join-preview, each iteration
 * of that loop re-runs a full server-side ffmpeg merge.
 *
 * Instead, the attempt flag only resets on a genuine success signal: call
 * the returned `onLoad` from the media element's onLoadedMetadata / onLoadedData
 * / onCanPlay. That preserves the intended behaviour — one retry per genuine
 * expiry, then give up until the media actually plays again — without the
 * url-identity loophole.
 */
export function useVideoErrorRetry(
  url: string | null | undefined,
  refetch: () => void | Promise<void>
): { onError: () => void; onLoad: () => void } {
  const attemptedRef = useRef(false)

  const onError = useCallback(() => {
    if (!url) return
    if (attemptedRef.current) return
    attemptedRef.current = true
    void refetch()
  }, [url, refetch])

  const onLoad = useCallback(() => {
    attemptedRef.current = false
  }, [])

  return { onError, onLoad }
}
