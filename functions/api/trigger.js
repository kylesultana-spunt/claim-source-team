// POST /api/trigger — kicks off the GitHub Actions pipeline workflow.
import { json, requireEnv } from "../_shared.js";

const WORKFLOW_FILE = "pipeline.yml";

export const onRequestPost = async ({ request, env }) => {
  try {
    requireEnv(env);
    const body = await request.json().catch(() => ({}));
    const stage = body.stage || "all";
    if (!["collect", "extract", "analyse", "verdict", "all"].includes(stage)) {
      return json({ error: "invalid stage" }, 400);
    }

    const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
    const r = await fetch(url, {
      method: "POST",
      headers: {
        "authorization": `Bearer ${env.GITHUB_TOKEN}`,
        "accept": "application/vnd.github+json",
        "user-agent": "spunt-admin-portal",
        "content-type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs: { stage } }),
    });

    if (r.status === 204) return json({ ok: true, stage });
    return json({ error: `GitHub ${r.status}: ${await r.text()}` }, r.status);
  } catch (e) {
    return json({ error: e.message }, 500);
  }
};
