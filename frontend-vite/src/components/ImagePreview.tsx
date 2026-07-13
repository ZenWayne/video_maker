// components/ImagePreview.tsx
// 全局图片放大预览基础模块：
//   - <ImagePreviewProvider> 在 App 根挂一次，渲染唯一一个 lightbox 遮罩；
//   - <PreviewableImage> 是 <img> 的即插即用替代，点击即放大（沿用原 ShotCard 的遮罩样式）；
//   - usePreview() 拿到 openPreview(url)，给非 <img> 场景（如背景图、canvas 缩略）手动触发。
import {
  createContext, useCallback, useContext, useState,
  type ImgHTMLAttributes, type ReactNode,
} from 'react'
import { X } from 'lucide-react'

// 接受 null/undefined 便于直接替换 setState 式的 setPreviewUrl；空值不开预览。
type OpenPreview = (url: string | null | undefined) => void

const ImagePreviewContext = createContext<OpenPreview>(() => {})

export function ImagePreviewProvider({ children }: { children: ReactNode }) {
  const [url, setUrl] = useState<string | null>(null)
  const open = useCallback<OpenPreview>((u) => {
    if (u) setUrl(u)
  }, [])

  return (
    <ImagePreviewContext.Provider value={open}>
      {children}
      {url && (
        // z-[100]：高于 Radix Dialog(z-50)，保证从任意弹窗内也能盖住全屏
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center bg-black/70"
          onClick={() => setUrl(null)}
        >
          <button
            type="button"
            className="absolute right-4 top-4 flex h-8 w-8 items-center justify-center rounded-full bg-white shadow"
            onClick={() => setUrl(null)}
          >
            <X className="h-5 w-5 text-zinc-700" />
          </button>
          <img
            src={url}
            alt="预览"
            className="max-h-[90vh] max-w-[90vw] rounded-lg object-contain shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </ImagePreviewContext.Provider>
  )
}

/** 拿到 openPreview(url)：用于背景图 / canvas / 自定义触发场景。 */
export function usePreview(): OpenPreview {
  return useContext(ImagePreviewContext)
}

/**
 * <img> 的即插即用替代：点击放大到全屏 lightbox。
 * 透传所有原生 img 属性（src/alt/className…）。若调用方自带 onClick，
 * 先执行它，再打开预览——因此仅用于「点击=放大」语义的图片；
 * 点击另有含义的图片（如切换选中态）请继续用普通 <img>。
 */
export function PreviewableImage({
  src, className = '', onClick, ...rest
}: ImgHTMLAttributes<HTMLImageElement>) {
  const open = usePreview()
  return (
    <img
      src={src}
      className={`cursor-zoom-in ${className}`}
      onClick={(e) => {
        onClick?.(e)
        if (src) open(String(src))
      }}
      {...rest}
    />
  )
}
