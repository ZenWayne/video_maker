/**
 * API helpers for test fixture seeding.
 * Uses real backend HTTP API where possible; direct DB seed script for complex states.
 */

import * as fs from 'fs'
import * as path from 'path'
import { execSync } from 'child_process'

const BASE = 'http://localhost:8002'
const USER = 'pw-test'
const headers = { 'Content-Type': 'application/json', 'X-User-Name': USER }

export async function createProject(title: string, themeText: string): Promise<string> {
  const res = await fetch(`${BASE}/api/projects`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ title, theme_text: themeText }),
  })
  if (!res.ok) throw new Error(`createProject failed: ${res.status} ${await res.text()}`)
  const data = await res.json() as { id: string }
  return data.id
}

export async function uploadReferenceImage(projectId: string): Promise<void> {
  const imgPath = path.resolve(__dirname, '../fixtures/test-character.jpg')
  const imgData = fs.readFileSync(imgPath)
  const form = new FormData()
  form.append('files', new Blob([imgData], { type: 'image/jpeg' }), 'test-character.jpg')
  form.append('kind', 'character')
  const res = await fetch(`${BASE}/api/projects/${projectId}/reference-images`, {
    method: 'POST',
    headers: { 'X-User-Name': USER },
    body: form,
  })
  if (!res.ok) throw new Error(`uploadReferenceImage failed: ${res.status} ${await res.text()}`)
}

export async function deleteProject(projectId: string): Promise<void> {
  await fetch(`${BASE}/api/projects/${projectId}`, {
    method: 'DELETE',
    headers,
  })
}

export async function getProject(projectId: string): Promise<Record<string, unknown>> {
  const res = await fetch(`${BASE}/api/projects/${projectId}`)
  if (!res.ok) throw new Error(`getProject failed: ${res.status}`)
  return res.json()
}

/**
 * Seed a project in a specific state by calling the Python seed script.
 * Returns the seeded project ID.
 */
export function seedProjectState(state: string, opts: Record<string, string> = {}): string {
  const scriptPath = path.resolve(__dirname, 'seed.py')
  const projectRoot = path.resolve(__dirname, '../..')
  const backendDir = path.join(projectRoot, 'backend')
  const devDb = path.join(projectRoot, 'backend', 'data', 'dev.db')
  const envFile = path.join(backendDir, '.env.test-seed')

  // Write env file (uv sandbox strips process.env; --env-file is the only way to pass vars)
  fs.writeFileSync(envFile, `DATABASE_URL=sqlite+aiosqlite:///${devDb}\n`)

  const argsJson = JSON.stringify({ state, ...opts })
  const result = execSync(
    `uv run --env-file .env.test-seed --project . python ${scriptPath} '${argsJson}'`,
    { cwd: backendDir, encoding: 'utf8' }
  ).trim()

  fs.unlinkSync(envFile)

  // Last line of output is the project ID
  const lines = result.split('\n')
  return lines[lines.length - 1].trim()
}
