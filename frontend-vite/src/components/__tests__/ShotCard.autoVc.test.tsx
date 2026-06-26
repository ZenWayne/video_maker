import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ShotCard } from '../ShotCard'

// Reuse the shot fixture shape from ShotCard.responsive.test.tsx
const shot: any = {
  id: 1, project_id: 'p', shot_id: 1, text: 't', shot_type: 'Medium Shot',
  visual_description: 'v', shot_duration: 6, status: 'completed',
  align_with_previous: false, use_prev_last_frame: false, motion_prompt: null,
  first_frame_path: null, video_path: '/v.mp4', last_frame_path: null,
  word_count_warning: false, error_message: null, custom_first_frame_path: null,
  custom_reference_paths: null, reference_image_hint: null,
  vc_status: 'converting', vc_error_message: null, cc_status: null,
  cc_error_message: null, target_last_frame_path: null, tf_status: null,
  tf_error_message: null, tf_confirmed: false,
}

describe('ShotCard auto VC hint', () => {
  it('shows 自动 hint while converting under auto mode', () => {
    render(<ShotCard shot={shot} variant="review" autoVoiceCalibrate />)
    expect(screen.getByText('自动')).toBeInTheDocument()
  })

  it('no 自动 hint when auto mode off', () => {
    render(<ShotCard shot={shot} variant="review" autoVoiceCalibrate={false} />)
    expect(screen.queryByText('自动')).not.toBeInTheDocument()
  })
})
