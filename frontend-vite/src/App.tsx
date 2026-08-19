import { Routes, Route } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Toaster } from '@/components/ui/toaster'
import { ImagePreviewProvider } from '@/components/ImagePreview'
import { RequireAuth } from '@/components/RequireAuth'
import { AuthProvider } from '@/lib/auth'
import HomePage from './pages/HomePage'
import ProjectPage from './pages/ProjectPage'
import ScriptPage from './pages/ScriptPage'
import ShotsPage from './pages/ShotsPage'
import ExportPage from './pages/ExportPage'
import NewProjectPage from './pages/NewProjectPage'
import AnalysesPage from './pages/AnalysesPage'
import NewAnalysisPage from './pages/NewAnalysisPage'
import AnalysisDetailPage from './pages/AnalysisDetailPage'
import LoginPage from './pages/LoginPage'

function App() {
  return (
    <TooltipProvider>
      <AuthProvider>
        <ImagePreviewProvider>
          <Routes>
            {/* 登录/注册本身必须在门禁之外，否则没人进得来 */}
            <Route path="/login" element={<LoginPage mode="login" />} />
            <Route path="/register" element={<LoginPage mode="register" />} />

            {/* 其余全部套 RequireAuth：漏包一条路由就是一个绕过口。
                门禁是否真的拦人由后端的 AUTH_ENFORCED 决定，见 RequireAuth。 */}
            <Route path="/" element={<RequireAuth><HomePage /></RequireAuth>} />
            <Route path="/projects/new" element={<RequireAuth><NewProjectPage /></RequireAuth>} />
            <Route path="/projects/:id" element={<RequireAuth><ProjectPage /></RequireAuth>} />
            <Route path="/projects/:id/script" element={<RequireAuth><ScriptPage /></RequireAuth>} />
            <Route path="/projects/:id/shots" element={<RequireAuth><ShotsPage /></RequireAuth>} />
            <Route path="/projects/:id/export" element={<RequireAuth><ExportPage /></RequireAuth>} />
            <Route path="/analyses" element={<RequireAuth><AnalysesPage /></RequireAuth>} />
            <Route path="/analyses/new" element={<RequireAuth><NewAnalysisPage /></RequireAuth>} />
            <Route path="/analyses/:id" element={<RequireAuth><AnalysisDetailPage /></RequireAuth>} />
          </Routes>
          <Toaster />
        </ImagePreviewProvider>
      </AuthProvider>
    </TooltipProvider>
  )
}

export default App
