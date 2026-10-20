import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import WorkspaceBoundary from "./components/WorkspaceBoundary";
import "./style.css";
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <WorkspaceBoundary>
      <App />
    </WorkspaceBoundary>
  </React.StrictMode>,
);
