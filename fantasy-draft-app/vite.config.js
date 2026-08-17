import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// IMPORTANT: update `base` to match your GitHub repo name before deploying,
// e.g. if your repo is github.com/you/fantasydrafter, set base to
// "/fantasydrafter/". GitHub Pages serves the site from that subpath, and
// without this, all your asset URLs will 404 once deployed (though it'll
// work fine locally with `npm run dev`, which is a common gotcha).
export default defineConfig({
  plugins: [react()],
  base: "/fantasydrafter/",
});