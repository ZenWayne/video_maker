import { Routes, Route } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Toaster } from '@/components/ui/toaster'
import HomePage from './pages/HomePage'
import ProjectPage from './pages/ProjectPage'
import ScriptPage from './pages/ScriptPage'
import ShotsPage from './pages/ShotsPage'
import ExportPage from './pages/ExportPage'
import NewProjectPage from './pages/NewProjectPage'

function App() {
  return (
    <TooltipProvider>
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/projects/new" element={<NewProjectPage />} />
        <Route path="/projects/:id" element={<ProjectPage />} />
        <Route path="/projects/:id/script" element={<ScriptPage />} />
        <Route path="/projects/:id/shots" element={<ShotsPage />} />
        <Route path="/projects/:id/export" element={<ExportPage />} />
      </Routes>
      <Toaster />
    </TooltipProvider>
  )
}

export default App
