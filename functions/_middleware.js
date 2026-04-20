// functions/_middleware.js
// Runs for every Pages Function request. We only guard /api/* — static files
// (including admin.html itself) stay public since they read the same CSVs the
// Cloudflare site is already serving. Real protection lives on the write
// actions (trigger, claim, block) that this middleware gates.

const constantTimeEquals = (a, b) => {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
};

export const onRequest = async ({ request, env, next }) => {
  const url = new URL(request.url);
  if (!url.pathname.startsWith("/api/")) return next();

  const expected = env.ADMIN_PASSWORD;
  if (!expected) {
    return new Response(JSON.stringify({
      error: "ADMIN_PASSWORD env var not set on Cloudflare Pages"
    }), { status: 500, headers: { "content-type": "application/json" } });
  }

  const given = request.headers.get("X-Admin-Password") || "";
  if (!constantTimeEquals(given, expected)) {
    return new Response(JSON.stringify({ error: "unauthorized" }), {
      status: 401, headers: { "content-type": "application/json" },
    });
  }

  return next();
};
