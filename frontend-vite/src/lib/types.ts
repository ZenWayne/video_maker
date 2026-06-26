// lib/types.ts - TypeScript 类型定义

export interface VideoInfo {
  fps: number
  total_frames: number
  duration: number
}

export type ProjectStatus =
  | 'draft'
  | 'scripting'
  | 'script_review'
  | 'shot_generating'
  | 'shot_review'
  | 'exporting'
  | 'exported'
  | 'failed'

export type ShotStatus =
  | 'pending'
  | 'prompt_generating'
  | 'video_generating'
  | 'completed'
  | 'failed'

export type ShotType = 'Close-up' | 'Medium Shot' | 'Wide Shot'

export type ShotDuration = 4 | 6 | 8

export type ReferenceImageKind = 'character' | 'scene'

export type AspectRatio = '16:9' | '9:16'

export interface Project {
  id: string
  title: string
  theme_text: string
  aspect_ratio: AspectRatio
  creator_name: string
  status: ProjectStatus
  scene_overview: string | null
  final_video_path: string | null
  error_message: string | null
  reference_voice_shot_id: number | null
  reference_voice_path: string | null
  auto_voice_calibrate: boolean
  created_at: string
  updated_at: string
}

export type VcStatus = 'converting' | 'done' | 'failed'
export type CcStatus = 'calibrating' | 'done' | 'failed'
export type TfStatus = 'generating' | 'done' | 'failed'

export interface Shot {
  id: number
  project_id: string
  shot_id: number
  text: string
  shot_type: ShotType
  visual_description: string
  shot_duration: ShotDuration
  status: ShotStatus
  align_with_previous: boolean
  use_prev_last_frame: boolean
  motion_prompt: string | null
  first_frame_path: string | null
  video_path: string | null
  last_frame_path: string | null
  word_count_warning: boolean
  error_message: string | null
  custom_first_frame_path: string | null
  custom_reference_paths: string[] | null
  reference_image_hint: string | null
  vc_status: VcStatus | null
  vc_error_message: string | null
  cc_status: CcStatus | null
  cc_error_message: string | null
  target_last_frame_path: string | null
  tf_status: TfStatus | null
  tf_error_message: string | null
  tf_confirmed: boolean
  auto_trim: boolean
}

export interface ReferenceImage {
  id: string
  project_id: string
  kind: ReferenceImageKind
  filename: string
  storage_path: string
  order_index: number
}

export interface ProjectDetail extends Project {
  shots: Shot[]
  reference_images: ReferenceImage[]
}

export type SSEEventType =
  | 'state_snapshot'
  | 'state_change'
  | 'script_ready'
  | 'shot_started'
  | 'shot_progress'
  | 'shot_completed'
  | 'shot_failed'
  | 'all_shots_ready'
  | 'shot_review_ready'
  | 'export_done'
  | 'pipeline_failed'
  | 'worker_status'
  | 'vc_started'
  | 'vc_completed'
  | 'vc_failed'
  | 'vc_batch_done'
  | 'cc_started'
  | 'cc_completed'
  | 'cc_failed'
  | 'cc_batch_done'
  | 'tf_started'
  | 'tf_pose_analyzed'
  | 'tf_completed'
  | 'tf_failed'

export interface WorkerStatusData {
  message: string
  attempt: number
  delay: number
}

export interface SSEEvent {
  type: SSEEventType
  data: unknown
}

export interface StateSnapshotData {
  project: Project
  shots: Shot[]
}

export interface ScriptReadyData {
  storyboard: {
    shots: Shot[]
  }
}

export interface ShotProgressData {
  shot_id: number
  sub_status: ShotStatus
}

export interface ShotCompletedData {
  shot_id: number
  video_path: string
  last_frame_path: string
}

export interface ShotFailedData {
  shot_id: number
  error_message: string
}

export interface ExportDoneData {
  final_video_path: string
}

export interface ShotReviewReadyData {
  completed: number
  total: number
  has_failures: boolean
}

export interface PipelineFailedData {
  error_message: string
}

export interface APIError {
  code: string
  message: string
}

export interface Toast {
  id: string
  type: 'info' | 'success' | 'warning' | 'error'
  message: string
}
