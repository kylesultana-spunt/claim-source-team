// POST /api/claim — appends a manually-entered claim to one of the queue CSVs.
// Body: { queue: "fact_check_queue"|"review_queue"|"rhetoric_archive", row: {...} }
import { json, requireEnv, ghGetFile, ghPutFile, parseCSV, serializeCSV } from "../_shared.js";

const VALID_QUEUES = ["fact_check_queue", "review_queue", "rhetoric_archive"];

// Exact column order the pipeline and frontend expect. Keep in sync with
// src/spunt/schema.py QUEUE_COLS — any drift here breaks the CSV readers.
const QUEUE_HEADERS = [
  "claim_text", "atomic_claim", "speaker", "role", "party",
  "source_name", "source_url", "publication_date", "fetched_at",
  "claim_type", "verifiability_status", "fact_checkability_score",
  "evidence_target", "numeric_flag", "legal_flag", "comparison_flag",
  "timeframe_present", "needs_human_review", "rejection_reason", "status",
];

function utcStamp() {
  const d = new Date();
  const pad = n => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth()+1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}

export const onRequestPost = async ({ request, env }) => {
  try {
    requireEnv(env);
    const { queue, row } = await request.json();
    if (!VALID_QUEUES.includes(queue)) return json({ error: "invalid queue" }, 400);
    if (!row || !row.atomic_claim) return json({ error: "row.atomic_claim required" }, 400);

    const path = `data/${queue}.csv`;
    const current = await ghGetFile(env, path);
    let headers = QUEUE_HEADERS, rows = [], sha = undefined;
    if (!current.notFound) {
      const parsed = parseCSV(current.text);
      headers = parsed.headers.length ? parsed.headers : QUEUE_HEADERS;
      rows = parsed.rows;
      sha = current.sha;
    }

    const fullRow = {};
    for (const h of headers) fullRow[h] = row[h] ?? "";
    if (!fullRow.fetched_at) fullRow.fetched_at = utcStamp();

    rows.push(fullRow);
    const newText = serializeCSV(headers, rows);
    const msg = `admin: add manual claim to ${queue}`;
    await ghPutFile(env, path, newText, sha, msg);
    return json({ ok: true, queue, written: rows.length });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
};
