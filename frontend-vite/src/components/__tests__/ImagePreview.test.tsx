import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { ImagePreviewProvider, PreviewableImage } from '../ImagePreview'

describe('ImagePreview 基础模块', () => {
  it('点击 PreviewableImage 打开全屏预览（放大同一 src）', () => {
    render(
      <ImagePreviewProvider>
        <PreviewableImage src="/api/media/a.png" alt="缩略" />
      </ImagePreviewProvider>,
    )
    // 初始只有缩略图，没有预览大图
    expect(screen.queryByAltText('预览')).toBeNull()

    fireEvent.click(screen.getByAltText('缩略'))

    const big = screen.getByAltText('预览') as HTMLImageElement
    expect(big).toBeInTheDocument()
    expect(big.getAttribute('src')).toBe('/api/media/a.png')
  })

  it('点击遮罩背景关闭预览', () => {
    render(
      <ImagePreviewProvider>
        <PreviewableImage src="/api/media/a.png" alt="缩略" />
      </ImagePreviewProvider>,
    )
    fireEvent.click(screen.getByAltText('缩略'))
    const big = screen.getByAltText('预览')
    // 遮罩是预览图的父级容器
    fireEvent.click(big.parentElement as HTMLElement)
    expect(screen.queryByAltText('预览')).toBeNull()
  })

  it('调用方自带的 onClick 仍会执行，且不阻止预览打开', () => {
    let clicked = false
    render(
      <ImagePreviewProvider>
        <PreviewableImage src="/api/media/a.png" alt="缩略" onClick={() => { clicked = true }} />
      </ImagePreviewProvider>,
    )
    fireEvent.click(screen.getByAltText('缩略'))
    expect(clicked).toBe(true)
    expect(screen.getByAltText('预览')).toBeInTheDocument()
  })

  it('空 src 不打开预览', () => {
    render(
      <ImagePreviewProvider>
        <PreviewableImage src="" alt="缩略" />
      </ImagePreviewProvider>,
    )
    fireEvent.click(screen.getByAltText('缩略'))
    expect(screen.queryByAltText('预览')).toBeNull()
  })
})
