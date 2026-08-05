import { Routes, Route } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Toaster } from '@/components/ui/toaster'
import { ImagePreviewProvider } from '@/components/ImagePreview'
import HomePage from './pages/HomePage'
import ProjectPage from './pages/ProjectPage'
import ScriptPage from './pages/ScriptPage'
import ShotsPage from './pages/ShotsPage'
import ExportPage from './pages/ExportPage'
import NewProjectPage from './pages/NewProjectPage'
import AnalysesPage from './pages/AnalysesPage'
import NewAnalysisPage from './pages/NewAnalysisPage'
import AnalysisDetailPage from './pages/AnalysisDetailPage'

function App() {
  return (
    <TooltipProvider>
      <ImagePreviewProvider>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/projects/new" element={<NewProjectPage />} />
          <Route path="/projects/:id" element={<ProjectPage />} />
          <Route path="/projects/:id/script" element={<ScriptPage />} />
          <Route path="/projects/:id/shots" element={<ShotsPage />} />
          <Route path="/projects/:id/export" element={<ExportPage />} />
          <Route path="/analyses" element={<AnalysesPage />} />
          <Route path="/analyses/new" element={<NewAnalysisPage />} />
          <Route path="/analyses/:id" element={<AnalysisDetailPage />} />
        </Routes>
        <Toaster />
      </ImagePreviewProvider>
    </TooltipProvider>
  )
}

export default App
