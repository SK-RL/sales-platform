import { Routes, Route } from "react-router-dom";
import { ProtectedRoute } from "./lib/auth";
import { Layout } from "./components/Layout";
import { WorkWindowGate } from "./components/WorkWindowGate";
import { DashboardPage } from "./pages/DashboardPage";
import { JobsPage } from "./pages/JobsPage";
import { JobDetailPage } from "./pages/JobDetailPage";
import { ReviewQueuePage } from "./pages/ReviewQueuePage";
import { CompaniesPage } from "./pages/CompaniesPage";
import { PipelinePage } from "./pages/PipelinePage";
import { AnalyticsPage } from "./pages/AnalyticsPage";
import { LoginPage } from "./pages/LoginPage";
import { ChangePasswordRequiredPage } from "./pages/ChangePasswordRequiredPage";
import { SettingsPage } from "./pages/SettingsPage";
import { PlatformsPage } from "./pages/PlatformsPage";
import { MonitoringPage } from "./pages/MonitoringPage";
import { ResumeScorePage } from "./pages/ResumeScorePage";
import { ApplicationsPage } from "./pages/ApplicationsPage";
import { AnswerBookPage } from "./pages/AnswerBookPage";
import { UserManagementPage } from "./pages/UserManagementPage";
import { RoleClustersPage } from "./pages/RoleClustersPage";
import { CredentialsPage } from "./pages/CredentialsPage";
import { CompanyDetailPage } from "./pages/CompanyDetailPage";
import { FeedbackPage } from "./pages/FeedbackPage";
import { DocsPage } from "./pages/DocsPage";
import { IntelligencePage } from "./pages/IntelligencePage";
import { InsightsPage } from "./pages/InsightsPage";
import { ProfilesPage } from "./pages/ProfilesPage";
import { ProfileDetailPage } from "./pages/ProfileDetailPage";
import { RoutinePage } from "./pages/RoutinePage";
import { RoutineRunDetailPage } from "./pages/RoutineRunDetailPage";
import { RequiredSetupPage } from "./pages/RequiredSetupPage";
import { WorkWindowsPage } from "./pages/WorkWindowsPage";
import { InterviewQuestionsPage } from "./pages/InterviewQuestionsPage";

function ProtectedLayout({ children }: { children: React.ReactNode }) {
  return (
    <ProtectedRoute>
      {/* Work-window gate sits between auth and layout so non-admins
          outside their IST shift see the lock-out screen instead of
          the app chrome. Admins fall straight through (the gate's
          first guard short-circuits on role). */}
      <WorkWindowGate>
        <Layout>{children}</Layout>
      </WorkWindowGate>
    </ProtectedRoute>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      {/*
        F247: the change-password gate is itself authenticated (the
        backend `POST /auth/change-password` requires a valid session
        cookie + the temp password to verify), but it must NOT be
        wrapped in <ProtectedRoute>. ProtectedRoute redirects to
        /change-password when must_change_password is true; if the
        gate page itself were inside ProtectedRoute, it would infinite-
        loop. The page renders its own session check (via useAuth)
        and bounces unauth users to /login.
      */}
      <Route path="/change-password" element={<ChangePasswordRequiredPage />} />
      <Route
        path="/"
        element={
          <ProtectedLayout>
            <DashboardPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/jobs"
        element={
          <ProtectedLayout>
            <JobsPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/jobs/:id"
        element={
          <ProtectedLayout>
            <JobDetailPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/review"
        element={
          <ProtectedLayout>
            <ReviewQueuePage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/companies"
        element={
          <ProtectedLayout>
            <CompaniesPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/companies/:id"
        element={
          <ProtectedLayout>
            <CompanyDetailPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/pipeline"
        element={
          <ProtectedLayout>
            <PipelinePage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/platforms"
        element={
          <ProtectedLayout>
            <PlatformsPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/analytics"
        element={
          <ProtectedLayout>
            <AnalyticsPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/resume-score"
        element={
          <ProtectedLayout>
            <ResumeScorePage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/applications"
        element={
          <ProtectedLayout>
            <ApplicationsPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/answer-book"
        element={
          <ProtectedLayout>
            <AnswerBookPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/credentials"
        element={
          <ProtectedLayout>
            <CredentialsPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/monitoring"
        element={
          <ProtectedLayout>
            <MonitoringPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/settings"
        element={
          <ProtectedLayout>
            <SettingsPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/users"
        element={
          <ProtectedLayout>
            <UserManagementPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/role-clusters"
        element={
          <ProtectedLayout>
            <RoleClustersPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/feedback"
        element={
          <ProtectedLayout>
            <FeedbackPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/intelligence"
        element={
          <ProtectedLayout>
            <IntelligencePage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/insights"
        element={
          <ProtectedLayout>
            <InsightsPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/docs"
        element={
          <ProtectedLayout>
            <DocsPage />
          </ProtectedLayout>
        }
      />
      {/* KYC profile docs vault — admin / super_admin only. The
          backend enforces the role gate (403 from the API); we still
          show these routes to every authenticated user so an admin
          following a deep-link doesn't hit a 404 loop — the page
          itself renders a permission-denied state on 403. */}
      <Route
        path="/profiles"
        element={
          <ProtectedLayout>
            <ProfilesPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/profiles/:id"
        element={
          <ProtectedLayout>
            <ProfileDetailPage />
          </ProtectedLayout>
        }
      />
      {/* v6 Claude Routine Apply — operator panel + required-setup. The
          required-setup page nests under /answer-book so the sidebar's
          Answer Book link still feels like the parent context. */}
      <Route
        path="/routine"
        element={
          <ProtectedLayout>
            <RoutinePage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/routine/runs/:id"
        element={
          <ProtectedLayout>
            <RoutineRunDetailPage />
          </ProtectedLayout>
        }
      />
      <Route
        path="/answer-book/required-setup"
        element={
          <ProtectedLayout>
            <RequiredSetupPage />
          </ProtectedLayout>
        }
      />
      {/* Admin: per-user IST work-window configuration + extension
          requests queue. The page itself doesn't role-gate (the
          backend 403s on every API call for non-admins); the sidebar
          link is gated by role so non-admins never see it. */}
      <Route
        path="/work-windows"
        element={
          <ProtectedLayout>
            <WorkWindowsPage />
          </ProtectedLayout>
        }
      />
      {/* Ticket 8ef0e9c2 — shared interview-question repository. All
          authenticated users can read + contribute. */}
      <Route
        path="/interview-questions"
        element={
          <ProtectedLayout>
            <InterviewQuestionsPage />
          </ProtectedLayout>
        }
      />
    </Routes>
  );
}
