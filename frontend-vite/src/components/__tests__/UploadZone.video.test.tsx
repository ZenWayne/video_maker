import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { fireEvent } from '@testing-library/react'
import { UploadZone } from '../UploadZone'

function makeFile(name: string, type: string): File {
  return new File(['x'], name, { type })
}

function getFileInput(container: HTMLElement): HTMLInputElement {
  const input = container.querySelector('input[type="file"]')
  if (!input) throw new Error('file input not found')
  return input as HTMLInputElement
}

describe('UploadZone - 视频模式', () => {
  it('kind="video" 时展示视频文案与接受视频文件', () => {
    const onChange = vi.fn()
    const { container } = render(
      <UploadZone kind="video" maxFiles={1} value={[]} onChange={onChange} />
    )

    expect(screen.getByText('点击或拖拽上传视频')).toBeInTheDocument()
    expect(screen.getByText('MP4 / MOV')).toBeInTheDocument()

    const input = getFileInput(container)
    expect(input.accept).toBe('video/*')

    const videoFile = makeFile('clip.mp4', 'video/mp4')
    fireEvent.change(input, { target: { files: [videoFile] } })

    expect(onChange).toHaveBeenCalledWith([videoFile])
  })

  it('kind="video" 时拒绝图片文件', () => {
    const onChange = vi.fn()
    const { container } = render(
      <UploadZone kind="video" maxFiles={1} value={[]} onChange={onChange} />
    )

    const input = getFileInput(container)
    const imageFile = makeFile('pic.png', 'image/png')
    fireEvent.change(input, { target: { files: [imageFile] } })

    expect(onChange).not.toHaveBeenCalled()
  })

  it('现有图片用法（kind="character"）保持不变：接受图片、拒绝视频', () => {
    const onChange = vi.fn()
    const { container } = render(
      <UploadZone kind="character" maxFiles={3} value={[]} onChange={onChange} />
    )

    // 文案未回归为视频文案
    expect(screen.queryByText('点击或拖拽上传视频')).toBeNull()
    expect(screen.getByText('支持 JPG、PNG、WebP 格式')).toBeInTheDocument()

    const input = getFileInput(container)
    expect(input.accept).toBe('image/*')

    const videoFile = makeFile('clip.mp4', 'video/mp4')
    fireEvent.change(input, { target: { files: [videoFile] } })
    expect(onChange).not.toHaveBeenCalled()

    const imageFile = makeFile('pic.png', 'image/png')
    fireEvent.change(input, { target: { files: [imageFile] } })
    expect(onChange).toHaveBeenCalledWith([imageFile])
  })

  it('现有图片用法（kind="scene"）保持不变：接受图片、拒绝视频', () => {
    const onChange = vi.fn()
    const { container } = render(
      <UploadZone kind="scene" maxFiles={3} value={[]} onChange={onChange} />
    )

    const input = getFileInput(container)
    expect(input.accept).toBe('image/*')

    const videoFile = makeFile('clip.mp4', 'video/mp4')
    fireEvent.change(input, { target: { files: [videoFile] } })
    expect(onChange).not.toHaveBeenCalled()

    const imageFile = makeFile('pic.jpg', 'image/jpeg')
    fireEvent.change(input, { target: { files: [imageFile] } })
    expect(onChange).toHaveBeenCalledWith([imageFile])
  })
})
