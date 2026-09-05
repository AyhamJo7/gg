import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createHashRouter, RouterProvider } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { AnalyticsPage } from "./pages/AnalyticsPage";
import { MissionControlPage } from "./pages/MissionControlPage";
import { NewMissionPage } from "./pages/NewMissionPage";
import { PriorityPage } from "./pages/PriorityPage";
import { ProjectsPage } from "./pages/ProjectsPage";
import { ProvidersPage } from "./pages/ProvidersPage";
import "./styles.css";

const router = createHashRouter([
  {
    element: <AppShell />,
    children: [
      { path: "/", element: <MissionControlPage /> },
      { path: "/new", element: <NewMissionPage /> },
      { path: "/projects", element: <ProjectsPage /> },
      { path: "/providers", element: <ProvidersPage /> },
      { path: "/priority", element: <PriorityPage /> },
      { path: "/analytics", element: <AnalyticsPage /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
