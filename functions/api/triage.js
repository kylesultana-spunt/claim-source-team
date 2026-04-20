// POST /api/triage — editor decides what to do with a pending claim.
//
// Body: {
//   atomic_claim: "<text>",           // locates the row in claims_raw.csv
//   source_url:   "<url>",            // optional, disambiguates duplicates
//   decision:     "verify" | "dismiss",
//   note:         "<optional editor note — kept in the summary column>"
// }
//
// Effect per decision:
//   verify  → append a full verification row to sent_to_verify.csv with
//             status="pending". Downstream `verdict` will pick it up.
//             The row is removed from claims_raw.csv.
//   dismiss → just remove the row from claims_raw.csv. Nothing kept.
//
// The two CSV writes run in sequence: destination first, then source. If
// the destination write fails we abort before touching claims_raw.csv so
// a row is never "lost between files".
import {
  json, requireEnv, ghGetFile, ghPutFile, parseCSV, serializeCSV,
} from "../_shared.js";

const CLAIMS_PATH = "data/claims_raw.csv";
const VERIFICATION_PATH = "data/sent_to_verify.csv";

// Must match src/spunt/schema.py CLAIMS_COLS.
const CLAIMS_HEADERS = [
  "claim_text", "atomic_claim", "speaker", "role", "party",
  "source_name", "source_url", "publication_date", "fetched_at",
];

// Must match src/spunt/schema.py VERIFICATION_COLS.
const VERIFICATION_HEADERS = [
  ...CLAIMS_HEADERS,
  "sent_for_verification_at", "status", "verdict", "confidence",
  "summary", "evidence", "requires_review", "checked_at", "model",
];

function utcStamp() {
  const d = new Date();
  const pad = n => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth()+1)}-${pad(d.getUTCDate())} ` +
         `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}

function buildVerificationRow(claimRow, note) {
  const out = {};
  for (const h of VERIFICATION_HEADERS) out[h] = "";
  for (const h of CLAIMS_HEADERS) out[h] = claimRow[h] ?? "";
  out.sent_for_verification_at = utcStamp();
  out.status = "pending";
  out.requires_review = "FALSE";
  // Editor note, if provided, is stashed in the summary column until the
  // verdict runs and overwrites it. This preserves any context the editor
  // wanted attached to the claim.
  if (note) out.summary = String(note).slice(0, 800);
  return out;
}

export const onRequestPost = async ({ request, env }) => {
  try {
    requireEnv(env);
    const body = await request.json().catch(() => ({}));
    const { atomic_claim, source_url, decision, note } = body;

    if (!atomic_claim) return json({ error: "atomic_claim required" }, 400);
    if (!["verify", "dismiss"].includes(decision)) {
      return json({ error: "decision must be 'verify' or 'dismiss'" }, 400);
    }

    // 1. Load claims_raw.csv and locate the row.
    const src = await ghGetFile(env, CLAIMS_PATH);
    if (src.notFound) {
      return json({ error: "claims_raw.csv not found" }, 404);
    }
    const parsed = parseCSV(src.text);
    const srcHeaders = parsed.headers.length ? parsed.headers : CLAIMS_HEADERS;
    const srcRows = parsed.rows;

    const idx = srcRows.findIndex(r =>
      (r.atomic_claim || "") === atomic_claim &&
      (!source_url || (r.source_url || "") === source_url)
    );
    if (idx < 0) return json({ error: "claim not found in claims_raw.csv" }, 404);
    const claimRow = srcRows[idx];

    // 2. If verifying, append to sent_to_verify.csv first.
    if (decision === "verify") {
      const dst = await ghGetFile(env, VERIFICATION_PATH);
      let dstHeaders = VERIFICATION_HEADERS;
      let dstRows = [];
      let dstSha;
      if (!dst.notFound) {
        const p = parseCSV(dst.text);
        dstHeaders = p.headers.length ? p.headers : VERIFICATION_HEADERS;
        dstRows = p.rows;
        dstSha = dst.sha;
      }
      const newRow = buildVerificationRow(claimRow, note);
      // Respect whatever header order the live file already uses.
      const aligned = {};
      for (const h of dstHeaders) aligned[h] = newRow[h] ?? "";
      dstRows.push(aligned);
      const dstText = serializeCSV(dstHeaders, dstRows);
      await ghPutFile(env, VERIFICATION_PATH, dstText, dstSha,
                      "admin: send claim for verification");
    }

    // 3. Remove the row from claims_raw.csv and save.
    srcRows.splice(idx, 1);
    const srcText = serializeCSV(srcHeaders, srcRows);
    const msg = decision === "verify"
      ? "admin: remove verified claim from claims_raw.csv"
      : "admin: dismiss claim from claims_raw.csv";
    await ghPutFile(env, CLAIMS_PATH, srcText, src.sha, msg);

    return json({
      ok: true,
      decision,
      claims_remaining: srcRows.length,
      stamped_at: utcStamp(),
    });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
};
