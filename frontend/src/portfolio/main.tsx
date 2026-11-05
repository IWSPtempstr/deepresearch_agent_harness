import React from "react";
import ReactDOM from "react-dom/client";
import { PortfolioApp } from "./PortfolioApp";
import "./portfolio.css";

// Fully static portfolio entry: no API client, no QueryClient, no router, no backend calls.
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <PortfolioApp />
  </React.StrictMode>,
);
