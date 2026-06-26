import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { VoiceCalibrationPanel } from '../VoiceCalibrationPanel'

const base = {
  referenceVoicePath: null,
  referenceVoiceShotId: null,
  autoVoiceCalibrate: false,
  onUpload: vi.fn(),
  onRemove: vi.fn(),
  onToggleAuto: vi.fn(),
  onCalibrateAll: vi.fn(),
}

describe('VoiceCalibrationPanel', () => {
  it('disables auto switch when no base voice', () => {
    render(<VoiceCalibrationPanel {...base} />)
    expect(screen.getByLabelText('自动音色校准')).toBeDisabled()
  })

  it('shows uploaded file name and enables auto switch', () => {
    render(<VoiceCalibrationPanel {...base} referenceVoicePath="/media/p/reference_voice/prompt.wav" />)
    expect(screen.getByText(/prompt\.wav/)).toBeInTheDocument()
    expect(screen.getByLabelText('自动音色校准')).not.toBeDisabled()
  })

  it('fires onToggleAuto when switched', () => {
    const onToggleAuto = vi.fn()
    render(<VoiceCalibrationPanel {...base} referenceVoiceShotId={2} onToggleAuto={onToggleAuto} />)
    fireEvent.click(screen.getByLabelText('自动音色校准'))
    expect(onToggleAuto).toHaveBeenCalledWith(true)
  })
})
