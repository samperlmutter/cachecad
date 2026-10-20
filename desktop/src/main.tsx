import React, { lazy, Suspense } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./style.css";
const Harness =
  import.meta.env.DEV &&
  new URLSearchParams(location.search).has("preview-test")
    ? lazy(() => import("./PreviewHarness"))
    : null;
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {Harness ? (
      <Suspense>
        <Harness />
      </Suspense>
    ) : (
      <App />
    )}
  </React.StrictMode>,
);
