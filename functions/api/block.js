// POST /api/block — editor veto. Removes a claim from fact_check_queue.csv
// and appends it to rhetoric_archive.csv with rejection_reason=editor_blocked
// so the verdict stage skips it on the next run.
//
// Body: { atomic_claim: "...", source_url: "..." }
// Match is on (atomic_claim, source_url) because the same claim can legitimately
// appear in multiple articles.
import { json, requireEnv, ghGetFile, ghPutFile, parseCSV, serializeCSV } from "../_shared.js";

function utcStamp() {
  const d = new Date();
  const pad = n => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth()+1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}

export const onRequestPost = async ({ request, env }) => {
  try {
    requireEnv(env);
    const { atomic_claim, source_url } = await request.json();
    if (!atomic_claim) return json({ error: "atomic_claim required" }, 400);

    // 1. Load fact_check_queue.csv + find the row.
    const fc = await ghGetFile(env, "data/fact_check_queue.csv");
    if (fc.notFound) return json({ error: "fact_check_queue.csv missing" }, 404);
    const fcParsed = parseCSV(fc.text);
    const idx = fcParsed.rows.findIndex(r =>
      (r.atomic_claim || "") === atomic_claim &&
      (r.source_url || "") === (source_url || r.source_url)
    );
    if (idx === -1) return json({ error: "claim not found in fact_check_queue" }, 404);

    const blocked = { ...fcParsed.rows[idx] };
    fcParsed.rows.splice(idx, 1);

    // 2. Prepare the row for rhetoric_archive.
    blocked.status = "archived_rhetoric";
    blocked.verifiability_status = "not_checkable";
    blocked.rejection_reason = "editor_blocked";
    blocked.needs_human_review = "FALSE";
    blocked.fetched_at = blocked.fetched_at || utcStamp();

    // 3. Load rhetoric_archive.csv and append.
    const ra = await ghGetFile(env, "data/rhetoric_archive.csv");
    let raHeaders = fcParsed.headers, raRows = [], raSha = undefined;
    if (!ra.notFound) {
      const p = parseCSV(ra.text);
      raHeaders = p.headers.length ? p.headers : fcParsed.headers;
      raRows = p.rows;
      raSha = ra.sha;
    }
    // Normalise to rhetoric's header order.
    const rowForRa = {};
    for (const h of raHeaders) rowForRa[h] = blocked[h] ?? "";
    raRows.push(rowForRa);

    // 4. Commit both files. Do rhetoric first: if the second commit fails the
    //    worst-case is a duplicate rhetoric row, not a lost claim.
    const msg = `admin: block claim — ${atomic_claim.slice(0, 80)}`;
    await ghPutFile(env, "data/rhetoric_archive.csv",
                    serializeCSV(raHeaders, raRows), raSha, msg);
    await ghPutFile(env, "data/fact_check_queue.csv",
                    serializeCSV(fcParsed.headers, fcParsed.rows), fc.sha, msg);

    return json({ ok: true, moved: true });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
};
