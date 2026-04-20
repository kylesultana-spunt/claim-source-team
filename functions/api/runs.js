// GET /api/runs — returns the latest workflow run status so the portal can
// show "Last run: 2026-04-20 14:02 UTC · success" in the header.
import { json, requireEnv } from "../_shared.js";

const WORKFLOW_FILE = "pipeline.yml";

export const onRequestGet = async ({ env }) => {
  try {
    requireEnv(env);
    const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/workflows/${WORKFLOW_FILE}/runs?per_page=5`;
    const r = await fetch(url, {
      headers: {
        "authorization": `Bearer ${env.GITHUB_TOKEN}`,
        "accept": "application/vnd.github+json",
        "user-agent": "spunt-admin-portal",
      },
    });
    if (!r.ok) return json({ error: `GitHub ${r.status}` }, r.status);
    const data = await r.json();
    const runs = (data.workflow_runs || []).slice(0, 5).map(w => ({
      id: w.id, status: w.status, conclusion: w.conclusion,
      created_at: w.created_at, updated_at: w.updated_at,
      html_url: w.html_url,
    }));
    return json({ latest: runs[0] || null, runs });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
};
