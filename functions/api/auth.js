// Lightweight endpoint whose only purpose is to let the client confirm the
// stored password is still correct (middleware already checked it).
export const onRequestPost = async () => {
  return new Response(JSON.stringify({ ok: true }), {
    headers: { "content-type": "application/json" },
  });
};
