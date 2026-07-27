// frontend-vite/src/hooks/useVideoErrorRetry.ts
import { useCallback, useEffect, useRef } from 'react'

/**
 * COS-signed media URLs carry a TTL (backend-side; the frontend never sees or
 * hardcodes it). A page left open past the TTL gets a stale signed URL, which
 * shows up as a `<video onError>` (typically a 403 from COS).
 *
 * This hook wires that recovery: on the first error for a given `url`, call
 * `refetch` once to obtain a freshly-signed URL from the real data source
 * (e.g. re-GET the project, or re-run whatever endpoint produced the URL).
 * It deliberately retries **only once per url** — a second error on the same
 * url means the media is genuinely broken (not just stale), so retrying
 * again would only loop forever. Once the caller re-renders with a new
 * (freshly-signed) `url`, the attempt flag resets so the *next* expiry can
 * be rescued too.
 */
export function useVideoErrorRetry(
  url: string | null | undefined,
  refetch: () => void | Promise<void>
): () => void {
  const attemptedRef = useRef(false)

  useEffect(() => {
    attemptedRef.current = false
  }, [url])

  return useCallback(() => {
    if (!url) return
    if (attemptedRef.current) return
    attemptedRef.current = true
    void refetch()
  }, [url, refetch])
}
