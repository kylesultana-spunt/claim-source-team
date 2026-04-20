// POST /api/block — removed.
//
// The old two-queue model (fact_check_queue + rhetoric_archive) is gone.
// Everything now starts in claims_raw.csv and the editor either sends a
// claim for verification or dismisses it via POST /api/triage.
//
// This stub stays here so any cached admin client hitting the old
// endpoint gets a clear signal instead of a 404 that hides the real
// reason. It can be safely deleted once you're sure nothing points here.
import { json } from "../_shared.js";

export const onRequestPost = async () => {
  return json({
    error: "endpoint removed — use POST /api/triage with decision: 'dismiss'",
  }, 410);
};
