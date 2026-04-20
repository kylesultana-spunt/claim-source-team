// functions/_shared.js — utilities shared across API endpoints.

export function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export function requireEnv(env) {
  const missing = [];
  for (const k of ["GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO"]) {
    if (!env[k]) missing.push(k);
  }
  if (missing.length) {
    throw new Error("missing env vars: " + missing.join(", "));
  }
}

const UA = "spunt-admin-portal";

// --- GitHub Contents API helpers -------------------------------------------

export async function ghGetFile(env, path, ref) {
  const url = new URL(`https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/contents/${path}`);
  if (ref) url.searchParams.set("ref", ref);
  const r = await fetch(url, {
    headers: {
      "authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "accept": "application/vnd.github+json",
      "user-agent": UA,
    },
  });
  if (r.status === 404) return { notFound: true };
  if (!r.ok) throw new Error(`GitHub GET ${path} -> ${r.status}: ${await r.text()}`);
  const body = await r.json();
  const text = atob(body.content.replace(/\n/g, ""));
  return { sha: body.sha, text, body };
}

export async function ghPutFile(env, path, text, sha, message, branch = "main") {
  const b64 = btoa(unescape(encodeURIComponent(text)));
  const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/contents/${path}`;
  const r = await fetch(url, {
    method: "PUT",
    headers: {
      "authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "accept": "application/vnd.github+json",
      "user-agent": UA,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      message, content: b64, sha, branch,
      committer: { name: "spunt-admin", email: "spunt-admin@users.noreply.github.com" },
    }),
  });
  if (!r.ok) throw new Error(`GitHub PUT ${path} -> ${r.status}: ${await r.text()}`);
  return await r.json();
}

// --- CSV parse/serialize (RFC 4180 subset) ---------------------------------

export function parseCSV(text) {
  const rows = [];
  let i = 0, field = "", row = [], inQ = false;
  while (i < text.length) {
    const c = text[i];
    if (inQ) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i += 2; continue; }
        inQ = false; i++; continue;
      }
      field += c; i++;
    } else {
      if (c === '"') { inQ = true; i++; continue; }
      if (c === ",") { row.push(field); field = ""; i++; continue; }
      if (c === "\r") { i++; continue; }
      if (c === "\n") { row.push(field); rows.push(row); field = ""; row = []; i++; continue; }
      field += c; i++;
    }
  }
  if (field.length || row.length) { row.push(field); rows.push(row); }
  if (!rows.length) return { headers: [], rows: [] };
  const headers = rows[0];
  const out = rows.slice(1)
    .filter(r => r.some(x => x !== ""))
    .map(r => Object.fromEntries(headers.map((h, j) => [h, r[j] ?? ""])));
  return { headers, rows: out };
}

function esc(v) {
  const s = v == null ? "" : String(v);
  if (/[",\n\r]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
  return s;
}

export function serializeCSV(headers, rows) {
  const out = [headers.map(esc).join(",")];
  for (const r of rows) out.push(headers.map(h => esc(r[h] ?? "")).join(","));
  return out.join("\n") + "\n";
}
