import { useState, type ReactNode } from "react";
import { Sidebar } from "./Sidebar";
import { ResumeSwitcher } from "./ResumeSwitcher";
import { NoticeBanner } from "./NoticeBanner";
import { Menu } from "lucide-react";

interface LayoutProps {
  children: ReactNode;
}

export function Layout({ children }: LayoutProps) {
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <div className="flex h-screen overflow-hidden bg-gray-50">
      {/* Desktop sidebar — hidden below lg */}
      <div className="hidden lg:flex">
        <Sidebar />
      </div>

      {/* Mobile sidebar drawer — overlay */}
      {mobileOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div
            className="fixed inset-0 bg-black/50"
            onClick={() => setMobileOpen(false)}
          />
          <div className="relative z-50 h-full w-64">
            <Sidebar mobile onClose={() => setMobileOpen(false)} />
          </div>
        </div>
      )}

      <div className="flex flex-1 flex-col overflow-hidden">
        {/* Header bar with hamburger on mobile + centered brand + resume switcher */}
        <header className="flex h-12 items-center border-b border-gray-200 bg-white px-4 lg:px-6">
          <button
            onClick={() => setMobileOpen(true)}
            className="mr-2 rounded-md p-1.5 text-gray-500 hover:bg-gray-100 lg:hidden"
            aria-label="Open navigation menu"
          >
            <Menu className="h-5 w-5" />
          </button>
          <div className="flex-1" />
          <span className="text-sm font-bold tracking-wide text-gray-900">reventlabs</span>
          <div className="flex-1 flex justify-end">
            <ResumeSwitcher />
          </div>
        </header>
        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto max-w-7xl px-4 py-6 lg:px-6 lg:py-8">
            <NoticeBanner />
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}
