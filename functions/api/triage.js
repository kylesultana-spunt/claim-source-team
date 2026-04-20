// POST /api/triage — editor triage for a pending claim.
//
// Body: {
//   atomic_claim: "<text>",          // used to locate the pending row
//   source_url:   "<url>",           // disambiguates when the same claim
//                                    // is made by multiple people/articles
//   decision:     "fact_check" | "rhetoric" | "reject",
//   note:         "<optional free-text editor note>"
// }
//
// Effect per decision:
//   fact_check → append a QUEUE-shaped row to fact_check_queue.csv with
//                status="approved_for_check", then drop the source row
//                from pending_claims.csv. Downstream verdict.py will now
//                pick it up on the next "Run verdicts" click.
//   rhetoric   → append to rhetoric_archive.csv with
//                status="archived_rhetoric", drop from pending.
//   reject     → simply drop from pending (not kept anywhere; if an editor
//                changes their mind they can re-fetch from inbox).
//
// The two CSV writes happen in sequence: append to the destination first,
// then remove from pending. If the destination append fails we bail before
// touching pending, so a row is never "lost between files".
import {
  json, requireEnv, ghGetFile, ghPutFile, parseCSV, serializeCSV,
} from "../_shared.js";

const PENDING_PATH = "data/pending_claims.csv";
const PENDING_HEADERS = [
  "claim_text", "atomic_claim", "speaker", "role", "party",
  "source_name", "source_url", "publication_date", "fetched_at",
];

// Must match src/spunt/schema.py QUEUE_COLS.
const QUEUE_HEADERS = [
  "claim_text", "atomic_claim", "speaker", "role", "party",
  "source_name", "source_url", "publication_date", "fetched_at",
  "claim_type", "verifiability_status", "fact_checkability_score",
  "evidence_target", "numeric_flag", "legal_flag", "comparison_flag",
  "timeframe_present", "needs_human_review", "rejection_reason", "status",
];

const DECISIONS = {
  fact_check: {
    target: "data/fact_check_queue.csv",
    status: "approved_for_check",
    msg: "admin: promote claim to fact-check queue",
  },
  rhetoric: {
    target: "data/rhetoric_archive.csv",
    status: "archived_rhetoric",
    msg: "admin: archive claim as rhetoric",
  },
  reject: {
    target: null,  // no destination file; drop only
    status: null,
    msg: "admin: reject claim from pending",
  },
};

function utcStamp() {
  const d = new Date();
  const pad = n => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth()+1)}-${pad(d.getUTCDate())} ` +
         `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}

// Build a QUEUE-shaped row from a pending-claims row + editor decision.
// Fields the analyser would have filled in (claim_type, scores, flags, etc.)
// are left blank — a human editor has already made the triage decision, so
// we don't need the model's classification metadata.
function toQueueRow(pending, status, note) {
  const base = {};
  for (const h of QUEUE_HEADERS) base[h] = pending[h] ?? "";
  base.status = status;
  base.needs_human_review = "FALSE";
  base.numeric_flag = "FALSE";
  base.legal_flag = "FALSE";
  base.comparison_flag = "FALSE";
  base.timeframe_present = "FALSE";
  base.fact_checkability_score = "";
  // If the editor left a note, stash it in rejection_reason (the only
  // free-text column available across queues without schema changes).
  if (note) base.rejection_reason = String(note).slice(0, 500);
  return base;
}

export const onRequestPost = async ({ request, env }) => {
  try {
    requireEnv(env);
    const body = await request.json().catch(() => ({}));
    const { atomic_claim, source_url, decision, note } = body;

    if (!atomic_claim) return json({ error: "atomic_claim required" }, 400);
    if (!DECISIONS[decision]) return json({ error: "invalid decision" }, 400);

    // 1. Load pending and locate the row.
    const pend = await ghGetFile(env, PENDING_PATH);
    if (pend.notFound) return json({ error: "pending_claims.csv not found" }, 404);
    const parsed = parseCSV(pend.text);
    const headers = parsed.headers.length ? parsed.headers : PENDING_HEADERS;
    const rows = parsed.rows;

    const idx = rows.findIndex(r =>
      (r.atomic_claim || "") === atomic_claim &&
      // source_url is optional — only use it to disambiguate if provided.
      (!source_url || (r.source_url || "") === source_url)
    );
    if (idx < 0) return json({ error: "claim not found in pending" }, 404);
    const claimRow = rows[idx];

    // 2. Write to the destination (unless reject).
    const dec = DECISIONS[decision];
    if (dec.target) {
      const dest = await ghGetFile(env, dec.target);
      let destHeaders = QUEUE_HEADERS;
      let destRows = [];
      let destSha;
      if (!dest.notFound) {
        const p = parseCSV(dest.text);
        destHeaders = p.headers.length ? p.headers : QUEUE_HEADERS;
        destRows = p.rows;
        destSha = dest.sha;
      }
      const newRow = toQueueRow(claimRow, dec.status, note);
      // Use the destination's actual header order when serializing.
      const aligned = {};
      for (const h of destHeaders) aligned[h] = newRow[h] ?? "";
      destRows.push(aligned);
      const destText = serializeCSV(destHeaders, destRows);
      await ghPutFile(env, dec.target, destText, destSha, dec.msg);
    }

    // 3. Drop the claim from pending and save.
    rows.splice(idx, 1);
    const pendText = serializeCSV(headers, rows);
    await ghPutFile(env, PENDING_PATH, pendText, pend.sha,
                    `admin: remove triaged claim from pending (${decision})`);

    return json({
      ok: true,
      decision,
      target: dec.target,
      pending_remaining: rows.length,
      stamped_at: utcStamp(),
    });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
};
