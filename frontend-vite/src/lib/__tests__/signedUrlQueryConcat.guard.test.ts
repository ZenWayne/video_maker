// frontend-vite/src/lib/__tests__/signedUrlQueryConcat.guard.test.ts
//
// @ts-nocheck — this file does Node fs/path source scanning (build tooling,
// not app code); @types/node isn't a project dependency (see
// useShotSync.test.tsx for the existing precedent of this pragma).
//
// C1 regression guard: presigned COS URLs always carry a query string
// (`?q-sign-algorithm=…&q-signature=<sig>`). Cache-busting a media URL by
// literally concatenating `${url}?v=${...}` / `${url}?t=${...}` produces a
// SECOND `?`, which `parse_qs`/`URLSearchParams` folds into the value of
// whatever the last real query param was (typically `q-signature`),
// corrupting the signature and making COS return 403 SignatureDoesNotMatch.
// Demonstrated in the final-review report:
//
//   signed:   …/output.mp4?q-sign-algorithm=sha1&…&q-signature=5c5a9d10…bca4
//   + "?v=…": …&q-signature=5c5a9d105bfc17a414b5f8f8cb7b662bfed6bca4?v=1700000000
//
// Twelve sites across ProgressStream.tsx/ShotCard.tsx/ShotsPage.tsx did this
// and were fixed by simply not cache-busting at all (signed_url() in
// app/services/object_store.py re-signs — and therefore changes q-sign-time —
// on every call, so the URL is already unique per generation without any
// extra query param).
//
// This is a pure static scan — no credentials, no network, no COS client —
// over the actual frontend source tree, so it fails if the `${x}?param=${y}`
// concatenation pattern is reintroduced ANYWHERE, not just at the twelve
// sites fixed here.
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, extname } from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC_ROOT = join(fileURLToPath(import.meta.url), '..', '..', '..') // frontend-vite/src

// Matches `${anything}?param=${` — a template-literal expression immediately
// followed by a bare `?param=` and another interpolation. This is exactly
// the shape of the forbidden pattern (e.g. `${video_path}?v=${Date.now()}`)
// and does NOT match legitimate first-query-param literals like
// `` `?index=${index}` `` (no preceding `${...}}` before the `?`).
const FORBIDDEN = /\$\{[^}]*\}\?[a-zA-Z_][a-zA-Z0-9_]*=\$\{/

const SKIP_DIRS = new Set(['__tests__', 'node_modules'])

function collectSourceFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (SKIP_DIRS.has(entry)) continue
    const full = join(dir, entry)
    const stat = statSync(full)
    if (stat.isDirectory()) {
      collectSourceFiles(full, out)
    } else if (['.ts', '.tsx'].includes(extname(full))) {
      out.push(full)
    }
  }
  return out
}

describe('no ?-concatenation cache-busting on already-query-stringed URLs (C1 guard)', () => {
  it('scans frontend-vite/src for the forbidden `${expr}?param=${expr}` pattern', () => {
    const files = collectSourceFiles(SRC_ROOT)
    // Sanity: the scan itself must actually be walking the real tree (currently
    // ~45 non-test .ts/.tsx files under src/) — guards against a silently
    // broken/empty walk making this test vacuously pass.
    expect(files.length).toBeGreaterThan(30)

    const violations: string[] = []
    for (const file of files) {
      const lines = readFileSync(file, 'utf8').split('\n')
      lines.forEach((line, i) => {
        if (FORBIDDEN.test(line)) {
          violations.push(`${file.slice(SRC_ROOT.length)}:${i + 1}: ${line.trim()}`)
        }
      })
    }

    expect(violations).toEqual([])
  })

  it('sanity check: the guard regex actually catches the exact historical bug pattern', () => {
    const buggyLine = 'video_path: `${completedData.video_path}?t=${Date.now()}`,'
    expect(FORBIDDEN.test(buggyLine)).toBe(true)
  })

  it('sanity check: the guard regex does NOT flag a legitimate first-query-param literal', () => {
    const okLine = "const query = index !== undefined ? `?index=${index}` : ''"
    expect(FORBIDDEN.test(okLine)).toBe(false)
  })
})
