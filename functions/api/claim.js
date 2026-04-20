// POST /api/claim — appends a manually-entered claim to claims_raw.csv.
//
// Only one destination now. Manually-added claims behave exactly like
// claims produced by the extractor: they land in claims_raw.csv and the
// editor then decides whether to send them for verification or dismiss
// them via POST /api/triage.
//
// Body: { row: { <CLAIMS_COLS fields> } }
// Required:        row.atomic_claim
// Auto-filled:     fetched_at (if missing)
// Unknown columns in row are ignored — we only write CLAIMS_HEADERS.
import { json, requireEnv, ghGetFile, ghPutFile, parseCSV, serializeCSV } from "../_shared.js";

const CLAIMS_PATH = "data/claims_raw.csv";

// Must match src/spunt/schema.py CLAIMS_COLS exactly.
const CLAIMS_HEADERS = [
  "claim_text", "atomic_claim", "speaker", "role", "party",
  "source_name", "source_url", "publication_date", "fetched_at",
];

function utcStamp() {
  const d = new Date();
  const pad = n => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth()+1)}-${pad(d.getUTCDate())} ` +
         `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}

export const onRequestPost = async ({ request, env }) => {
  try {
    requireEnv(env);
    const body = await request.json().catch(() => ({}));
    const row = body.row || {};
    if (!row.atomic_claim) return json({ error: "row.atomic_claim required" }, 400);

    const current = await ghGetFile(env, CLAIMS_PATH);
    let headers = CLAIMS_HEADERS, rows = [], sha;
    if (!current.notFound) {
      const parsed = parseCSV(current.text);
      // Respect whatever header order the live file already uses, so
      // we never shuffle existing columns out of place.
      headers = parsed.headers.length ? parsed.headers : CLAIMS_HEADERS;
      rows = parsed.rows;
      sha = current.sha;
    }

    const fullRow = {};
    for (const h of headers) fullRow[h] = row[h] ?? "";
    if (!fullRow.claim_text) fullRow.claim_text = row.atomic_claim;
    if (!fullRow.fetched_at) fullRow.fetched_at = utcStamp();

    rows.push(fullRow);
    const newText = serializeCSV(headers, rows);
    await ghPutFile(env, CLAIMS_PATH, newText, sha,
                    "admin: add manual claim to claims_raw.csv");

    return json({ ok: true, written: rows.length });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
};
