import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: [['list'], ['html', { open: 'never' }]],

  use: {
    baseURL: 'http://0.0.0.0:4000',
    // Connect to Playwright server running in container
    connectOptions: {
      wsEndpoint: 'ws://127.0.0.1:3003',
    },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    extraHTTPHeaders: {
      'X-User-Name': 'pw-test',
    },
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
