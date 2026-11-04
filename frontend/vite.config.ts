import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The portfolio build is a separate, fully static entry (no API client, no app shell).
// It is what gets published to GitHub Pages; the default build remains the interactive app.
export default defineConfig(({ mode }) => {
  const isPortfolio = mode === "portfolio";
  return {
    plugins: [react()],
    base: isPortfolio ? (process.env.PORTFOLIO_BASE ?? "/") : "/",
    build: isPortfolio
      ? {
          outDir: "portfolio-dist",
          emptyOutDir: true,
          // Vite names HTML output after the source file (portfolio.html); the npm script
          // copies it to index.html afterwards so GitHub Pages serves the site root.
          rollupOptions: { input: { portfolio: "portfolio.html" } },
        }
      : undefined,
    server: { host: "127.0.0.1", port: 5173 },
  };
});
