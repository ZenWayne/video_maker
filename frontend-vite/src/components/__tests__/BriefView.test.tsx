import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BriefView } from '@/components/BriefView'
import type { CreationBrief } from '@/lib/types'

function brief(overrides: Partial<CreationBrief> = {}): CreationBrief {
  return {
    niche_summary: '美食探店赛道以真实体验和强烈视觉冲击取胜。',
    sample_stats: { sample_n: 5, no_speech_pct: 0.2, sample_warning: '样本量偏少，结论仅供参考' },
    hook_strategy: {
      common_hook_types: ['悬念式', '反差式', '数字清单'],
      example_hooks: ['你绝对想不到这家店的秘密武器是什么', '99%的人都吃错了这道菜'],
    },
    script_structure: {
      pacing: '前3秒强钩子，中段快节奏剪辑，结尾留悬念',
      emotion: '好奇 → 惊喜 → 满足',
      info_gap: '先展示结果，再揭晓做法，制造信息差',
      cta: '引导评论区留言想吃的下一家',
    },
    do: ['开头3秒必须出现悬念', '使用真实用户视角'],
    dont: ['避免过度剪辑导致失真', '不要堆砌专业术语'],
    screenwriter_directives: '请在开场镜头使用特写强调食物质感，全片保持快节奏剪辑。',
    ...overrides,
  }
}

describe('BriefView', () => {
  it('renders niche summary, hook types, structure tiles, do/dont, and directives', () => {
    render(<BriefView brief={brief()} />)

    const view = screen.getByTestId('brief-view')
    expect(view).toBeInTheDocument()

    // niche summary
    expect(screen.getByText('美食探店赛道以真实体验和强烈视觉冲击取胜。')).toBeInTheDocument()

    // sample stats incl. warning
    expect(screen.getByText(/样本数 5/)).toBeInTheDocument()
    expect(screen.getByText(/无人声占比 20%/)).toBeInTheDocument()
    expect(screen.getByText('样本量偏少，结论仅供参考')).toBeInTheDocument()

    // hook strategy - type chips + example hooks
    expect(screen.getByText('悬念式')).toBeInTheDocument()
    expect(screen.getByText('反差式')).toBeInTheDocument()
    expect(screen.getByText('数字清单')).toBeInTheDocument()
    expect(screen.getByText(/你绝对想不到这家店的秘密武器是什么/)).toBeInTheDocument()

    // script structure - 4 tiles
    expect(screen.getByText('节奏')).toBeInTheDocument()
    expect(screen.getByText(/前3秒强钩子/)).toBeInTheDocument()
    expect(screen.getByText('情绪')).toBeInTheDocument()
    expect(screen.getByText(/好奇 → 惊喜 → 满足/)).toBeInTheDocument()
    expect(screen.getByText('信息差')).toBeInTheDocument()
    expect(screen.getByText(/先展示结果/)).toBeInTheDocument()
    expect(screen.getByText('CTA')).toBeInTheDocument()
    expect(screen.getByText(/引导评论区留言/)).toBeInTheDocument()

    // do / dont
    expect(screen.getByText('开头3秒必须出现悬念')).toBeInTheDocument()
    expect(screen.getByText('使用真实用户视角')).toBeInTheDocument()
    expect(screen.getByText('避免过度剪辑导致失真')).toBeInTheDocument()
    expect(screen.getByText('不要堆砌专业术语')).toBeInTheDocument()

    // screenwriter directives - blue highlighted block with tag
    expect(screen.getByText('→ screenwriter')).toBeInTheDocument()
    expect(
      screen.getByText(/请在开场镜头使用特写强调食物质感/)
    ).toBeInTheDocument()

    // attach button
    expect(screen.getByTestId('brief-attach-btn')).toBeInTheDocument()

    // export button is dead UI without a handler — must not render when onExport is absent
    expect(screen.queryByText('导出 Markdown')).not.toBeInTheDocument()
  })

  it('renders the export button only when onExport is provided', () => {
    const { rerender } = render(<BriefView brief={brief()} />)
    expect(screen.queryByText('导出 Markdown')).not.toBeInTheDocument()

    rerender(<BriefView brief={brief()} onExport={vi.fn()} />)
    expect(screen.getByText('导出 Markdown')).toBeInTheDocument()
  })

  it('calls onAttach and onExport when buttons are clicked', async () => {
    const user = userEvent.setup()
    const onAttach = vi.fn()
    const onExport = vi.fn()
    render(<BriefView brief={brief()} onAttach={onAttach} onExport={onExport} />)

    await user.click(screen.getByTestId('brief-attach-btn'))
    expect(onAttach).toHaveBeenCalledTimes(1)

    await user.click(screen.getByText('导出 Markdown'))
    expect(onExport).toHaveBeenCalledTimes(1)
  })
})
